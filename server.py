#!/usr/bin/env python3
"""cc-executor — Claude Code CLI HTTP proxy (OpenAI-compatible API).

POST /v1/chat/completions  — OpenAI-compatible chat completions
GET  /v1/models            — Available models
GET  /health               — Health check

Admin (Bearer-protected):
  GET  /admin/status              — claude auth status
  POST /admin/oauth/start         — begin OAuth 2.0 + PKCE flow
  POST /admin/oauth/complete      — exchange code, save .credentials.json
  POST /admin/credentials         — paste .credentials.json manually
  POST /admin/logout              — claude auth logout
"""

import base64
import hashlib
import json
import os
import re
import secrets
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HOST = os.environ.get("CC_EXECUTOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("CC_EXECUTOR_PORT", "9100"))
API_KEY = os.environ.get("CC_API_KEY", "")
TIMEOUT = int(os.environ.get("CC_TIMEOUT", "300"))
WORKDIR = "/app/workdir"

RETRY_DELAY_SECONDS = 5

# Model name mapping: OpenAI-style names → Claude Code CLI model names.
# Bare names (`opus`, `sonnet`) map to the standard 200K-context CLI model.
# To request 1M context, use the explicit `[1m]` suffix (`opus[1m]`,
# `sonnet[1m]`). 1M context is included on the Max plan for Opus; Sonnet
# availability depends on the account.
MODEL_MAP = {
    # 1M context — explicit opt-in via `[1m]` suffix
    "opus[1m]": "opus[1m]",
    "sonnet[1m]": "sonnet[1m]",
    "claude-opus[1m]": "opus[1m]",
    "claude-sonnet[1m]": "sonnet[1m]",
    "cc-executor/opus[1m]": "opus[1m]",
    "cc-executor/sonnet[1m]": "sonnet[1m]",
    # 200K context — default for bare model names
    "opus": "opus",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "opus200k": "opus",
    "sonnet200k": "sonnet",
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
    "claude-opus-4": "opus",
    "claude-sonnet-4": "sonnet",
    "claude-haiku-4": "haiku",
    "cc-executor/opus": "opus",
    "cc-executor/sonnet": "sonnet",
    "cc-executor/haiku": "haiku",
    "cc-executor/opus200k": "opus",
    "cc-executor/sonnet200k": "sonnet",
}

AVAILABLE_MODELS = [
    {"id": "opus[1m]", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "sonnet[1m]", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "opus", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "sonnet", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "haiku", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "opus200k", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "sonnet200k", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/opus[1m]", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/sonnet[1m]", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/opus", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/sonnet", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/haiku", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/opus200k", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/sonnet200k", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
]


def _cleanup_session_file(session_id):
    """Delete the jsonl that Claude Code wrote for this one-shot call.

    Claude Code persists every conversation (including --print runs) to
    ~/.claude/projects/<hyphenated-cwd>/<session_id>.jsonl. For an HTTP
    executor that's pure waste — the file is never resumed. We remove it
    immediately after the subprocess finishes so the volume does not
    grow unbounded across retries/fallbacks.
    """
    project_name = WORKDIR.replace("/", "-")
    path = Path.home() / ".claude" / "projects" / project_name / f"{session_id}.jsonl"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _run_claude(cli_model, prompt, system_prompt=None, max_turns=None, allowed_tools=None):
    """Run claude CLI with a resolved CLI model name (e.g. 'opus[1m]' or 'opus')."""
    session_id = str(uuid.uuid4())
    cmd = ["claude", "--print", "--setting-sources", "", "--session-id", session_id]

    if cli_model:
        cmd += ["--model", cli_model]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if allowed_tools:
        for tool in allowed_tools:
            cmd += ["--allowedTools", tool]

    print(f"[DEBUG] cmd={' '.join(cmd)}", file=sys.stderr)
    try:
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                cwd=WORKDIR,
            )
            if result.returncode == 0:
                return True, result.stdout.strip(), None
            else:
                print(f"[ERROR] returncode={result.returncode}", file=sys.stderr)
                print(f"[ERROR] stderr={result.stderr.strip()[:500]}", file=sys.stderr)
                print(f"[ERROR] stdout={result.stdout.strip()[:200]}", file=sys.stderr)
                return False, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "", "timeout"
        except FileNotFoundError:
            return False, "", "claude CLI not found"
        except Exception as e:
            return False, "", str(e)
    finally:
        _cleanup_session_file(session_id)


def _run_claude_with_retry(model, prompt, system_prompt=None, max_turns=None, allowed_tools=None):
    """Resolve model alias, run once, retry once after a short delay, and
    fall back from 1M (`foo[1m]`) to the 200K variant (`foo`) if still failing.

    Returns (ok, output, error, fallback_info) where fallback_info is either
    None or a dict {"from": "<1m model>", "to": "<200k model>"}.
    """
    resolved = MODEL_MAP.get(model, model)

    ok, output, error = _run_claude(resolved, prompt, system_prompt, max_turns, allowed_tools)
    if ok:
        return ok, output, error, None

    print(
        f"[RETRY] model={resolved} failed, retrying in {RETRY_DELAY_SECONDS}s: {(error or '')[:200]}",
        file=sys.stderr,
    )
    time.sleep(RETRY_DELAY_SECONDS)
    ok, output, error = _run_claude(resolved, prompt, system_prompt, max_turns, allowed_tools)
    if ok:
        return ok, output, error, None

    if resolved.endswith("[1m]"):
        fallback = resolved[:-4]
        print(
            f"[FALLBACK] {resolved} → {fallback} after retry failure: {(error or '')[:200]}",
            file=sys.stderr,
        )
        ok, output, error = _run_claude(fallback, prompt, system_prompt, max_turns, allowed_tools)
        fallback_info = {"from": resolved, "to": fallback} if ok else None
        return ok, output, error, fallback_info

    return ok, output, error, None


# ─── OAuth 2.0 + PKCE state (in-memory, process-local) ───
#
# We run the OAuth flow ourselves — same endpoints the official
# `claude auth login` CLI hits, same published client_id. This keeps the
# built-in CLI TUI out of the critical path (Ink's raw-mode stdin is
# opaque to programmatic I/O; been there, lost hours to it).
#
# Sessions are tiny: {state, code_verifier, created_at}, 10-minute TTL.
# They're transient by design — a process restart just voids in-flight
# logins (user retries, done). The *token* that comes out of the exchange
# is persisted to /root/.claude/.credentials.json (cc-auth volume), so
# completed logins survive restarts.

_AUTH_URL = "https://claude.ai/oauth/authorize"
_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_CLIENT_ID = os.environ.get(
    "CC_OAUTH_CLIENT_ID",
    "9d1c250a-e61b-44d9-88ed-5944d1962f5e",  # published Claude Code client_id
)
_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
_SCOPE = "org:create_api_key user:profile user:inference"

_OAUTH_SESSIONS = {}  # id -> {state, code_verifier, created_at}
_OAUTH_TTL = 600


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _reap_oauth_sessions():
    now = time.time()
    for sid in list(_OAUTH_SESSIONS.keys()):
        if now - _OAUTH_SESSIONS[sid]["created_at"] > _OAUTH_TTL:
            _OAUTH_SESSIONS.pop(sid, None)


def _normalize_code(raw):
    """Accept bare code, 'code#state', or the full callback URL.
    Returns (code, state_or_None). Empty code → ('', None)."""
    raw = (raw or "").strip()
    if not raw:
        return "", None
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        return (qs.get("code") or [""])[0], (qs.get("state") or [None])[0]
    if "#" in raw:
        code, _, state = raw.partition("#")
        return code.strip(), state.strip() or None
    if "&state=" in raw:
        code, _, rest = raw.partition("&state=")
        return code.strip(), rest.strip() or None
    return raw, None


def _exchange_code_for_token(code, code_verifier, state):
    """POST to Anthropic's token endpoint. Returns (status, body_dict)."""
    payload = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _CLIENT_ID,
        "code": code,
        "redirect_uri": _REDIRECT_URI,
        "code_verifier": code_verifier,
        "state": state,
    }).encode("utf-8")
    req = urllib.request.Request(
        _TOKEN_URL,
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


def _write_credentials(creds: dict) -> tuple[bool, str]:
    """Atomically write to /root/.claude/.credentials.json (0600)."""
    claude_dir = "/root/.claude"
    os.makedirs(claude_dir, exist_ok=True)
    target = os.path.join(claude_dir, ".credentials.json")
    tmp = target + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(creds, fh, ensure_ascii=False)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, target)
        return True, ""
    except Exception as exc:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False, str(exc)


