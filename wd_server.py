from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from wd_security import (
    WDClaimError,
    get_or_allocate_uid,
    prepare_slot_dirs,
    validate_claim_paths,
    verify_claim,
)

API_KEY = os.environ.get("CC_API_KEY", "")
CLAIM_SECRET = os.environ.get("WD_CLAIM_SIGNING_SECRET", "")
TIMEOUT = int(os.environ.get("CC_TIMEOUT", "300"))
MAX_TIMEOUT = int(os.environ.get("WD_MAX_TIMEOUT", "600"))
ENV_WHITELIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "TMPDIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
SECRET_ENV_KEYS = {
    "WD_CLAIM_SIGNING_SECRET",
    "CC_API_KEY",
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
}
_SENSITIVE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\b(?:sk|sk-proj|sk-ant|sk-codex)-[A-Za-z0-9._-]+"),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
)

app = FastAPI(title="cc-executor webductor mode")


def _auth_ok(authorization: str | None) -> bool:
    if not API_KEY:
        return False
    return bool(authorization and authorization.startswith("Bearer ") and authorization[7:] == API_KEY)


def _child_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_WHITELIST if key in os.environ}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    for key in SECRET_ENV_KEYS:
        env.pop(key, None)
    return env


def _mask_secrets(value: str) -> str:
    masked = value
    for pattern in _SENSITIVE_PATTERNS:
        masked = pattern.sub("[REDACTED]", masked)
    return masked


def _bounded_timeout(body: Mapping[str, Any]) -> int:
    raw_timeout = body.get("timeout")
    if raw_timeout is None:
        requested = TIMEOUT
    elif isinstance(raw_timeout, int) and not isinstance(raw_timeout, bool):
        requested = raw_timeout
    else:
        raise HTTPException(status_code=422, detail="timeout must be an integer")
    if requested <= 0:
        raise HTTPException(status_code=422, detail="timeout must be positive")
    return min(requested, MAX_TIMEOUT)


def _run_body(body: Mapping[str, Any]) -> tuple[str, str | None, str | None, str | None, int]:
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise HTTPException(status_code=422, detail="prompt is required")

    model = body.get("model")
    system_prompt = body.get("system_prompt")
    resume = body.get("resume")
    for name, value in (
        ("model", model),
        ("system_prompt", system_prompt),
        ("resume", resume),
    ):
        if value is not None and not isinstance(value, str):
            raise HTTPException(status_code=422, detail=f"{name} must be a string")

    return prompt, model, system_prompt, resume, _bounded_timeout(body)


def _provider_env(provider: str, config_dir: Path, ws_cwd: Path) -> dict[str, str]:
    env = _child_env()
    env["HOME"] = str(config_dir)
    env["TMPDIR"] = str(ws_cwd)
    if provider == "claude":
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    elif provider == "codex":
        env["CODEX_HOME"] = str(config_dir)
    return env


def _append_common_claude_args(
    argv: list[str],
    *,
    model: str | None,
    system_prompt: str | None,
    resume: str | None,
    tools_allowed: bool,
) -> None:
    if model:
        argv += ["--model", model]
    if system_prompt:
        argv += ["--system-prompt", system_prompt]
    if resume:
        argv += ["--resume", resume]
    argv += ["--disallowedTools", "*"]


def _build_claude_argv(
    *,
    model: str | None,
    system_prompt: str | None,
    resume: str | None,
    tools_allowed: bool,
) -> list[str]:
    argv = ["claude", "--print", "--output-format", "json", "--setting-sources", ""]
    _append_common_claude_args(
        argv,
        model=model,
        system_prompt=system_prompt,
        resume=resume,
        tools_allowed=tools_allowed,
    )
    return argv


def _build_codex_argv(
    *,
    model: str | None,
    ws_cwd: Path,
    last_message_path: Path,
    resume: str | None,
    tools_allowed: bool,
) -> list[str]:
    sandbox = "read-only"
    if resume:
        argv = [
            "codex",
            "-C",
            str(ws_cwd),
            "--sandbox",
            sandbox,
            "exec",
            "resume",
            "--json",
            "--skip-git-repo-check",
            "--output-last-message",
            str(last_message_path),
        ]
    else:
        argv = [
            "codex",
            "exec",
            "--json",
            "-C",
            str(ws_cwd),
            "--skip-git-repo-check",
            "--output-last-message",
            str(last_message_path),
            "--color",
            "never",
            "--sandbox",
            sandbox,
        ]
    if model:
        argv += ["-m", model]
    if resume:
        argv.append(resume)
    return argv


def _json_objects(stdout: str) -> list[dict[str, Any]]:
    stripped = stdout.strip()
    if not stripped:
        return []
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return [parsed]
    except json.JSONDecodeError:
        pass

    objects: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
    return objects


