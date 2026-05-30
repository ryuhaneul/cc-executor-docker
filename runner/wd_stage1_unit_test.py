from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wd_server


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["fake"], returncode, stdout=stdout, stderr="")


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


def test_execute_provider_uses_slot_identity_and_env() -> None:
    original = wd_server.subprocess.run
    calls: list[dict[str, object]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        return _completed(
            json.dumps(
                {
                    "result": "ok",
                    "session_id": "session-a",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            )
        )

    try:
        wd_server.subprocess.run = fake_run  # type: ignore[assignment]
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
        wd_server.subprocess.run = original  # type: ignore[assignment]

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


def test_codex_argv_env_and_last_message_file() -> None:
    original = wd_server.subprocess.run
    calls: list[dict[str, object]] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"argv": argv, **kwargs})
        last_message_path = Path(argv[argv.index("--output-last-message") + 1])
        last_message_path.write_text("from last message", encoding="utf-8")
        return _completed(
            "\n".join(
                [
                    json.dumps({"type": "session.created", "thread_id": "thread-a"}),
                    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3}}),
                ]
            )
        )

    try:
        wd_server.subprocess.run = fake_run  # type: ignore[assignment]
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
        wd_server.subprocess.run = original  # type: ignore[assignment]

    assert text == "from last message"
    assert usage["input_tokens"] == 3
    assert session_id == "thread-a"
    assert env["CODEX_HOME"] == str(config_dir)
    assert env["TMPDIR"] == str(ws_cwd)
    assert "--sandbox" in argv
    assert "read-only" in argv
    assert "resume" in argv
    assert "thread-old" in argv
    assert not any(key in env for key in wd_server.SECRET_ENV_KEYS)
    call = calls[-1]
    assert call["cwd"] == str(ws_cwd)
    assert call["user"] == 23457
    assert call["group"] == 23457


if __name__ == "__main__":
    test_claude_parser()
    test_codex_parser()
    test_execute_provider_uses_slot_identity_and_env()
    test_codex_argv_env_and_last_message_file()
    print("PASS wd_stage1_unit_test")