def _claude_auth_status():
    """Return dict parsed from `claude auth status` JSON, or {}."""
    try:
        out = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        m = re.search(r"\{.*?\}", out.stdout, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return {}


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        ts = self.log_date_time_string()
        sys.stderr.write(f"[{ts}] {fmt % args}\n")

    # ── Auth ──

    def _check_auth(self):
        """Check Bearer token. Returns True if OK."""
        if not API_KEY:
            return False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:] == API_KEY:
            return True
        return False

    # ── GET ──

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        elif self.path == "/v1/models":
            if not self._check_auth():
                self._json_response(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
                return
            self._json_response(200, {"object": "list", "data": AVAILABLE_MODELS})
        elif self.path == "/admin/status":
            if not self._check_auth():
                self._json_response(401, {"error": {"message": "Invalid API key"}})
                return
            status = _claude_auth_status()
            self._json_response(200, {
                "loggedIn": bool(status.get("loggedIn")),
                "authMethod": status.get("authMethod"),
                "apiProvider": status.get("apiProvider"),
            })
        else:
            self.send_error(404)

    # ── POST ──

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions()
        elif self.path == "/admin/credentials":
            self._handle_set_credentials()
        elif self.path == "/admin/oauth/start":
            self._handle_oauth_start()
        elif self.path == "/admin/oauth/complete":
            self._handle_oauth_complete()
        elif self.path == "/admin/logout":
            self._handle_logout()
        else:
            self.send_error(404)

    # ── /admin/credentials ──
    # Manual fallback: paste the contents of an existing .credentials.json
    # (e.g. produced by `claude auth login` on another machine). Prefer
    # /admin/oauth/* — this exists for recovery scenarios.
    def _handle_set_credentials(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        body = self._read_json() or {}
        creds = body.get("credentials")
        if not isinstance(creds, dict):
            self._json_response(422, {"error": {"message": "credentials must be an object"}})
            return
        oauth = creds.get("claudeAiOauth")
        if not isinstance(oauth, dict) or not oauth.get("accessToken"):
            self._json_response(422, {"error": {"message": "credentials.claudeAiOauth.accessToken missing"}})
            return
        ok, err = _write_credentials(creds)
        if not ok:
            self._json_response(500, {"error": {"message": f"write failed: {err}"}})
            return
        status = _claude_auth_status()
        self._json_response(200, {
            "ok": bool(status.get("loggedIn")),
            "loggedIn": bool(status.get("loggedIn")),
            "authMethod": status.get("authMethod"),
        })

    # ── /admin/oauth/start ──
    # Generate a fresh PKCE pair + state, return the authorize URL.
    # The caller opens the URL in a browser; Anthropic redirects back
    # to console.anthropic.com/oauth/code/callback with "code#state" in
    # the fragment. The user copies that blob and posts it to
    # /admin/oauth/complete.
    def _handle_oauth_start(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        _reap_oauth_sessions()

        state = secrets.token_hex(32)
        code_verifier = _b64url(secrets.token_bytes(32))
        code_challenge = _b64url(hashlib.sha256(code_verifier.encode("ascii")).digest())

        session_id = str(uuid.uuid4())
        _OAUTH_SESSIONS[session_id] = {
            "state": state,
            "code_verifier": code_verifier,
            "created_at": time.time(),
        }
        qs = urllib.parse.urlencode({
            "code": "true",
            "client_id": _CLIENT_ID,
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "scope": _SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        })
        self._json_response(200, {
            "session_id": session_id,
            "url": f"{_AUTH_URL}?{qs}",
        })

    # ── /admin/oauth/complete ──
    # Accept {session_id, code}. The code can be bare, "code#state", or
    # the full callback URL — we normalize. We POST to Anthropic's token
    # endpoint, verify the bundle, then write it to .credentials.json.
    def _handle_oauth_complete(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        body = self._read_json() or {}
        sid = body.get("session_id")
        if not sid:
            self._json_response(422, {"error": {"message": "session_id is required"}})
            return
        sess = _OAUTH_SESSIONS.pop(sid, None)
        if not sess:
            self._json_response(404, {"error": {"message": "login session expired or unknown"}})
            return

        code, state_from_payload = _normalize_code(body.get("code"))
        if not code:
            self._json_response(422, {"error": {"message": "authorization code is required"}})
            return
        if state_from_payload and state_from_payload != sess["state"]:
            self._json_response(400, {"error": {"message": "state mismatch — paste came from a different session"}})
            return

        status, tok = _exchange_code_for_token(code, sess["code_verifier"], sess["state"])
        if status != 200 or not isinstance(tok, dict) or not tok.get("access_token"):
            err = tok.get("error") if isinstance(tok, dict) else None
            message = None
            etype = None
            if isinstance(err, dict):
                message = err.get("message")
                etype = err.get("type")
            # Friendly translations for the ones we actually see in practice.
            if etype == "rate_limit_error":
                message = (message or "Anthropic rate limit") + " — 10~15분 후 다시 시도하세요"
            elif etype == "invalid_request_error" and "code" in (message or "").lower():
                message = "코드가 만료됐거나 잘못됐습니다 — 새로 발급해 주세요"
            self._json_response(400 if status else 502, {
                "ok": False,
                "error": {"message": message or f"token exchange failed (HTTP {status})", "type": etype},
            })
            return

        expires_at_ms = (int(time.time()) + int(tok.get("expires_in") or 0)) * 1000
        scopes = [s for s in (tok.get("scope") or _SCOPE).split() if s] or _SCOPE.split()
        creds = {
            "claudeAiOauth": {
                "accessToken": tok["access_token"],
                "refreshToken": tok.get("refresh_token"),
                "expiresAt": expires_at_ms,
                "scopes": scopes,
                "isMax": True,
            }
        }
        ok, err = _write_credentials(creds)
        if not ok:
            self._json_response(500, {"ok": False, "error": {"message": f"write failed: {err}"}})
            return

        final = _claude_auth_status()
        self._json_response(200, {
            "ok": True,
            "loggedIn": bool(final.get("loggedIn")),
            "authMethod": final.get("authMethod"),
            "expiresAt": expires_at_ms,
            "scopes": scopes,
        })

    # ── /admin/logout ──

    def _handle_logout(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        try:
            out = subprocess.run(
                ["claude", "auth", "logout"],
                capture_output=True, text=True, timeout=10,
            )
            self._json_response(200, {"ok": out.returncode == 0, "stdout": out.stdout[-400:]})
        except Exception as exc:
            self._json_response(500, {"error": {"message": str(exc)}})

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            return None

    # ── /v1/chat/completions ──

    def _handle_chat_completions(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
            return

        body = self._read_json()
        if body is None:
            self._json_response(400, {"error": {"message": "Invalid JSON", "type": "invalid_request_error"}})
            return

        messages = body.get("messages", [])
        model = body.get("model", "sonnet")

        if not messages:
            self._json_response(400, {"error": {"message": "messages is required", "type": "invalid_request_error"}})
            return

        # Extract system prompt and user messages
        system_prompt = None
        user_parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if p.get("type") == "text"
                )
            if role == "system":
                system_prompt = content
            elif role == "user":
                user_parts.append(content)
            elif role == "assistant":
                user_parts.append(f"[Previous assistant response: {content}]")

        prompt = "\n\n".join(user_parts)

        ok, output, error, fallback_info = _run_claude_with_retry(
            model=model,
            prompt=prompt,
            system_prompt=system_prompt,
        )

        if not ok:
            err_msg = error or "Generation failed"
            print(f"[ERROR] model={model} error={err_msg}", file=sys.stderr)
            if output:
                print(f"[ERROR] stdout={output[:500]}", file=sys.stderr)
            self._json_response(500, {
                "error": {"message": err_msg, "type": "server_error"}
            })
            return

        # On successful fallback, prepend a visible notice so the caller
        # knows the response came from the 200K model, not the requested 1M.
        if fallback_info:
            notice = (
                f"[Fallback notice: requested {fallback_info['from']} but 1M "
                f"context was unavailable after retry; served with "
                f"{fallback_info['to']} (200K context) instead.]\n\n"
            )
            output = notice + output

        response = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": output},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        if fallback_info:
            response["fallback"] = fallback_info
        self._json_response(200, response)

    # ── Response helper ──

    def _json_response(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    os.makedirs(WORKDIR, exist_ok=True)
    if not API_KEY:
        print("WARNING: CC_API_KEY is not set. All requests will be rejected.", file=sys.stderr)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"cc-executor listening on {HOST}:{PORT} (threaded)", file=sys.stderr)
    print(f"  POST /v1/chat/completions", file=sys.stderr)
    print(f"  GET  /v1/models", file=sys.stderr)
    print(f"  GET  /health", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
