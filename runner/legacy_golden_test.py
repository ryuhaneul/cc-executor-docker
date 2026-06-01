from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import login_core


API_KEY = "legacy-golden-key"


class TokenHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.dumps(
            {
                "access_token": "golden-access-token",
                "refresh_token": "golden-refresh-token",
                "expires_in": 3600,
                "scope": login_core.SCOPE,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def request(method: str, url: str, body: dict[str, object] | None = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, object]]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def main() -> None:
    token_server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    token_thread = threading.Thread(target=token_server.serve_forever, daemon=True)
    token_thread.start()
    port = 19100
    with tempfile.TemporaryDirectory() as tmp:
        config_dir = "/root/.claude/users/wd-legacy-golden"
        shutil.rmtree(config_dir, ignore_errors=True)
        env = os.environ.copy()
        env.update(
            {
                "CC_DISABLE_LEGACY": "false",
                "CC_API_KEY": API_KEY,
                "CC_EXECUTOR_PORT": str(port),
                "CC_OAUTH_TOKEN_URL": f"http://127.0.0.1:{token_server.server_port}/token",
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "server.py"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.time() + 10
            while time.time() < deadline:
                try:
                    status, _ = request("GET", f"{base}/health")
                    if status == 200:
                        break
                except Exception:
                    time.sleep(0.1)
            headers = {"Authorization": f"Bearer {API_KEY}", "X-Claude-Config-Dir": config_dir}
            status, models = request("GET", f"{base}/v1/models", headers={"Authorization": f"Bearer {API_KEY}"})
            assert status == 200
            assert models.get("object") == "list"
            assert isinstance(models.get("data"), list) and models["data"]

            status, started = request("POST", f"{base}/admin/oauth/start", headers=headers)
            assert status == 200, started
            assert started.get("session_id")
            auth_url = str(started["url"])
            state = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["state"][0]
            status, completed = request(
                "POST",
                f"{base}/admin/oauth/complete",
                {"session_id": started["session_id"], "code": f"golden-code#{state}", "claude_config_dir": config_dir},
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            assert status == 200, completed
            assert completed.get("ok") is True
            assert (Path(config_dir) / ".credentials.json").exists()

            url, code = login_core.parse_codex_device_login_output(
                "Open https://auth.openai.com/codex/device and enter ABCD-ABCDE"
            )
            assert url == "https://auth.openai.com/codex/device"
            assert code == "ABCD-ABCDE"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            shutil.rmtree(config_dir, ignore_errors=True)
            token_server.shutdown()
    print("PASS legacy_golden_test")


if __name__ == "__main__":
    main()
