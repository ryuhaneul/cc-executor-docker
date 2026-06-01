from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
import uuid
import atexit
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

import login_core
from wd_security import (
    WDClaimError,
    get_or_allocate_uid,
    prepare_config_dir,
    prepare_slot_dirs,
    validate_claim_paths,
    validate_login_claim_path,
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
INSTANCE_ID = os.environ.get("WD_INSTANCE_ID", str(uuid.uuid4()))
LOGIN_TTL_SECONDS = min(int(os.environ.get("WD_LOGIN_TTL_SECONDS", "600")), 600)
CODEX_LOGIN_PARSE_TIMEOUT = float(os.environ.get("WD_CODEX_LOGIN_PARSE_TIMEOUT", "15"))
_LOGIN_SESSIONS: dict[str, dict[str, Any]] = {}
_LOGIN_LOCK = threading.Lock()
_REAPER_STARTED = False


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


def _kill_process_group(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def _cleanup_login_session(login_id: str, session: Mapping[str, Any] | None = None) -> None:
    data = dict(session or {})
    proc = data.get("proc")
    if isinstance(proc, subprocess.Popen):
        _kill_process_group(proc)
    with _LOGIN_LOCK:
        if login_id in _LOGIN_SESSIONS and (not session or _LOGIN_SESSIONS[login_id] is session):
            _LOGIN_SESSIONS.pop(login_id, None)


def _reap_login_sessions_once() -> None:
    now = time.time()
    expired: list[tuple[str, dict[str, Any]]] = []
    with _LOGIN_LOCK:
        for login_id, session in list(_LOGIN_SESSIONS.items()):
            if float(session.get("expires_at", 0)) <= now:
                expired.append((login_id, session))
                _LOGIN_SESSIONS.pop(login_id, None)
    for _login_id, session in expired:
        proc = session.get("proc")
        if isinstance(proc, subprocess.Popen):
            _kill_process_group(proc)


def _reaper_loop() -> None:
    while True:
        _reap_login_sessions_once()
        time.sleep(30)


def _start_reaper() -> None:
    global _REAPER_STARTED
    if _REAPER_STARTED:
        return
    _REAPER_STARTED = True
    thread = threading.Thread(target=_reaper_loop, name="wd-login-reaper", daemon=True)
    thread.start()


def _kill_all_login_sessions() -> None:
    with _LOGIN_LOCK:
        sessions = list(_LOGIN_SESSIONS.items())
        _LOGIN_SESSIONS.clear()
    for _login_id, session in sessions:
        proc = session.get("proc")
        if isinstance(proc, subprocess.Popen):
            _kill_process_group(proc)


atexit.register(_kill_all_login_sessions)


@app.on_event("startup")
async def _startup() -> None:
    _start_reaper()


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    return body


def _verify_login_claim(
    *,
    body: Mapping[str, Any],
    authorization: str | None,
    x_wd_claim: str | None,
) -> tuple[dict[str, Any], Path, int]:
    if not _auth_ok(authorization):
        raise HTTPException(status_code=403, detail="invalid API key")
    if not x_wd_claim:
        raise HTTPException(status_code=403, detail="missing WD claim")
    try:
        claims = verify_claim(x_wd_claim, CLAIM_SECRET, body=body, expected_op="wd.login")
        config_dir = validate_login_claim_path(claims)
        slot_uid = get_or_allocate_uid(str(claims["slot_id"]))
        prepare_config_dir(str(claims["slot_id"]), config_dir, slot_uid)
    except WDClaimError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"slot dir preparation failed: {exc}") from exc
    return claims, config_dir, slot_uid


def _login_id_from(body: Mapping[str, Any]) -> str:
    login_id = body.get("login_id")
    if not isinstance(login_id, str) or not login_id:
        raise HTTPException(status_code=422, detail="login_id is required")
    try:
        uuid.UUID(login_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="login_id must be a UUID") from exc
    return login_id


def _session_binding(claims: Mapping[str, Any]) -> dict[str, str]:
    return {
        "slot_id": str(claims["slot_id"]),
        "slot_tenant_id": str(claims["slot_tenant_id"]),
        "tenant_id": str(claims["tenant_id"]),
        "requester_id": str(claims["requester_id"]),
        "provider": str(claims["provider"]),
    }


def _get_bound_session(login_id: str, claims: Mapping[str, Any]) -> dict[str, Any]:
    with _LOGIN_LOCK:
        session = _LOGIN_SESSIONS.get(login_id)
    if not session:
        raise HTTPException(status_code=404, detail="login session expired or unknown")
    if float(session.get("expires_at", 0)) <= time.time():
        _cleanup_login_session(login_id, session)
        raise HTTPException(status_code=404, detail="login session expired or unknown")
    for key, value in _session_binding(claims).items():
        if session.get(key) != value:
            raise HTTPException(status_code=403, detail="login session binding mismatch")
    return session


def _write_slot_json(path: Path, data: dict[str, Any], uid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.chown(tmp_name, uid, uid)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _claude_credentials_from_token(token: Mapping[str, Any]) -> dict[str, Any]:
    expires_at_ms = (int(time.time()) + int(token.get("expires_in") or 0)) * 1000
    scopes = [item for item in str(token.get("scope") or login_core.SCOPE).split() if item]
    return {
        "claudeAiOauth": {
            "accessToken": token["access_token"],
            "refreshToken": token.get("refresh_token"),
            "expiresAt": expires_at_ms,
            "scopes": scopes or login_core.SCOPE.split(),
            "isMax": True,
        }
    }


def _slot_status(provider: str, slot_id: str, config_dir: Path) -> dict[str, Any]:
    if provider == "claude":
        expires_at, expired = login_core.claude_creds_expiry(config_dir / ".credentials.json")
    else:
        expires_at, expired = login_core.codex_auth_expiry(config_dir / "auth.json")
    pending = False
    login_expires_at: int | None = None
    now = time.time()
    with _LOGIN_LOCK:
        for session in _LOGIN_SESSIONS.values():
            if (
                session.get("provider") == provider
                and session.get("slot_id") == slot_id
                and float(session.get("expires_at", 0)) > now
            ):
                pending = True
                login_expires_at = int(float(session["expires_at"]) * 1000)
                break
    return {
        "provider": provider,
        "slot_id": slot_id,
        "loggedIn": expires_at is not None and expired is False,
        "expiresAt": expires_at,
        "expired": expired,
        "pending": pending,
        "loginExpiresAt": login_expires_at,
    }


def _read_codex_login_output(login_id: str, proc: subprocess.Popen[str]) -> None:
    if proc.stdout is None:
        return
    for line in proc.stdout:
        with _LOGIN_LOCK:
            session = _LOGIN_SESSIONS.get(login_id)
            if not session:
                return
            session["output"] = str(session.get("output", "")) + line
            url, user_code = login_core.parse_codex_device_login_output(str(session["output"]))
            if url:
                session["verification_url"] = url
            if user_code:
                session["user_code"] = user_code


def _spawn_codex_login(config_dir: Path, slot_uid: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["codex", "login", "--device-auth"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=str(config_dir),
        env=_provider_env("codex", config_dir, config_dir),
        user=slot_uid,
        group=slot_uid,
        extra_groups=[],
        start_new_session=True,
    )


def _codex_auth_valid(path: Path, uid: int) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_uid != uid or stat.st_gid != uid:
        return False
    return stat.st_mode & 0o777 == 0o600


@app.get("/wd/health")
async def health() -> dict[str, object]:
    return {
        "status": "ok",
        "instance_id": INSTANCE_ID,
        "legacy_disabled": os.environ.get("CC_DISABLE_LEGACY", "false").lower()
        in {"1", "true", "yes", "on"},
    }


@app.post("/wd/v1/login/start")
async def login_start(
    request: Request,
    authorization: str | None = Header(default=None),
    x_wd_claim: str | None = Header(default=None, alias="X-WD-Claim"),
) -> dict[str, object]:
    body = await _json_body(request)
    login_id = _login_id_from(body)
    claims, config_dir, slot_uid = _verify_login_claim(
        body=body,
        authorization=authorization,
        x_wd_claim=x_wd_claim,
    )
    provider = str(claims["provider"])
    expires_at = time.time() + LOGIN_TTL_SECONDS
    session: dict[str, Any] = {
        **_session_binding(claims),
        "config_dir": str(config_dir),
        "slot_uid": slot_uid,
        "status": "pending",
        "created_at": time.time(),
        "expires_at": expires_at,
    }
    with _LOGIN_LOCK:
        if login_id in _LOGIN_SESSIONS:
            raise HTTPException(status_code=409, detail="login_id already exists")
    if provider == "claude":
        state, code_verifier, auth_url = login_core.new_pkce()
        session["state"] = state
        session["code_verifier"] = code_verifier
        with _LOGIN_LOCK:
            _LOGIN_SESSIONS[login_id] = session
        return {"ok": True, "provider": provider, "login_id": login_id, "auth_url": auth_url}

    proc = _spawn_codex_login(config_dir, slot_uid)
    session["proc"] = proc
    session["output"] = ""
    with _LOGIN_LOCK:
        _LOGIN_SESSIONS[login_id] = session
    threading.Thread(
        target=_read_codex_login_output,
        args=(login_id, proc),
        name=f"wd-codex-login-{login_id}",
        daemon=True,
    ).start()
    deadline = time.time() + CODEX_LOGIN_PARSE_TIMEOUT
    while time.time() < deadline:
        with _LOGIN_LOCK:
            current = _LOGIN_SESSIONS.get(login_id, {})
            url = current.get("verification_url")
            user_code = current.get("user_code")
        if isinstance(url, str) and isinstance(user_code, str):
            return {
                "ok": True,
                "provider": provider,
                "login_id": login_id,
                "verification_url": url,
                "user_code": user_code,
            }
        if proc.poll() is not None:
            _cleanup_login_session(login_id, session)
            raise HTTPException(status_code=502, detail="codex login exited before device code")
        time.sleep(0.1)
    return {
        "ok": False,
        "provider": provider,
        "login_id": login_id,
        "error": "codex login did not emit device code",
    }


@app.post("/wd/v1/login/complete")
async def login_complete(
    request: Request,
    authorization: str | None = Header(default=None),
    x_wd_claim: str | None = Header(default=None, alias="X-WD-Claim"),
) -> dict[str, object]:
    body = await _json_body(request)
    login_id = _login_id_from(body)
    claims, _request_config_dir, _slot_uid = _verify_login_claim(
        body=body,
        authorization=authorization,
        x_wd_claim=x_wd_claim,
    )
    session = _get_bound_session(login_id, claims)
    provider = str(session["provider"])
    config_dir = Path(str(session["config_dir"]))
    slot_uid = int(session["slot_uid"])
    if provider == "claude":
        raw_code = body.get("code")
        if not isinstance(raw_code, str) or not raw_code:
            raise HTTPException(status_code=422, detail="code is required")
        code, state = login_core.normalize_claude_oauth_code(raw_code)
        if not code:
            raise HTTPException(status_code=422, detail="code is required")
        if state != session["state"]:
            raise HTTPException(status_code=403, detail="state mismatch")
        status, token = login_core.exchange_code_for_token(
            code,
            str(session["code_verifier"]),
            str(session["state"]),
        )
        if status != 200 or not isinstance(token, dict) or not token.get("access_token"):
            message = "token exchange failed"
            err = token.get("error") if isinstance(token, dict) else None
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                message = err["message"]
            return {"ok": False, "provider": provider, "login_id": login_id, "status": "failed", "error": _mask_secrets(message)}
        _write_slot_json(config_dir / ".credentials.json", _claude_credentials_from_token(token), slot_uid)
        _cleanup_login_session(login_id, session)
        return {"ok": True, "provider": provider, "login_id": login_id, "status": "ok"}

    proc = session.get("proc")
    if isinstance(proc, subprocess.Popen) and proc.poll() is None:
        return {"ok": False, "provider": provider, "login_id": login_id, "status": "pending"}
    auth_path = config_dir / "auth.json"
    if _codex_auth_valid(auth_path, slot_uid):
        _cleanup_login_session(login_id, session)
        return {"ok": True, "provider": provider, "login_id": login_id, "status": "ok"}
    try:
        auth_path.unlink(missing_ok=True)
    except OSError:
        pass
    _cleanup_login_session(login_id, session)
    return {
        "ok": False,
        "provider": provider,
        "login_id": login_id,
        "status": "failed",
        "error": "codex auth.json missing or invalid owner/mode",
    }


@app.get("/wd/v1/login/status")
@app.post("/wd/v1/login/status")
async def login_status(
    request: Request,
    authorization: str | None = Header(default=None),
    x_wd_claim: str | None = Header(default=None, alias="X-WD-Claim"),
) -> dict[str, object]:
    body = await _json_body(request)
    claims, config_dir, _slot_uid = _verify_login_claim(
        body=body,
        authorization=authorization,
        x_wd_claim=x_wd_claim,
    )
    return _slot_status(str(claims["provider"]), str(claims["slot_id"]), config_dir)


@app.delete("/wd/v1/login")
async def login_logout(
    request: Request,
    authorization: str | None = Header(default=None),
    x_wd_claim: str | None = Header(default=None, alias="X-WD-Claim"),
) -> dict[str, object]:
    body = await _json_body(request)
    claims, config_dir, _slot_uid = _verify_login_claim(
        body=body,
        authorization=authorization,
        x_wd_claim=x_wd_claim,
    )
    provider = str(claims["provider"])
    for name in ((".credentials.json",) if provider == "claude" else ("auth.json",)):
        try:
            (config_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    with _LOGIN_LOCK:
        matching = [
            (login_id, session)
            for login_id, session in _LOGIN_SESSIONS.items()
            if session.get("slot_id") == claims["slot_id"] and session.get("provider") == provider
        ]
    for login_id, session in matching:
        _cleanup_login_session(login_id, session)
    return {"ok": True, "provider": provider, "slot_id": claims["slot_id"]}


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
