from __future__ import annotations

import base64
import asyncio
import hmac
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import login_core
import wd_security
import wd_server


SECRET = "stage2-login-secret"
API_KEY = "stage2-api-key"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _ids() -> tuple[str, str, str, str]:
    return (
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    )


def _claim(body: dict[str, Any], *, provider: str = "claude", op: str = "wd.login") -> str:
    tenant_id, requester_id, slot_id, _chatroom_id = _ids()
    claims = {
        "slot_id": slot_id,
        "slot_tenant_id": tenant_id,
        "tenant_id": tenant_id,
        "requester_id": requester_id,
        "provider": provider,
        "config_dir": f"/data/auth/{provider}/users/{slot_id}",
        "exp": int(time.time()) + 60,
        "jti": str(uuid.uuid4()),
        "kid": "stage2",
        "body_hash": wd_security.compute_body_hash(body),
        "op": op,
    }
    payload = wd_security.canonical_json_bytes(claims)
    sig = hmac.new(SECRET.encode("utf-8"), _b64url(payload).encode("ascii"), hashlib.sha256).digest()
    return f"{_b64url(payload)}.{_b64url(sig)}"


def _headers(body: dict[str, Any], *, provider: str = "claude", op: str = "wd.login") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {API_KEY}",
        "X-WD-Claim": _claim(body, provider=provider, op=op),
    }


class FakeRequest:
    def __init__(self, body: dict[str, Any], method: str = "POST") -> None:
        self._body = json.dumps(body).encode("utf-8")
        self.method = method

    async def body(self) -> bytes:
        return self._body


def _call_login_complete(body: dict[str, Any], *, provider: str = "claude") -> dict[str, Any]:
    headers = _headers(body, provider=provider)
    return asyncio.run(
        wd_server.login_complete(
            FakeRequest(body),  # type: ignore[arg-type]
            authorization=headers["Authorization"],
            x_wd_claim=headers["X-WD-Claim"],
        )
    )


def _call_login_status(body: dict[str, Any], *, provider: str = "claude") -> dict[str, Any]:
    headers = _headers(body, provider=provider)
    return asyncio.run(
        wd_server.login_status(
            FakeRequest(body),  # type: ignore[arg-type]
            authorization=headers["Authorization"],
            x_wd_claim=headers["X-WD-Claim"],
        )
    )


def _binding(provider: str = "claude") -> dict[str, str]:
    tenant_id, requester_id, slot_id, _chatroom_id = _ids()
    return {
        "slot_id": slot_id,
        "slot_tenant_id": tenant_id,
        "tenant_id": tenant_id,
        "requester_id": requester_id,
        "provider": provider,
    }


def _patch_executor_auth(config_dir: Path, uid: int) -> tuple[Any, Any, Any, str, str]:
    old_key = wd_server.API_KEY
    old_secret = wd_server.CLAIM_SECRET
    old_validate = wd_server.validate_login_claim_path
    old_uid = wd_server.get_or_allocate_uid
    old_prepare = wd_server.prepare_config_dir
    wd_server.API_KEY = API_KEY
    wd_server.CLAIM_SECRET = SECRET
    wd_server.validate_login_claim_path = lambda claims: config_dir  # type: ignore[assignment]
    wd_server.get_or_allocate_uid = lambda slot_id: uid  # type: ignore[assignment]
    wd_server.prepare_config_dir = lambda slot_id, path, slot_uid: None  # type: ignore[assignment]
    return old_validate, old_uid, old_prepare, old_key, old_secret


def _restore_executor_auth(saved: tuple[Any, Any, Any, str, str]) -> None:
    old_validate, old_uid, old_prepare, old_key, old_secret = saved
    wd_server.validate_login_claim_path = old_validate  # type: ignore[assignment]
    wd_server.get_or_allocate_uid = old_uid  # type: ignore[assignment]
    wd_server.prepare_config_dir = old_prepare  # type: ignore[assignment]
    wd_server.API_KEY = old_key
    wd_server.CLAIM_SECRET = old_secret


def test_login_claim_contract_and_wrong_op() -> None:
    body: dict[str, Any] = {}
    claims = wd_security.verify_claim(
        _claim(body),
        SECRET,
        body=body,
        expected_op="wd.login",
        replay_store=wd_security.MemoryReplayStore(),
        now=int(time.time()),
    )
    assert claims["op"] == "wd.login"
    assert "ws_cwd" not in claims
    bad_claims = dict(claims)
    bad_claims["ws_cwd"] = "/data/ws/x/y"
    bad_claims["body_hash"] = wd_security.compute_body_hash(body)
    payload = wd_security.canonical_json_bytes(bad_claims)
    token = f"{_b64url(payload)}.{_b64url(hmac.new(SECRET.encode(), _b64url(payload).encode(), hashlib.sha256).digest())}"
    try:
        wd_security.verify_claim(token, SECRET, body=body, expected_op="wd.login")
    except wd_security.WDClaimError as exc:
        assert "unexpected login claim fields" in str(exc)
    else:
        raise AssertionError("login claim with run-only fields must be rejected")
    try:
        wd_security.verify_claim(_claim(body, op="wd.run"), SECRET, body=body, expected_op="wd.login")
    except wd_security.WDClaimError as exc:
        assert "claim op mismatch" in str(exc)
    else:
        raise AssertionError("wrong op must be rejected")


def test_normalize_claude_oauth_code() -> None:
    assert login_core.normalize_claude_oauth_code("code-a#state-a") == ("code-a", "state-a")
    assert login_core.normalize_claude_oauth_code(
        "https://console.anthropic.com/oauth/code/callback?code=code-b&state=state-b",
    ) == ("code-b", "state-b")


