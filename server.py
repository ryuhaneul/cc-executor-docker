#!/usr/bin/env python3
"""cc-executor — Claude Code CLI HTTP proxy (OpenAI-compatible API).

POST /v1/chat/completions  — OpenAI-compatible chat completions
GET  /v1/models            — Available models
GET  /health               — Health check
"""

import json
import os
import subprocess
import sys
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler

HOST = os.environ.get("CC_EXECUTOR_HOST", "0.0.0.0")
PORT = int(os.environ.get("CC_EXECUTOR_PORT", "9100"))
API_KEY = os.environ.get("CC_API_KEY", "")
TIMEOUT = int(os.environ.get("CC_TIMEOUT", "300"))
WORKDIR = "/app/workdir"

# Model name mapping: OpenAI-style names → Claude Code CLI model names
MODEL_MAP = {
    "opus": "claude-opus-4-7",
    "sonnet": "sonnet",
    "haiku": "haiku",
    "claude-opus": "claude-opus-4-7",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
    "claude-opus-4": "claude-opus-4-7",
    "claude-sonnet-4": "sonnet",
    "claude-haiku-4": "haiku",
    "cc-executor/opus": "claude-opus-4-7",
    "cc-executor/sonnet": "sonnet",
    "cc-executor/haiku": "haiku",
}

AVAILABLE_MODELS = [
    {"id": "opus", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "sonnet", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "haiku", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/opus", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/sonnet", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
    {"id": "cc-executor/haiku", "object": "model", "created": 1700000000, "owned_by": "anthropic"},
]


def _run_claude(model, prompt, system_prompt=None, max_turns=None, allowed_tools=None):
    """Run claude CLI and return (ok, output, error)."""
    cmd = ["claude", "--print", "--setting-sources", ""]

    if model:
        cmd += ["--model", MODEL_MAP.get(model, model)]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if allowed_tools:
        for tool in allowed_tools:
            cmd += ["--allowedTools", tool]

    print(f"[DEBUG] cmd={' '.join(cmd)}", file=sys.stderr)
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
        else:
            self.send_error(404)

    # ── POST ──

    def do_POST(self):
        if self.path == "/v1/chat/completions":
            self._handle_chat_completions()
        else:
            self.send_error(404)

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

        ok, output, error = _run_claude(
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

    server = HTTPServer((HOST, PORT), Handler)
    print(f"cc-executor listening on {HOST}:{PORT}", file=sys.stderr)
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
