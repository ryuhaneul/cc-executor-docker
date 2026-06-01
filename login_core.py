from __future__ import annotations

import base64
import json
import os
import re
import hashlib
import secrets
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

AUTH_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = os.environ.get("CC_OAUTH_TOKEN_URL", "https://console.anthropic.com/v1/oauth/token")
CLIENT_ID = os.environ.get("CC_OAUTH_CLIENT_ID", "9d1c250a-e61b-44d9-88ed-5944d1962f5e")
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPE = "org:create_api_key user:profile user:inference"
CODEX_LOGIN_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device[^\s]*")
CODEX_USER_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{5}\b")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def new_pkce() -> tuple[str, str, str]:
    state = secrets.token_hex(32)
    code_verifier = b64url(secrets.token_bytes(32))
    code_challenge = b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())
    qs = urllib.parse.urlencode(
        {
            "code": "true",
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return state, code_verifier, f"{AUTH_URL}?{qs}"


def normalize_claude_oauth_code(raw: str | None) -> tuple[str, str | None]:
    raw = (raw or "").strip()
    if not raw:
        return "", None
    if raw.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        fragment = urllib.parse.parse_qs(parsed.fragment)
        code = (qs.get("code") or fragment.get("code") or [""])[0]
        state = (qs.get("state") or fragment.get("state") or [None])[0]
        if not code and parsed.fragment and "#" not in raw:
            code, _, state_from_fragment = parsed.fragment.partition("#")
            state = state or state_from_fragment or None
        return code.strip(), state.strip() if isinstance(state, str) and state else None
    if "#" in raw:
        code, _, state = raw.partition("#")
        return code.strip(), state.strip() or None
    if "&state=" in raw:
        code, _, rest = raw.partition("&state=")
        return code.strip(), rest.strip() or None
    return raw, None


def exchange_code_for_token(
    code: str,
    code_verifier: str,
    state: str,
    token_url: str | None = None,
) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
            "state": state,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        token_url or TOKEN_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "cc-executor/oauth",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": {"message": f"HTTP {exc.code}"}}
        return exc.code, body
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, {"error": {"message": f"network: {exc}"}}


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def parse_codex_device_login_output(text: str) -> tuple[str | None, str | None]:
    clean = strip_ansi(text)
    url_match = CODEX_LOGIN_URL_RE.search(clean)
    code_match = CODEX_USER_CODE_RE.search(clean)
    return (url_match.group(0) if url_match else None, code_match.group(0) if code_match else None)


def atomic_write_json(path: str | Path, data: dict[str, Any], mode: int = 0o600) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def claude_creds_expiry(path: str | Path) -> tuple[int | None, bool | None]:
    try:
        with open(path, encoding="utf-8") as fh:
            creds = json.load(fh)
        oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
        expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
        expires_at = int(expires_at) if expires_at is not None else None
        return expires_at, (int(time.time() * 1000) > expires_at) if expires_at is not None else None
    except Exception:
        return None, None


def _decode_jwt_payload(token: str | None) -> dict[str, Any] | None:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def codex_auth_expiry(path: str | Path) -> tuple[int | None, bool | None]:
    try:
        with open(path, encoding="utf-8") as fh:
            auth = json.load(fh)
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        if not isinstance(tokens, dict):
            return None, None
        payload = _decode_jwt_payload(tokens.get("access_token") or tokens.get("id_token"))
        exp = payload.get("exp") if isinstance(payload, dict) else None
        expires_at = int(exp) * 1000 if exp is not None else None
        return expires_at, (int(time.time() * 1000) > expires_at) if expires_at is not None else None
    except Exception:
        return None, None
