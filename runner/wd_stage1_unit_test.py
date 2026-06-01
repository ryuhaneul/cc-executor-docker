from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import login_core
import wd_security
import wd_server


def test_claude_parser() -> None:
    stdout = json.dumps(
        {
            "type": "result",
            "result": "four",
            "session_id": "claude-session-a",
            "model": "claude-sonnet",
            "usage": {"input_tokens": 8, "output_tokens": 2},
        }
    )
    text, usage, session_id, model = wd_server._parse_claude_output(stdout)
    assert text == "four"
    assert usage["input_tokens"] == 8
    assert usage["output_tokens"] == 2
    assert session_id == "claude-session-a"
    assert model == "claude-sonnet"


def test_codex_parser() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "session.created", "thread_id": "codex-thread-a"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "four"}],
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 2}}),
        ]
    )
    text, usage, session_id, model = wd_server._parse_codex_output(stdout, "")
    assert text == "four"
    assert usage["input_tokens"] == 9
    assert usage["output_tokens"] == 2
    assert session_id == "codex-thread-a"
    assert model is None


class FakeProc:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.pid = 4242
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.waited = False

    def communicate(self, input: str | None = None, timeout: int | None = None) -> tuple[str, str]:
        return self.stdout, self.stderr

    def wait(self) -> int:
        self.waited = True
        return self.returncode


class TimeoutProc(FakeProc):
    def communicate(self, input: str | None = None, timeout: int | None = None) -> tuple[str, str]:
        raise subprocess.TimeoutExpired(["fake"], timeout)


def test_claude_tools_false_argv_and_true_reject() -> None:
    tool_free = wd_server._build_claude_argv(
        model=None,
        system_prompt=None,
        resume=None,
        tools_allowed=False,
    )

    assert "--disallowedTools" in tool_free
    assert "*" in tool_free
    assert "--permission-mode" not in tool_free
    try:
        wd_server._reject_unsupported_tools({"tools_allowed": True})
    except wd_server.HTTPException as exc:
        assert exc.status_code == 403
        assert exc.detail == "tools not supported until isolation sandbox lands (later stage)"
    else:
        raise AssertionError("tools_allowed=True must be rejected")


