from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Mapping
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


def _argv_from_body(body: Mapping[str, Any]) -> list[str]:
    command = str(body.get("command") or "echo")
    args = body.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise HTTPException(status_code=422, detail="args must be a string list")

    if command == "echo":
        return [shutil.which("echo") or "/bin/echo", *(args or ["wd-stage0"])]
    if command == "id":
        allowed = {"-u", "-g", "-G", "-un", "-gn"}
        if any(item not in allowed for item in args):
            raise HTTPException(status_code=422, detail="unsupported id args")
        return [shutil.which("id") or "/usr/bin/id", *(args or ["-u"])]
    raise HTTPException(status_code=422, detail="command must be echo or id")


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
            raise WDClaimError("Stage 0 supports mode A only")
        config_dir, ws_cwd = validate_claim_paths(claims)
        slot_uid = get_or_allocate_uid(str(claims["slot_id"]))
        prepare_slot_dirs(str(claims["slot_id"]), config_dir, ws_cwd, slot_uid)
    except WDClaimError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"slot dir preparation failed: {exc}") from exc

    argv = _argv_from_body(body)
    env = _child_env()
    timeout = int(body.get("timeout") or min(TIMEOUT, 30))
    started = time.time()
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(ws_cwd),
            env=env,
            user=slot_uid,
            group=slot_uid,
            extra_groups=[],
            start_new_session=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="command timeout") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="dummy command not found") from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"setuid command failed: {exc}") from exc

    forbidden_env_present = sorted(key for key in SECRET_ENV_KEYS if key in env)
    return {
        "ok": result.returncode == 0,
        "provider": claims["provider"],
        "mode": claims["mode"],
        "slot_id": claims["slot_id"],
        "slot_uid": slot_uid,
        "gid": slot_uid,
        "config_dir": str(config_dir),
        "ws_cwd": str(ws_cwd),
        "argv": argv,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": int((time.time() - started) * 1000),
        "secret_env_present": forbidden_env_present,
    }


@app.api_route("/{path:path}", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
async def reject_legacy_on_wd_port(path: str) -> JSONResponse:
    if path.startswith("v1/"):
        return JSONResponse({"error": {"message": "legacy disabled on WD port"}}, status_code=404)
    if path.startswith("admin/"):
        return JSONResponse({"error": {"message": "legacy disabled on WD port"}}, status_code=403)
    return JSONResponse({"error": {"message": "not found"}}, status_code=404)
