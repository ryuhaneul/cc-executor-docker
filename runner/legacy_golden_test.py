from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT_CANDIDATE = Path(__file__).resolve().parents[1]
ROOT = ROOT_CANDIDATE if (ROOT_CANDIDATE / "server.py").exists() else Path("/app")
sys.path.insert(0, str(ROOT))

import server  # noqa: E402


API_KEY = "legacy-golden-api-key"


def request_json(
    method: str,
    url: str,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, object]]:
    data = json.dumps(body or {}).encode("utf-8") if body is not None else None
    request_headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    request_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"error": raw}


def wait_ready(base_url: str) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            status, _ = request_json("GET", f"{base_url}/v1/models")
            if status == 200:
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("legacy server did not start")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        claude_root = tmp_path / "claude"
        codex_root = tmp_path / "codex"
        claude_user = claude_root / "users" / "golden"
        codex_user = codex_root / "users" / "golden"
        claude_user.mkdir(parents=True)
        codex_user.mkdir(parents=True)
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

        old_values = {
            "API_KEY": server.API_KEY,
            "DISABLE_LEGACY": server.DISABLE_LEGACY,
            "DEFAULT_CLAUDE_CONFIG_DIR": server.DEFAULT_CLAUDE_CONFIG_DIR,
            "USER_CLAUDE_CONFIG_ROOT": server.USER_CLAUDE_CONFIG_ROOT,
            "DEFAULT_CODEX_CONFIG_DIR": server.DEFAULT_CODEX_CONFIG_DIR,
            "USER_CODEX_CONFIG_ROOT": server.USER_CODEX_CONFIG_ROOT,
            "_exchange_code_for_token": server._exchange_code_for_token,
        }
        old_path = os.environ.get("PATH", "")
        server.API_KEY = API_KEY
        server.DISABLE_LEGACY = False
        server.DEFAULT_CLAUDE_CONFIG_DIR = str(claude_root)
        server.USER_CLAUDE_CONFIG_ROOT = str(claude_root / "users")
        server.DEFAULT_CODEX_CONFIG_DIR = str(codex_root)
        server.USER_CODEX_CONFIG_ROOT = str(codex_root / "users")
        server._exchange_code_for_token = lambda code, verifier, state: (  # type: ignore[assignment]
            200,
            {
                "access_token": "legacy-access-token",
                "refresh_token": "legacy-refresh-token",
                "expires_in": 60,
                "scope": server._SCOPE,
            },
        )
        os.environ["PATH"] = f"{tmp}:{old_path}"

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{httpd.server_port}"
            wait_ready(base_url)
            status, models = request_json("GET", f"{base_url}/v1/models")
            assert status == 200
            assert "data" in models

            claude_headers = {"X-Claude-Config-Dir": str(claude_user)}
            status, started = request_json("POST", f"{base_url}/admin/oauth/start", {}, claude_headers)
            assert status == 200, started
            state = re.search(r"[?&]state=([^&]+)", str(started["url"])).group(1)  # type: ignore[union-attr]
            status, completed = request_json(
                "POST",
                f"{base_url}/admin/oauth/complete",
                {"session_id": started["session_id"], "code": f"legacy-code#{state}"},
                claude_headers,
            )
            assert status == 200, completed
            assert completed["ok"] is True
            assert (claude_user / ".credentials.json").exists()

            codex_headers = {"X-Codex-Config-Dir": str(codex_user)}
            status, codex = request_json("POST", f"{base_url}/admin/codex/login/start", {}, codex_headers)
            assert status == 200, codex
            assert str(codex["url"]).startswith("https://auth.openai.com/codex/device")
            assert codex["user_code"] == "MZHT-0HT0G"
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            os.environ["PATH"] = old_path
            for name, value in old_values.items():
                setattr(server, name, value)
            shutil.rmtree(tmp_path, ignore_errors=True)
    print("PASS legacy_golden_test")


if __name__ == "__main__":
    main()