def test_execute_provider_uses_slot_identity_and_env() -> None:
    original = wd_server.subprocess.Popen
    calls: list[dict[str, object]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProc:
        calls.append({"argv": argv, **kwargs})
        return FakeProc(
            stdout=json.dumps(
                {
                    "result": "ok",
                    "session_id": "session-a",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            )
        )

    try:
        wd_server.subprocess.Popen = fake_popen  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            ws_cwd = Path(tmp) / "ws"
            config_dir.mkdir()
            ws_cwd.mkdir()
            result, text, usage, session_id, _model, argv, env = wd_server._execute_provider(
                provider="claude",
                prompt="hello",
                model="sonnet",
                system_prompt="system",
                resume=None,
                tools_allowed=False,
                timeout=5,
                config_dir=config_dir,
                ws_cwd=ws_cwd,
                slot_uid=23456,
            )
    finally:
        wd_server.subprocess.Popen = original  # type: ignore[assignment]

    assert result.returncode == 0
    assert text == "ok"
    assert usage["input_tokens"] == 1
    assert session_id == "session-a"
    assert "--disallowedTools" in argv
    assert "*" in argv
    assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)
    assert env["HOME"] == str(config_dir)
    assert env["TMPDIR"] == str(ws_cwd)
    assert not any(key in env for key in wd_server.SECRET_ENV_KEYS)
    call = calls[-1]
    assert call["cwd"] == str(ws_cwd)
    assert call["user"] == 23456
    assert call["group"] == 23456
    assert call["extra_groups"] == []
    assert call["start_new_session"] is True
    assert call["stdin"] == wd_server.subprocess.PIPE
    assert call["stdout"] == wd_server.subprocess.PIPE
    assert call["stderr"] == wd_server.subprocess.PIPE


def test_run_subprocess_timeout_kills_process_group() -> None:
    original_popen = wd_server.subprocess.Popen
    original_killpg = wd_server.os.killpg
    proc = TimeoutProc()
    kill_calls: list[tuple[int, int]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> TimeoutProc:
        return proc

    def fake_killpg(pid: int, sig: int) -> None:
        kill_calls.append((pid, sig))

    try:
        wd_server.subprocess.Popen = fake_popen  # type: ignore[assignment]
        wd_server.os.killpg = fake_killpg  # type: ignore[assignment]
        try:
            wd_server._run_subprocess(
                ["fake"],
                prompt="hello",
                timeout=1,
                cwd=Path("/tmp"),
                env={},
                slot_uid=23456,
            )
        except subprocess.TimeoutExpired:
            pass
        else:
            raise AssertionError("timeout must raise TimeoutExpired")
    finally:
        wd_server.subprocess.Popen = original_popen  # type: ignore[assignment]
        wd_server.os.killpg = original_killpg  # type: ignore[assignment]

    assert kill_calls == [(4242, wd_server.signal.SIGKILL)]
    assert proc.waited is True


def test_codex_non_resume_argv_has_no_literal_stdin_prompt() -> None:
    argv = wd_server._build_codex_argv(
        model="gpt-5-codex",
        ws_cwd=Path("/tmp/ws"),
        last_message_path=Path("/tmp/ws/.wd-codex-last-test.txt"),
        resume=None,
        tools_allowed=False,
    )

    assert argv[:2] == ["codex", "exec"]
    assert "resume" not in argv
    assert "-" not in argv
    assert "workspace-write" not in wd_server._build_codex_argv(
        model="gpt-5-codex",
        ws_cwd=Path("/tmp/ws"),
        last_message_path=Path("/tmp/ws/.wd-codex-last-test.txt"),
        resume=None,
        tools_allowed=True,
    )


def test_codex_argv_env_and_last_message_file() -> None:
    original = wd_server.subprocess.Popen
    calls: list[dict[str, object]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProc:
        calls.append({"argv": argv, **kwargs})
        last_message_path = Path(argv[argv.index("--output-last-message") + 1])
        last_message_path.write_text("from last message", encoding="utf-8")
        return FakeProc(
            stdout="\n".join(
                [
                    json.dumps({"type": "session.created", "thread_id": "thread-a"}),
                    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}),
                ]
            )
        )

    try:
        wd_server.subprocess.Popen = fake_popen  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            ws_cwd = Path(tmp) / "ws"
            config_dir.mkdir()
            ws_cwd.mkdir()
            _result, text, usage, session_id, _model, argv, env = wd_server._execute_provider(
                provider="codex",
                prompt="hello",
                model="gpt-5-codex",
                system_prompt=None,
                resume="thread-old",
                tools_allowed=False,
                timeout=5,
                config_dir=config_dir,
                ws_cwd=ws_cwd,
                slot_uid=23457,
            )
    finally:
        wd_server.subprocess.Popen = original  # type: ignore[assignment]

    assert text == "from last message"
    assert usage["input_tokens"] == 3
    assert session_id == "thread-a"
    assert env["CODEX_HOME"] == str(config_dir)
    assert env["TMPDIR"] == str(ws_cwd)
    assert "--sandbox" in argv
    assert "read-only" in argv
    exec_index = argv.index("exec")
    assert argv[exec_index + 1] == "resume"
    assert argv[-1] == "thread-old"
    assert "-" not in argv
    assert not any(key in env for key in wd_server.SECRET_ENV_KEYS)
    call = calls[-1]
    assert call["cwd"] == str(ws_cwd)
    assert call["user"] == 23457
    assert call["group"] == 23457


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _login_claim(secret: str, body: dict[str, object], **overrides: object) -> str:
    slot_id = str(overrides.pop("slot_id", uuid.uuid4()))
    tenant_id = str(overrides.pop("tenant_id", uuid.uuid4()))
    provider = str(overrides.pop("provider", "claude"))
    payload = {
        "slot_id": slot_id,
        "slot_tenant_id": str(overrides.pop("slot_tenant_id", tenant_id)),
        "tenant_id": tenant_id,
        "requester_id": str(overrides.pop("requester_id", uuid.uuid4())),
        "provider": provider,
        "config_dir": str(overrides.pop("config_dir", f"/data/auth/{provider}/users/{slot_id}")),
        "exp": int(time.time()) + 600,
        "jti": str(uuid.uuid4()),
        "kid": "stage2-test",
        "op": str(overrides.pop("op", "wd.login")),
    }
    payload.update(overrides)
    payload["body_hash"] = wd_security.compute_body_hash(body)
    segment = _b64url(wd_security.canonical_json_bytes(payload))
    sig = hmac.new(secret.encode("utf-8"), segment.encode("ascii"), hashlib.sha256).digest()
    return f"{segment}.{_b64url(sig)}"


def test_login_claim_contract_and_bad_op_rejected() -> None:
    body = {"login_id": str(uuid.uuid4())}
    token = _login_claim("secret", body)
    claims = wd_security.verify_claim(token, "secret", body=body, expected_op="wd.login")
    assert claims["op"] == "wd.login"
    assert "ws_cwd" not in claims

    bad = _login_claim("secret", body, op="wd.run")
    try:
        wd_security.verify_claim(bad, "secret", body=body, expected_op="wd.login")
    except wd_security.WDClaimError as exc:
        assert "missing claim fields" in str(exc) or "op mismatch" in str(exc)
    else:
        raise AssertionError("bad op must be rejected")


def test_login_complete_binding_mismatch_rejected() -> None:
    claims = {
        "slot_id": str(uuid.uuid4()),
        "slot_tenant_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "requester_id": str(uuid.uuid4()),
        "provider": "claude",
    }
    session = {**claims, "config_dir": "/data/auth/claude/users/x", "slot_uid": 20000}
    changed = dict(claims)
    changed["tenant_id"] = str(uuid.uuid4())
    login_id = str(uuid.uuid4())
    with wd_server._LOGIN_LOCK:
        wd_server._LOGIN_SESSIONS[login_id] = session
    try:
        try:
            wd_server._get_bound_session(login_id, changed)
        except wd_server.HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("cross-tenant complete must be rejected")
    finally:
        with wd_server._LOGIN_LOCK:
            wd_server._LOGIN_SESSIONS.pop(login_id, None)


def test_normalize_claude_oauth_code_and_state_required() -> None:
    code, state = login_core.normalize_claude_oauth_code("https://console.anthropic.com/oauth/code/callback?code=abc&state=st")
    assert (code, state) == ("abc", "st")
    assert login_core.normalize_claude_oauth_code("abc#st") == ("abc", "st")


def test_login_status_uses_slot_credentials_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp)
        creds = {
            "claudeAiOauth": {
                "accessToken": "x",
                "refreshToken": "y",
                "expiresAt": int(time.time() * 1000) + 60000,
            }
        }
        (config_dir / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")
        status = wd_server._slot_status("claude", str(uuid.uuid4()), config_dir)
        assert status["loggedIn"] is True
        assert status["expiresAt"] == creds["claudeAiOauth"]["expiresAt"]


def test_codex_login_spawn_uses_slot_uid_and_session() -> None:
    original = wd_server.subprocess.Popen
    calls: list[dict[str, object]] = []

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProc:
        calls.append({"argv": argv, **kwargs})
        return FakeProc(stdout="")

    try:
        wd_server.subprocess.Popen = fake_popen  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            proc = wd_server._spawn_codex_login(Path(tmp), 23458)
    finally:
        wd_server.subprocess.Popen = original  # type: ignore[assignment]

    assert proc.returncode == 0
    call = calls[-1]
    assert call["argv"] == ["codex", "login", "--device-auth"]
    assert call["user"] == 23458
    assert call["group"] == 23458
    assert call["extra_groups"] == []
    assert call["start_new_session"] is True


def test_codex_auth_json_owner_mode_failure() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "auth.json"
        path.write_text("{}", encoding="utf-8")
        os.chmod(path, 0o644)
        assert wd_server._codex_auth_valid(path, os.getuid()) is False
        os.chmod(path, 0o600)
        assert wd_server._codex_auth_valid(path, os.getuid()) is True
        assert wd_server._codex_auth_valid(path, os.getuid() + 10000) is False


if __name__ == "__main__":
    test_claude_parser()
    test_codex_parser()
    test_claude_tools_false_argv_and_true_reject()
    test_execute_provider_uses_slot_identity_and_env()
    test_run_subprocess_timeout_kills_process_group()
    test_codex_non_resume_argv_has_no_literal_stdin_prompt()
    test_codex_argv_env_and_last_message_file()
    test_login_claim_contract_and_bad_op_rejected()
    test_login_complete_binding_mismatch_rejected()
    test_normalize_claude_oauth_code_and_state_required()
    test_login_status_uses_slot_credentials_only()
    test_codex_login_spawn_uses_slot_uid_and_session()
    test_codex_auth_json_owner_mode_failure()
    print("PASS wd_stage1_unit_test")