def test_claude_complete_binding_state_and_session_config_dir() -> None:
    uid = os.getuid()
    with tempfile.TemporaryDirectory() as tmp:
        claim_config = Path(tmp) / "claim"
        session_config = Path(tmp) / "session"
        attacker_config = Path(tmp) / "attacker"
        session_config.mkdir()
        attacker_config.mkdir()
        saved = _patch_executor_auth(claim_config, uid)
        old_exchange = login_core.exchange_code_for_token
        try:
            login_core.exchange_code_for_token = lambda code, verifier, state: (  # type: ignore[assignment]
                200,
                {"access_token": "access-token", "refresh_token": "refresh-token", "expires_in": 60},
            )
            login_id = str(uuid.uuid4())
            wd_server._LOGIN_SESSIONS[login_id] = {
                **_binding("claude"),
                "config_dir": str(session_config),
                "slot_uid": uid,
                "state": "state-a",
                "code_verifier": "verifier-a",
                "expires_at": time.time() + 60,
            }
            body = {"login_id": login_id, "code": "code-a#state-a", "config_dir": str(attacker_config)}
            data = _call_login_complete(body)
            assert data["status"] == "ok"
            assert (session_config / ".credentials.json").exists()
            assert not (attacker_config / ".credentials.json").exists()

            wd_server._LOGIN_SESSIONS[login_id] = {
                **_binding("claude"),
                "config_dir": str(session_config),
                "slot_uid": uid,
                "state": "state-a",
                "code_verifier": "verifier-a",
                "expires_at": time.time() + 60,
            }
            bad_state = {"login_id": login_id, "code": "code-a#wrong"}
            try:
                _call_login_complete(bad_state)
            except wd_server.HTTPException as exc:
                assert exc.status_code == 403
            else:
                raise AssertionError("state mismatch must be rejected")

            wd_server._LOGIN_SESSIONS[login_id] = {
                **(_binding("claude") | {"tenant_id": "99999999-9999-4999-8999-999999999999"}),
                "config_dir": str(session_config),
                "slot_uid": uid,
                "state": "state-a",
                "code_verifier": "verifier-a",
                "expires_at": time.time() + 60,
            }
            try:
                _call_login_complete(bad_state)
            except wd_server.HTTPException as exc:
                assert exc.status_code == 403
            else:
                raise AssertionError("binding mismatch must be rejected")
        finally:
            login_core.exchange_code_for_token = old_exchange  # type: ignore[assignment]
            wd_server._LOGIN_SESSIONS.clear()
            _restore_executor_auth(saved)


def test_status_reads_slot_creds_only() -> None:
    uid = os.getuid()
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp) / "slot"
        other_dir = Path(tmp) / "other"
        config_dir.mkdir()
        other_dir.mkdir()
        (config_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"expiresAt": int(time.time() * 1000) + 60000}}),
            encoding="utf-8",
        )
        (other_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"expiresAt": int(time.time() * 1000) - 60000}}),
            encoding="utf-8",
        )
        saved = _patch_executor_auth(config_dir, uid)
        try:
            body = {"config_dir": str(other_dir)}
            data = _call_login_status(body)
            assert data["loggedIn"] is True
            assert data["expired"] is False
        finally:
            _restore_executor_auth(saved)


class FakeProc:
    def __init__(self) -> None:
        self.pid = 4242
        self.stdout = iter(["Visit https://auth.openai.com/codex/device and enter ABCD-ABCDE\n"])
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


def test_codex_spawn_slot_uid_start_new_session_and_auth_validation_failure() -> None:
    old_popen = wd_server.subprocess.Popen
    calls: list[dict[str, Any]] = []

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        calls.append({"argv": argv, **kwargs})
        return FakeProc()

    wd_server.subprocess.Popen = fake_popen  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            proc = wd_server._spawn_codex_login(Path(tmp), 23456)
            assert proc.poll() == 0
    finally:
        wd_server.subprocess.Popen = old_popen  # type: ignore[assignment]
    assert calls[-1]["argv"] == ["codex", "login", "--device-auth"]
    assert calls[-1]["user"] == 23456
    assert calls[-1]["group"] == 23456
    assert calls[-1]["extra_groups"] == []
    assert calls[-1]["start_new_session"] is True

    uid = os.getuid()
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = Path(tmp)
        (config_dir / "auth.json").write_text("{}", encoding="utf-8")
        os.chmod(config_dir / "auth.json", 0o644)
        saved = _patch_executor_auth(config_dir, uid)
        try:
            login_id = str(uuid.uuid4())
            wd_server._LOGIN_SESSIONS[login_id] = {
                **_binding("codex"),
                "config_dir": str(config_dir),
                "slot_uid": uid,
                "proc": FakeProc(),
                "expires_at": time.time() + 60,
            }
            body = {"login_id": login_id}
            data = _call_login_complete(body, provider="codex")
            assert data["status"] == "failed"
            assert not (config_dir / "auth.json").exists()
        finally:
            wd_server._LOGIN_SESSIONS.clear()
            _restore_executor_auth(saved)


def main() -> None:
    for test in (
        test_login_claim_contract_and_wrong_op,
        test_normalize_claude_oauth_code,
        test_claude_complete_binding_state_and_session_config_dir,
        test_status_reads_slot_creds_only,
        test_codex_spawn_slot_uid_start_new_session_and_auth_validation_failure,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
