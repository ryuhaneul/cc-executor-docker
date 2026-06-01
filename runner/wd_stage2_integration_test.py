from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


API_KEY = "stage2-integration-api-key"
SECRET = "stage2-integration-secret"


class TokenHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = {
            "access_token": "mock-access-token",
            "refresh_token": "mock-refresh-token",
            "expires_in": 60,
            "scope": "org:create_api_key user:profile user:inference",
        }
        raw = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _fmt: str, *_args: object) -> None:
        return


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def claim(body: dict[str, Any], *, slot_id: str, tenant_id: str, requester_id: str, provider: str) -> str:
    payload = {
        "slot_id": slot_id,
        "slot_tenant_id": tenant_id,
        "tenant_id": tenant_id,
        "requester_id": requester_id,
        "provider": provider,
        "config_dir": f"/data/auth/{provider}/users/{slot_id}",
        "exp": int(time.time()) + 60,
        "jti": str(uuid.uuid4()),
        "kid": "stage2-integration",
        "body_hash": hashlib.sha256(canonical(body)).hexdigest(),
        "op": "wd.login",
    }
    segment = b64url(canonical(payload))
    sig = hmac.new(SECRET.encode("utf-8"), segment.encode("ascii"), hashlib.sha256).digest()
    return f"{segment}.{b64url(sig)}"


def request_json(method: str, path: str, body: dict[str, Any], *, slot_id: str, tenant_id: str, requester_id: str, provider: str) -> dict[str, Any]:
    data = None if method == "GET" else canonical(body)
    req = urllib.request.Request(
        f"http://127.0.0.1:19101{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "X-WD-Claim": claim(body, slot_id=slot_id, tenant_id=tenant_id, requester_id=requester_id, provider=provider),
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def wait_ready() -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:19101/wd/health", timeout=1) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("wd test server did not start")


def main() -> None:
    token_server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    threading.Thread(target=token_server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin = Path(tmp) / "bin"
        fake_bin.mkdir()
        codex = fake_bin / "codex"
        codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"login\" ] && [ \"$2\" = \"--device-auth\" ]; then\n"
            "  echo 'Visit https://auth.openai.com/codex/device and enter WXYZ-12345'\n"
            "  sleep 60\n"
            "fi\n",
            encoding="utf-8",
        )
        codex.chmod(0o755)
        env = os.environ.copy()
        env.update(
            {
                "CC_API_KEY": API_KEY,
                "WD_CLAIM_SIGNING_SECRET": SECRET,
                "CC_OAUTH_TOKEN_URL": f"http://127.0.0.1:{token_server.server_port}/token",
                "WD_EXECUTOR_PORT": "19101",
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "wd_server:app", "--host", "127.0.0.1", "--port", "19101"],
            cwd="/app",
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            wait_ready()
            tenant_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            requester_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            claude_slot = "11111111-1111-4111-8111-111111111111"
            codex_slot = "22222222-2222-4222-8222-222222222222"

            login_id = str(uuid.uuid4())
            started = request_json("POST", "/wd/v1/login/start", {"login_id": login_id}, slot_id=claude_slot, tenant_id=tenant_id, requester_id=requester_id, provider="claude")
            state = re.search(r"[?&]state=([^&]+)", started["auth_url"]).group(1)  # type: ignore[union-attr]
            completed = request_json(
                "POST",
                "/wd/v1/login/complete",
                {"login_id": login_id, "code": f"mock-code#{state}"},
                slot_id=claude_slot,
                tenant_id=tenant_id,
                requester_id=requester_id,
                provider="claude",
            )
            assert completed["status"] == "ok"
            cred_path = Path(f"/data/auth/claude/users/{claude_slot}/.credentials.json")
            st = cred_path.stat()
            assert st.st_uid == 20000
            assert stat.S_IMODE(st.st_mode) == 0o600
            claude_status = request_json("GET", "/wd/v1/login/status", {}, slot_id=claude_slot, tenant_id=tenant_id, requester_id=requester_id, provider="claude")
            assert claude_status["loggedIn"] is True

            codex_login_id = str(uuid.uuid4())
            codex_started = request_json("POST", "/wd/v1/login/start", {"login_id": codex_login_id}, slot_id=codex_slot, tenant_id=tenant_id, requester_id=requester_id, provider="codex")
            assert codex_started["verification_url"].startswith("https://auth.openai.com/codex/device")
            assert codex_started["user_code"] == "WXYZ-12345"
            assert not Path(f"/data/auth/codex/users/{codex_slot}/auth.json").exists()
            codex_status = request_json("GET", "/wd/v1/login/status", {}, slot_id=codex_slot, tenant_id=tenant_id, requester_id=requester_id, provider="codex")
            assert codex_status["loggedIn"] is False
            assert codex_status["pending"] is True
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            token_server.shutdown()
    print("PASS wd_stage2_integration_test")


if __name__ == "__main__":
    main()
