from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_ROOT_CANDIDATE = Path(__file__).resolve().parents[1]
ROOT = _ROOT_CANDIDATE if (_ROOT_CANDIDATE / "server.py").exists() else Path("/app")
API_KEY = "legacy-golden-api-key"


class TokenHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        _ = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = {
            "access_token": "legacy-access-token",
            "refresh_token": "legacy-refresh-token",
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


def request_json(method: str, url: str, body: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def wait_ready(base_url: str) -> None:
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            request_json("GET", f"{base_url}/v1/models")
            return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("legacy server did not start")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        codex_bin = tmp_path / "codex"
        codex_bin.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"login\" ] && [ \"$2\" = \"--device-auth\" ]; then\n"
            "  echo 'Visit https://auth.openai.com/codex/device and enter MZHT-0HT0G'\n"
            "  sleep 60\n"
            "fi\n",
            encoding="utf-8",
        )
        codex_bin.chmod(0o755)
        token_server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
        threading.Thread(target=token_server.serve_forever, daemon=True).start()
        port = "19100"
        env = os.environ.copy()
        env.update(
            {
                "CC_API_KEY": API_KEY,
                "CC_EXECUTOR_PORT": port,
                "CC_DISABLE_LEGACY": "false",
                "CC_OAUTH_TOKEN_URL": f"http://127.0.0.1:{token_server.server_port}/token",
                "PATH": f"{tmp}:{env.get('PATH', '')}",
                "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
                "CODEX_HOME": str(tmp_path / "codex-home"),
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            wait_ready(base_url)
            status, models = request_json("GET", f"{base_url}/v1/models")
            assert status == 200
            assert "data" in models

            status, started = request_json("POST", f"{base_url}/admin/oauth/start", {})
            assert status == 200
            session_id = str(started["session_id"])
            auth_url = str(started["url"])
            state = re.search(r"[?&]state=([^&]+)", auth_url).group(1)  # type: ignore[union-attr]
            status, completed = request_json(
                "POST",
                f"{base_url}/admin/oauth/complete",
                {"session_id": session_id, "code": f"legacy-code#{state}"},
            )
            assert status == 200
            assert completed["ok"] is True

            status, codex = request_json("POST", f"{base_url}/admin/codex/login/start", {})
            assert status == 200
            assert codex["url"].startswith("https://auth.openai.com/codex/device")
            assert codex["user_code"] == "MZHT-0HT0G"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            token_server.shutdown()
            shutil.rmtree(tmp_path, ignore_errors=True)
    print("PASS legacy_golden_test")


if __name__ == "__main__":
    main()