def _usage_from(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        if isinstance(text, str):
            return text
    return ""


def _parse_claude_output(stdout: str) -> tuple[str, dict[str, Any], str | None, str | None]:
    objects = _json_objects(stdout)
    if not objects:
        return stdout.strip(), {}, None, None
    payload = objects[-1]
    text = payload.get("result") or payload.get("text") or _content_text(payload.get("content"))
    usage = _usage_from(payload.get("usage"))
    session_id = payload.get("session_id")
    model = payload.get("model")
    return (
        text if isinstance(text, str) else "",
        usage,
        session_id if isinstance(session_id, str) else None,
        model if isinstance(model, str) else None,
    )


def _message_text_from_event(event: Mapping[str, Any]) -> str:
    if event.get("role") == "assistant":
        text = _content_text(event.get("content") or event.get("message"))
        if text:
            return text
    item = event.get("item")
    if isinstance(item, dict) and item.get("role") == "assistant":
        text = _content_text(item.get("content"))
        if text:
            return text
    message = event.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        text = _content_text(message.get("content"))
        if text:
            return text
    delta = event.get("delta")
    if event.get("type") in {"assistant_message", "message"} and isinstance(delta, str):
        return delta
    return ""


def _parse_codex_output(
    stdout: str,
    last_message: str,
) -> tuple[str, dict[str, Any], str | None, str | None]:
    text = last_message.strip()
    usage: dict[str, Any] = {}
    session_id: str | None = None
    model: str | None = None
    assistant_parts: list[str] = []
    for event in _json_objects(stdout):
        if not session_id:
            raw_id = event.get("thread_id") or event.get("session_id")
            if isinstance(raw_id, str):
                session_id = raw_id
        if not model and isinstance(event.get("model"), str):
            model = str(event["model"])
        event_usage = event.get("usage") or event.get("token_usage")
        if isinstance(event_usage, dict):
            usage = dict(event_usage)
        event_text = _message_text_from_event(event)
        if event_text:
            assistant_parts.append(event_text)
    if not text and assistant_parts:
        text = assistant_parts[-1]
    return text, usage, session_id, model


def _run_subprocess(
    argv: list[str],
    *,
    prompt: str,
    timeout: int,
    cwd: Path,
    env: Mapping[str, str],
    slot_uid: int,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(cwd),
        env=dict(env),
        user=slot_uid,
        group=slot_uid,
        extra_groups=[],
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, stdout=stdout, stderr=stderr)


def _reject_unsupported_tools(claims: Mapping[str, Any]) -> None:
    if bool(claims["tools_allowed"]):
        # Owner-private full-tools requires a later filesystem boundary stage
        # (chroot/mount namespace/Landlock). Stage 1 remains tool-free.
        raise HTTPException(
            status_code=403,
            detail="tools not supported until isolation sandbox lands (later stage)",
        )


def _execute_provider(
    *,
    provider: str,
    prompt: str,
    model: str | None,
    system_prompt: str | None,
    resume: str | None,
    tools_allowed: bool,
    timeout: int,
    config_dir: Path,
    ws_cwd: Path,
    slot_uid: int,
) -> tuple[subprocess.CompletedProcess[str], str, dict[str, Any], str | None, str | None, list[str], dict[str, str]]:
    env = _provider_env(provider, config_dir, ws_cwd)
    if provider == "claude":
        argv = _build_claude_argv(
            model=model,
            system_prompt=system_prompt,
            resume=resume,
            tools_allowed=tools_allowed,
        )
        result = _run_subprocess(
            argv,
            prompt=prompt,
            timeout=timeout,
            cwd=ws_cwd,
            env=env,
            slot_uid=slot_uid,
        )
        text, usage, session_id, actual_model = _parse_claude_output(result.stdout)
        return result, text, usage, session_id, actual_model or model, argv, env

    codex_prompt = prompt
    if system_prompt:
        codex_prompt = (
            "SYSTEM INSTRUCTIONS:\n"
            f"{system_prompt}\n\n"
            "CONVERSATION PROMPT:\n"
            f"{prompt}"
        )
    last_message_path = ws_cwd / f".wd-codex-last-{uuid.uuid4().hex}.txt"
    argv = _build_codex_argv(
        model=model,
        ws_cwd=ws_cwd,
        last_message_path=last_message_path,
        resume=resume,
        tools_allowed=tools_allowed,
    )
    try:
        result = _run_subprocess(
            argv,
            prompt=codex_prompt,
            timeout=timeout,
            cwd=ws_cwd,
            env=env,
            slot_uid=slot_uid,
        )
        try:
            last_message = last_message_path.read_text(encoding="utf-8")
        except OSError:
            last_message = ""
        text, usage, session_id, actual_model = _parse_codex_output(result.stdout, last_message)
        return result, text, usage, session_id, actual_model or model, argv, env
    finally:
        try:
            last_message_path.unlink(missing_ok=True)
        except OSError:
            pass


def _error_from_result(result: subprocess.CompletedProcess[str]) -> str | None:
    if result.returncode == 0:
        return None
    raw = result.stderr.strip() or result.stdout.strip() or "CLI exited without output"
    return _mask_secrets(raw[:2000])


def _run_response(
    *,
    claims: Mapping[str, Any],
    slot_uid: int,
    config_dir: Path,
    ws_cwd: Path,
    text: str,
    usage: Mapping[str, Any],
    session_id: str | None,
    model: str | None,
    duration_ms: int,
    returncode: int,
    error: str | None,
    env: Mapping[str, str],
) -> dict[str, object]:
    return {
        "ok": returncode == 0 and error is None,
        "provider": claims["provider"],
        "mode": claims["mode"],
        "slot_id": claims["slot_id"],
        "slot_uid": slot_uid,
        "config_dir": str(config_dir),
        "ws_cwd": str(ws_cwd),
        "text": text,
        "usage": dict(usage),
        "session_id": session_id,
        "model": model,
        "duration_ms": duration_ms,
        "returncode": returncode,
        "error": error,
        "secret_env_present": sorted(key for key in SECRET_ENV_KEYS if key in env),
    }


@app.get("/wd/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "legacy_disabled": os.environ.get("CC_DISABLE_LEGACY", "false").lower()
        in {"1", "true", "yes", "on"},
    }


@app.post("/wd/v1/run")
async def run(
    request: Request,
    authorization: str | None = Header(default=None),
    x_wd_claim: str | None = Header(default=None, alias="X-WD-Claim"),
) -> dict[str, object]:
    if not _auth_ok(authorization):
        raise HTTPException(status_code=403, detail="invalid API key")
    if not x_wd_claim:
        raise HTTPException(status_code=403, detail="missing WD claim")

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")

    try:
        claims = verify_claim(x_wd_claim, CLAIM_SECRET, body=body)
        if claims["mode"] != "A":
            raise WDClaimError("Stage 1 supports mode A only")
        _reject_unsupported_tools(claims)
        config_dir, ws_cwd = validate_claim_paths(claims)
        slot_uid = get_or_allocate_uid(str(claims["slot_id"]))
        prepare_slot_dirs(str(claims["slot_id"]), config_dir, ws_cwd, slot_uid)
    except WDClaimError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"slot dir preparation failed: {exc}") from exc

    prompt, model, system_prompt, resume, timeout = _run_body(body)
    started = time.time()
    try:
        result, text, usage, session_id, actual_model, _argv, env = _execute_provider(
            provider=str(claims["provider"]),
            prompt=prompt,
            model=model,
            system_prompt=system_prompt,
            resume=resume,
            tools_allowed=bool(claims["tools_allowed"]),
            timeout=timeout,
            config_dir=config_dir,
            ws_cwd=ws_cwd,
            slot_uid=slot_uid,
        )
    except subprocess.TimeoutExpired as exc:
        env = _provider_env(str(claims["provider"]), config_dir, ws_cwd)
        return _run_response(
            claims=claims,
            slot_uid=slot_uid,
            config_dir=config_dir,
            ws_cwd=ws_cwd,
            text="",
            usage={},
            session_id=None,
            model=model,
            duration_ms=int((time.time() - started) * 1000),
            returncode=-1,
            error="CLI timeout",
            env=env,
        )
    except FileNotFoundError as exc:
        env = _provider_env(str(claims["provider"]), config_dir, ws_cwd)
        return _run_response(
            claims=claims,
            slot_uid=slot_uid,
            config_dir=config_dir,
            ws_cwd=ws_cwd,
            text="",
            usage={},
            session_id=None,
            model=model,
            duration_ms=int((time.time() - started) * 1000),
            returncode=-1,
            error=_mask_secrets(str(exc)),
            env=env,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"setuid command failed: {exc}") from exc

    return _run_response(
        claims=claims,
        slot_uid=slot_uid,
        config_dir=config_dir,
        ws_cwd=ws_cwd,
        text=text,
        usage=usage,
        session_id=session_id,
        model=actual_model,
        duration_ms=int((time.time() - started) * 1000),
        returncode=result.returncode,
        error=_error_from_result(result),
        env=env,
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def reject_legacy_on_wd_port(path: str) -> JSONResponse:
    if path.startswith("v1/"):
        return JSONResponse({"error": {"message": "legacy disabled on WD port"}}, status_code=404)
    if path.startswith("admin/"):
        return JSONResponse({"error": {"message": "legacy disabled on WD port"}}, status_code=403)
    return JSONResponse({"error": {"message": "not found"}}, status_code=404)
