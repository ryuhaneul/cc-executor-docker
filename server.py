#!/usr/bin/env python3
"""cc-executor — Claude Code and Codex CLI HTTP proxy (OpenAI-compatible API).

POST /v1/chat/completions  — OpenAI-compatible chat completions
GET  /v1/models            — Available models
GET  /health               — Health check

Admin (Bearer-protected):
  GET  /admin/status              — claude auth status
  GET  /admin/codex/status        — codex auth status
  POST /admin/codex/login/start   — begin Codex device login
  POST /admin/codex/login/complete — poll Codex device login completion
  POST /admin/codex/credentials   — import Codex auth fallback
  POST /admin/codex/logout        — remove local Codex auth state
  POST /admin/oauth/start         — begin OAuth 2.0 + PKCE flow
  POST /admin/oauth/complete      — exchange code, save .credentials.json
  POST /admin/credentials         — paste .credentials.json manually
  POST /admin/logout              — claude auth logout
  DELETE /admin/config-dir        — purge one per-user local config dir
  DELETE /admin/codex/config-dir  — purge one per-user Codex config dir
"""

import atexit
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
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
DEFAULT_CLAUDE_CONFIG_DIR = "/root/.claude"
USER_CLAUDE_CONFIG_ROOT = "/root/.claude/users"
DEFAULT_CODEX_CONFIG_DIR = "/root/.codex"
USER_CODEX_CONFIG_ROOT = "/root/.codex/users"
CODEX_DEFAULT_MODEL = os.environ.get("CODEX_DEFAULT_MODEL", "gpt-5.5")
CODEX_ALLOW_DANGER_FULL_ACCESS = (
    os.environ.get("CC_CODEX_ALLOW_DANGER_FULL_ACCESS", "false").lower()
    in {"1", "true", "yes", "on"}
)
CODEX_REQUIRE_USER_AUTH = (
    os.environ.get("CC_CODEX_REQUIRE_USER_AUTH", "false").lower()
    in {"1", "true", "yes", "on"}
)
CODEX_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

RETRY_DELAY_SECONDS = 5

# Claude model name mapping: OpenAI-style names → Claude Code CLI model names.
# `opus` aliases default to the 1M-context variant (`opus[1m]`, included on
# Max plan). `sonnet` aliases default to the standard 200K variant — request
# 1M Sonnet explicitly via `sonnet[1m]`. The `[1m]` suffix on any name maps
# to the 1M CLI model verbatim. Use `*200k` to force 200K explicitly.
CLAUDE_MODEL_MAP = {
    # 1M context — explicit suffix
    "opus[1m]": "opus[1m]",
    "sonnet[1m]": "sonnet[1m]",
    "claude-opus[1m]": "opus[1m]",
    "claude-sonnet[1m]": "sonnet[1m]",
    "cc-executor/opus[1m]": "opus[1m]",
    "cc-executor/sonnet[1m]": "sonnet[1m]",
    # opus bare aliases → 1M (default)
    "opus": "opus[1m]",
    "claude-opus": "opus[1m]",
    "claude-opus-4": "opus[1m]",
    "cc-executor/opus": "opus[1m]",
    # sonnet bare aliases → 200K (default)
    "sonnet": "sonnet",
    "claude-sonnet": "sonnet",
    "claude-sonnet-4": "sonnet",
    "cc-executor/sonnet": "sonnet",
    # haiku (1M not supported)
    "haiku": "haiku",
    "claude-haiku": "haiku",
    "claude-haiku-4": "haiku",
    "cc-executor/haiku": "haiku",
    # explicit 200K opt-out for opus/sonnet
    "opus200k": "opus",
    "sonnet200k": "sonnet",
    "cc-executor/opus200k": "opus",
    "cc-executor/sonnet200k": "sonnet",
}

CLAUDE_AVAILABLE_MODELS = [
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

CODEX_MODEL_MAP = {
    "codex/default": CODEX_DEFAULT_MODEL,
    "codex/gpt-5.5": "gpt-5.5",
}
CLAUDE_SUPPORTED_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
CODEX_SUPPORTED_EFFORTS = ["low", "medium", "high", "xhigh"]
_DEFAULT_CODEX_STATIC_MODEL_IDS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2",
]
CODEX_STATIC_MODEL_IDS = [
    m.strip()
    for m in os.environ.get("CODEX_STATIC_MODELS", ",".join(_DEFAULT_CODEX_STATIC_MODEL_IDS)).split(",")
    if m.strip()
]

CODEX_AVAILABLE_MODELS = [
    {"id": "codex/default", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "codex/gpt-5.5", "object": "model", "created": 1700000000, "owned_by": "openai"},
]


def _static_codex_models():
    out = []
    for slug in CODEX_STATIC_MODEL_IDS:
        out.append({
            "slug": slug,
            "supported_efforts": list(CODEX_SUPPORTED_EFFORTS),
            "default_effort": "high" if slug == "gpt-5.3-codex-spark" else "medium",
        })
    return out


def _load_codex_models(codex_config_dir):
    config_dir = _valid_codex_config_dir(codex_config_dir)
    if not config_dir:
        return _static_codex_models()
    path = Path(config_dir) / "models_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _static_codex_models()
    rows = data.get("models") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return _static_codex_models()
    out = []
    for row in rows:
        if not isinstance(row, dict) or row.get("visibility") != "list":
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        levels = row.get("reasoning_levels") or row.get("supported_reasoning_levels") or []
        efforts = []
        if isinstance(levels, list):
            for level in levels:
                if isinstance(level, dict):
                    effort = str(level.get("effort") or "").strip()
                    if effort:
                        efforts.append(effort)
        efforts = [e for e in dict.fromkeys(efforts) if e]
        if not efforts:
            efforts = list(CODEX_SUPPORTED_EFFORTS)
        default_effort = row.get("default_reasoning_level")
        default_effort = str(default_effort).strip() if default_effort else None
        out.append({
            "slug": slug,
            "supported_efforts": efforts,
            "default_effort": default_effort,
        })
    return out or _static_codex_models()


def _resolve_codex_model(model_name, codex_config_dir):
    name = str(model_name or "codex/default")
    if name in CODEX_MODEL_MAP:
        slug = CODEX_MODEL_MAP[name]
    elif name.startswith("codex/"):
        slug = name.split("/", 1)[1]
    else:
        slug = name
    by_slug = {m["slug"]: m for m in _load_codex_models(codex_config_dir)}
    meta = by_slug.get(slug)
    if not meta:
        raise ValueError(f"unsupported Codex model: {name}")
    return slug, meta


def _codex_model_entries(codex_config_dir=None, force_static=False):
    entries = []
    seen = set()
    models = _static_codex_models() if force_static else _load_codex_models(codex_config_dir)

    def add(entry):
        if entry["id"] in seen:
            return
        seen.add(entry["id"])
        entries.append(entry)

    for model in models:
        slug = model["slug"]
        add({
            "id": f"codex/{slug}",
            "object": "model",
            "created": 1700000000,
            "owned_by": "openai",
            "slug": slug,
            "supported_efforts": list(model.get("supported_efforts") or []),
            "default_effort": model.get("default_effort"),
        })
    for alias_id, slug in CODEX_MODEL_MAP.items():
        meta = next((m for m in models if m["slug"] == slug), None)
        if meta is None:
            meta = {
                "slug": slug,
                "supported_efforts": list(CODEX_SUPPORTED_EFFORTS),
                "default_effort": "medium",
            }
        add({
            "id": alias_id,
            "object": "model",
            "created": 1700000000,
            "owned_by": "openai",
            "slug": slug,
            "supported_efforts": list(meta.get("supported_efforts") or []),
            "default_effort": meta.get("default_effort"),
        })
    return entries


def _normalize_reasoning_effort(value):
    if value is None:
        return None
    effort = str(value).strip()
    return effort or None


def _valid_claude_config_dir(path):
    if not path:
        return DEFAULT_CLAUDE_CONFIG_DIR
    real = os.path.realpath(path)
    root = os.path.realpath(USER_CLAUDE_CONFIG_ROOT)
    if real == DEFAULT_CLAUDE_CONFIG_DIR or real.startswith(root + os.sep):
        return real
    return None


def _valid_codex_config_dir(path):
    if not path:
        return DEFAULT_CODEX_CONFIG_DIR
    path = os.path.normpath(path)
    if os.path.islink(path):
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(USER_CODEX_CONFIG_ROOT)
    default = os.path.realpath(DEFAULT_CODEX_CONFIG_DIR)
    if real == default:
        return real
    if real == root:
        return None
    if not real.startswith(root + os.sep):
        return None
    if os.path.dirname(real) != root:
        return None
    if os.path.basename(real) == "":
        return None
    return real


def _valid_delete_config_dir(path):
    if not path:
        return None
    # normpath drops trailing separators / "." segments so a directory
    # symlink given as "users/link/" is still caught by islink (which
    # returns False on a trailing slash). normpath does NOT resolve
    # symlinks, so the lstat-based islink check below stays meaningful.
    path = os.path.normpath(path)
    if os.path.islink(path):
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(USER_CLAUDE_CONFIG_ROOT)
    default = os.path.realpath(DEFAULT_CLAUDE_CONFIG_DIR)
    if real == root or real == default:
        return None
    if not real.startswith(root + os.sep):
        return None
    if os.path.dirname(real) != root:
        return None
    if os.path.basename(real) == "":
        return None
    return real


def _valid_codex_delete_config_dir(path):
    if not path:
        return None
    path = os.path.normpath(path)
    if os.path.islink(path):
        return None
    real = os.path.realpath(path)
    root = os.path.realpath(USER_CODEX_CONFIG_ROOT)
    default = os.path.realpath(DEFAULT_CODEX_CONFIG_DIR)
    if real == root or real == default:
        return None
    if not real.startswith(root + os.sep):
        return None
    if os.path.dirname(real) != root:
        return None
    if os.path.basename(real) == "":
        return None
    return real


def _claude_env(claude_config_dir=None):
    config_dir = _valid_claude_config_dir(claude_config_dir)
    if not config_dir:
        raise ValueError("invalid CLAUDE_CONFIG_DIR")
    os.makedirs(config_dir, exist_ok=True)
    env = os.environ.copy()
    for key in ("CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME", "OPENAI_API_KEY"):
        env.pop(key, None)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    return env, config_dir


def _codex_env(codex_config_dir=None):
    config_dir = _valid_codex_config_dir(codex_config_dir)
    if not config_dir:
        raise ValueError("invalid CODEX_HOME")
    os.makedirs(config_dir, exist_ok=True)
    env = {
        key: os.environ[key]
        for key in CODEX_ENV_PASSTHROUGH
        if key in os.environ
    }
    env["CODEX_HOME"] = config_dir
    codex_api_key = os.environ.get("CODEX_API_KEY")
    if codex_api_key and not CODEX_REQUIRE_USER_AUTH:
        env["CODEX_API_KEY"] = codex_api_key
        env["OPENAI_API_KEY"] = codex_api_key
    return env, config_dir


def _resolve_provider_and_model(body):
    raw_model = body.get("model")
    provider = body.get("provider")

    if provider is None:
        model_name = "sonnet" if raw_model is None else str(raw_model)
        model_is_codex = model_name.startswith("codex/")
        provider = "codex" if model_is_codex else "claude"
    else:
        provider = str(provider)
        if raw_model is None:
            model_name = "codex/default" if provider == "codex" else "sonnet"
        else:
            model_name = str(raw_model)
        model_is_codex = model_name.startswith("codex/")

    if provider not in {"claude", "codex"}:
        return None, None, {
            "message": "provider must be 'claude' or 'codex'",
            "type": "invalid_request_error",
        }

    if provider == "claude":
        if model_is_codex:
            return None, None, {
                "message": "codex/* models require provider=codex",
                "type": "invalid_request_error",
            }
        return provider, CLAUDE_MODEL_MAP.get(model_name, model_name), None

    if model_name in CLAUDE_MODEL_MAP and not model_is_codex:
        return None, None, {
            "message": "Claude model aliases require provider=claude",
            "type": "invalid_request_error",
        }
    return provider, model_name, None


def _cleanup_session_file(session_id, cwd=None, claude_config_dir=None):
    """Delete the jsonl that Claude Code wrote for this one-shot call.

    Claude Code persists every conversation (including --print runs) to
    ~/.claude/projects/<hyphenated-cwd>/<session_id>.jsonl. For an HTTP
    executor that's pure waste — the file is never resumed. We remove it
    immediately after the subprocess finishes so the volume does not
    grow unbounded across retries/fallbacks.

    `cwd` controls which project dir we look in (defaults to WORKDIR). For
    per-request workdirs we also try to rmdir the project dir if empty so
    ~/.claude/projects/ doesn't accumulate one entry per file-output call.
    """
    effective_cwd = cwd or WORKDIR
    project_name = effective_cwd.replace("/", "-")
    config_dir = _valid_claude_config_dir(claude_config_dir) or DEFAULT_CLAUDE_CONFIG_DIR
    project_path = Path(config_dir) / "projects" / project_name
    path = project_path / f"{session_id}.jsonl"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if cwd and cwd != WORKDIR:
        try:
            project_path.rmdir()
        except OSError:
            pass


def _run_claude(cli_model, prompt, system_prompt=None, max_turns=None, allowed_tools=None,
                cwd=None, dangerously_skip_permissions=False, timeout=None, add_dirs=None,
                claude_config_dir=None, effort=None):
    """Run claude CLI with a resolved CLI model name (e.g. 'opus[1m]' or 'opus')."""
    session_id = str(uuid.uuid4())
    cmd = ["claude", "--print", "--setting-sources", "", "--session-id", session_id]

    if cli_model:
        cmd += ["--model", cli_model]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if effort:
        cmd += ["--effort", effort]
    if allowed_tools:
        for tool in allowed_tools:
            cmd += ["--allowedTools", tool]
    if add_dirs:
        for d in add_dirs:
            cmd += ["--add-dir", d]
    if dangerously_skip_permissions:
        # Two distinct flags. `--allow-dangerously-skip-permissions` enables
        # the option (gated off by default in the CLI); the second flag then
        # actually applies it. Passing only the second is a no-op on builds
        # where the gate is enforced.
        cmd += ["--allow-dangerously-skip-permissions", "--dangerously-skip-permissions"]

    effective_cwd = cwd or WORKDIR
    effective_timeout = timeout if timeout is not None else TIMEOUT
    try:
        env, effective_config_dir = _claude_env(claude_config_dir)
    except ValueError as exc:
        return False, "", str(exc)

    print(f"[DEBUG] cmd={' '.join(cmd)} cwd={effective_cwd} timeout={effective_timeout}",
          file=sys.stderr)
    try:
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                cwd=effective_cwd,
                env=env,
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
        _cleanup_session_file(session_id, cwd=effective_cwd, claude_config_dir=effective_config_dir)


def _run_codex(cli_model, prompt, system_prompt=None, cwd=None, timeout=None, add_dirs=None,
               sandbox="read-only", codex_config_dir=None, effort=None):
    """Run codex CLI with stdin prompt and return its last-message output."""
    effective_cwd = cwd or WORKDIR
    effective_timeout = timeout if timeout is not None else TIMEOUT
    if system_prompt:
        prompt = (
            "SYSTEM INSTRUCTIONS:\n"
            f"{system_prompt}\n\n"
            "CONVERSATION PROMPT:\n"
            f"{prompt}"
        )

    try:
        env, _ = _codex_env(codex_config_dir)
    except ValueError as exc:
        return False, "", str(exc)

    tmp_path = None
    try:
        tmp = tempfile.NamedTemporaryFile(prefix="codex-last-", suffix=".txt", delete=False)
        tmp_path = tmp.name
        tmp.close()
        cmd = [
            "codex",
            "--ask-for-approval", "never",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--output-last-message", tmp_path,
            "-C", effective_cwd,
            "-m", cli_model,
            "--color", "never",
        ]
        if sandbox == "danger-full-access":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            cmd += ["--sandbox", sandbox]
        if add_dirs:
            for d in add_dirs:
                cmd += ["--add-dir", d]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        cmd.append("-")

        print(f"[DEBUG] cmd={' '.join(cmd)} cwd={effective_cwd} timeout={effective_timeout}",
              file=sys.stderr)
        result = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            cwd=effective_cwd,
            env=env,
        )
        try:
            with open(tmp_path, encoding="utf-8") as fh:
                last_message = fh.read().strip()
        except OSError:
            last_message = ""

        if result.returncode == 0:
            return True, last_message or result.stdout.strip(), None

        print(f"[ERROR] codex returncode={result.returncode}", file=sys.stderr)
        print(f"[ERROR] codex stderr={result.stderr.strip()[:500]}", file=sys.stderr)
        print(f"[ERROR] codex stdout={result.stdout.strip()[:200]}", file=sys.stderr)
        err = result.stderr.strip() or result.stdout.strip() or "codex CLI failed"
        return False, last_message or result.stdout.strip(), err[:2000]
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except FileNotFoundError:
        return False, "", "codex CLI not found"
    except Exception as exc:
        return False, "", str(exc)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _run_claude_with_retry(model, prompt, system_prompt=None, max_turns=None, allowed_tools=None,
                            cwd=None, dangerously_skip_permissions=False, timeout=None,
                            add_dirs=None, claude_config_dir=None, effort=None):
    """Resolve model alias, run once, retry once after a short delay, and
    fall back from 1M (`foo[1m]`) to the 200K variant (`foo`) if still failing.

    Returns (ok, output, error, fallback_info) where fallback_info is either
    None or a dict {"from": "<1m model>", "to": "<200k model>"}.
    """
    resolved = CLAUDE_MODEL_MAP.get(model, model)
    kwargs = dict(
        system_prompt=system_prompt,
        max_turns=max_turns,
        allowed_tools=allowed_tools,
        cwd=cwd,
        dangerously_skip_permissions=dangerously_skip_permissions,
        timeout=timeout,
        add_dirs=add_dirs,
        claude_config_dir=claude_config_dir,
        effort=effort,
    )

    ok, output, error = _run_claude(resolved, prompt, **kwargs)
    if ok:
        return ok, output, error, None

    print(
        f"[RETRY] model={resolved} failed, retrying in {RETRY_DELAY_SECONDS}s: {(error or '')[:200]}",
        file=sys.stderr,
    )
    time.sleep(RETRY_DELAY_SECONDS)
    ok, output, error = _run_claude(resolved, prompt, **kwargs)
    if ok:
        return ok, output, error, None

    if resolved.endswith("[1m]"):
        fallback = resolved[:-4]
        print(
            f"[FALLBACK] {resolved} → {fallback} after retry failure: {(error or '')[:200]}",
            file=sys.stderr,
        )
        ok, output, error = _run_claude(fallback, prompt, **kwargs)
        fallback_info = {"from": resolved, "to": fallback} if ok else None
        return ok, output, error, fallback_info

    return ok, output, error, None


def _collect_files(request_dir):
    """Walk request_dir and return {relative_path: content_str}.

    UTF-8 text is returned verbatim; binary or undecodable files are returned
    as `data:base64,<...>` so the JSON envelope can carry anything the model
    wrote (images, archives, etc.).
    """
    out = {}
    for root, _, names in os.walk(request_dir):
        for name in sorted(names):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, request_dir)
            try:
                with open(full, encoding="utf-8") as fh:
                    out[rel] = fh.read()
            except UnicodeDecodeError:
                try:
                    with open(full, "rb") as fh:
                        out[rel] = "data:base64," + base64.b64encode(fh.read()).decode("ascii")
                except OSError as exc:
                    out[rel] = f"[read error: {exc}]"
            except OSError as exc:
                out[rel] = f"[read error: {exc}]"
    return out


_FILES_MODE_INSTRUCTION = (
    "OUTPUT MODE: file-output. Write all deliverables as files in the current "
    "working directory using the Write tool — choose clear filenames (e.g. "
    "`subtitle.srt`, `songs.json`). Your final text response must be a brief "
    "summary listing which files you produced and what each contains; do NOT "
    "inline the deliverable content in the text response."
)


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

_CODEX_LOGIN_SESSIONS = {}
_CODEX_LOGIN_SESSIONS_LOCK = threading.Lock()
_CODEX_LOGIN_TTL = 900
_CODEX_LOGIN_URL_RE = re.compile(r"https://auth\.openai\.com/codex/device[^\s]*")
_CODEX_USER_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{5}\b")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _strip_ansi(text):
    return _ANSI_RE.sub("", text or "")


def _parse_codex_device_login_output(text):
    # Fixture shape observed from Codex 0.133.0: ANSI text containing
    # https://auth.openai.com/codex/device and MZHT-0HT0G.
    clean = _strip_ansi(text)
    url_match = _CODEX_LOGIN_URL_RE.search(clean)
    url = url_match.group(0) if url_match else None
    m = _CODEX_USER_CODE_RE.search(clean)
    user_code = m.group(0) if m else None
    return url, user_code


def _terminate_proc(proc, timeout=1):
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _drain_codex_login_output(session_id):
    with _CODEX_LOGIN_SESSIONS_LOCK:
        sess = _CODEX_LOGIN_SESSIONS.get(session_id)
    if not sess:
        return
    proc = sess["proc"]
    try:
        for line in proc.stdout or []:
            clean = _strip_ansi(line)
            with sess["lock"]:
                sess["stdout_buf"] += clean
                url, user_code = _parse_codex_device_login_output(sess["stdout_buf"])
                if url:
                    sess["url"] = url
                if user_code:
                    sess["user_code"] = user_code
                if sess.get("status") == "starting" and url and user_code:
                    sess["status"] = "pending"
    except Exception:
        with sess["lock"]:
            sess["status"] = "error"
    finally:
        rc = proc.poll()
        with sess["lock"]:
            if sess.get("status") not in {"error", "cancelled"} and rc is not None:
                sess["status"] = "exited"


def _get_codex_login_session(session_id):
    with _CODEX_LOGIN_SESSIONS_LOCK:
        return _CODEX_LOGIN_SESSIONS.get(session_id)


def _pop_codex_login_session(session_id, kill_proc=True):
    with _CODEX_LOGIN_SESSIONS_LOCK:
        sess = _CODEX_LOGIN_SESSIONS.pop(session_id, None)
    if sess and kill_proc:
        with sess["lock"]:
            sess["status"] = "cancelled"
        _terminate_proc(sess.get("proc"))
    return sess


def _cancel_codex_login_sessions_for_home(codex_home):
    with _CODEX_LOGIN_SESSIONS_LOCK:
        session_ids = [
            sid for sid, sess in _CODEX_LOGIN_SESSIONS.items()
            if sess.get("codex_home") == codex_home
        ]
    for sid in session_ids:
        _pop_codex_login_session(sid, kill_proc=True)


def _reap_codex_login_sessions():
    now = time.time()
    with _CODEX_LOGIN_SESSIONS_LOCK:
        expired = [
            sid for sid, sess in _CODEX_LOGIN_SESSIONS.items()
            if now - sess["created_at"] > _CODEX_LOGIN_TTL
        ]
    for sid in expired:
        _pop_codex_login_session(sid, kill_proc=True)


def _kill_all_codex_login_sessions():
    with _CODEX_LOGIN_SESSIONS_LOCK:
        session_ids = list(_CODEX_LOGIN_SESSIONS.keys())
    for sid in session_ids:
        _pop_codex_login_session(sid, kill_proc=True)


def _codex_login_reaper_loop():
    while True:
        time.sleep(30)
        try:
            _reap_codex_login_sessions()
        except Exception as exc:
            print(f"[WARN] codex login reaper failed: {exc}", file=sys.stderr)


atexit.register(_kill_all_codex_login_sessions)


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


def _write_credentials(creds: dict, claude_config_dir=None) -> tuple[bool, str]:
    """Atomically write .credentials.json in the selected Claude config dir."""
    try:
        _, claude_dir = _claude_env(claude_config_dir)
    except ValueError as exc:
        return False, str(exc)
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


def _claude_auth_status(claude_config_dir=None):
    """Return dict parsed from `claude auth status` JSON, or {}."""
    status = {}
    try:
        env, _ = _claude_env(claude_config_dir)
        out = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10, env=env,
        )
        m = re.search(r"\{.*?\}", out.stdout, re.DOTALL)
        if m:
            status = json.loads(m.group(0))
    except Exception:
        pass
    try:
        _, config_dir = _claude_env(claude_config_dir)
        with open(os.path.join(config_dir, ".credentials.json"), "r", encoding="utf-8") as fh:
            creds = json.load(fh)
        oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
        expires_at = oauth.get("expiresAt") if isinstance(oauth, dict) else None
        expires_at = int(expires_at) if expires_at is not None else None
    except Exception:
        expires_at = None
    status["expiresAt"] = expires_at
    status["expired"] = (int(time.time() * 1000) > expires_at) if expires_at is not None else None
    return status


def _decode_jwt_payload(token):
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8")
        data = json.loads(decoded)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _codex_auth_expiry(config_dir):
    try:
        with open(os.path.join(config_dir, "auth.json"), "r", encoding="utf-8") as fh:
            auth = json.load(fh)
        tokens = auth.get("tokens") if isinstance(auth, dict) else None
        if not isinstance(tokens, dict):
            return None, None
        payload = _decode_jwt_payload(tokens.get("access_token") or tokens.get("id_token"))
        exp = payload.get("exp") if isinstance(payload, dict) else None
        if exp is None:
            return None, None
        expires_at = int(exp) * 1000
        return expires_at, int(time.time() * 1000) > expires_at
    except Exception:
        return None, None


def _codex_cli_available():
    if shutil.which("codex"):
        return True
    try:
        out = subprocess.run(["codex", "--version"], capture_output=True, text=True, timeout=5)
        return out.returncode == 0
    except Exception:
        return False


def _codex_login_status(codex_config_dir=None, timeout=10):
    try:
        env, _ = _codex_env(codex_config_dir)
        out = subprocess.run(
            ["codex", "login", "status"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        return out.returncode == 0
    except Exception:
        return False


def _codex_auth_json_logged_in(config_dir, timeout=10):
    auth_json = os.path.join(config_dir, "auth.json")
    return os.path.exists(auth_json) and _codex_login_status(config_dir, timeout=timeout)


def _write_codex_auth_json(config_dir, auth_json):
    target = os.path.join(config_dir, "auth.json")
    tmp = os.path.join(config_dir, f".auth.json.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = None
    try:
        os.makedirs(config_dir, exist_ok=True)
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = None
            json.dump(auth_json, fh, ensure_ascii=False)
        os.replace(tmp, target)
        return True
    except Exception:
        if fd is not None:
            try:
                os.close(fd)
            except Exception:
                pass
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


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

    def _claude_config_dir_from_request(self, body=None):
        requested = self.headers.get("X-Claude-Config-Dir", "")
        if not requested and isinstance(body, dict):
            requested = body.get("claude_config_dir") or ""
        config_dir = _valid_claude_config_dir(requested)
        if not config_dir:
            self._json_response(400, {"error": {"message": "invalid CLAUDE_CONFIG_DIR"}})
            return None
        return config_dir

    def _codex_config_dir_from_request(self, body=None):
        requested = self.headers.get("X-Codex-Config-Dir", "")
        if not requested and isinstance(body, dict):
            requested = body.get("codex_config_dir") or ""
        config_dir = _valid_codex_config_dir(requested)
        if not config_dir:
            self._json_response(400, {"error": {"message": "invalid CODEX_HOME"}})
            return None
        if CODEX_REQUIRE_USER_AUTH and config_dir == os.path.realpath(DEFAULT_CODEX_CONFIG_DIR):
            self._json_response(400, {"error": {"message": "CODEX_USER_CONFIG_DIR_REQUIRED"}})
            return None
        return config_dir

    # ── GET ──

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"status": "ok"})
        elif self.path == "/v1/models":
            if not self._check_auth():
                self._json_response(401, {"error": {"message": "Invalid API key", "type": "authentication_error"}})
                return
            requested_codex_dir = self.headers.get("X-Codex-Config-Dir", "")
            if requested_codex_dir:
                codex_config_dir = _valid_codex_config_dir(requested_codex_dir)
                if not codex_config_dir:
                    self._json_response(400, {"error": {"message": "invalid CODEX_HOME"}})
                    return
                codex_models = _codex_model_entries(codex_config_dir)
            else:
                codex_models = _codex_model_entries(force_static=True)
            claude_models = [
                {
                    **m,
                    "supported_efforts": list(CLAUDE_SUPPORTED_EFFORTS),
                    "default_effort": None,
                }
                for m in CLAUDE_AVAILABLE_MODELS
            ]
            self._json_response(200, {
                "object": "list",
                "data": claude_models + codex_models,
            })
        elif self.path == "/admin/status":
            if not self._check_auth():
                self._json_response(401, {"error": {"message": "Invalid API key"}})
                return
            config_dir = self._claude_config_dir_from_request()
            if not config_dir:
                return
            status = _claude_auth_status(config_dir)
            self._json_response(200, {
                "loggedIn": bool(status.get("loggedIn")),
                "authMethod": status.get("authMethod"),
                "apiProvider": status.get("apiProvider"),
                "expiresAt": status.get("expiresAt"),
                "expired": status.get("expired"),
            })
        elif self.path == "/admin/codex/status":
            if not self._check_auth():
                self._json_response(401, {"error": {"message": "Invalid API key"}})
                return
            config_dir = self._codex_config_dir_from_request()
            if not config_dir:
                return
            cli_available = _codex_cli_available()
            has_api_key = bool(os.environ.get("CODEX_API_KEY")) and not CODEX_REQUIRE_USER_AUTH
            auth_method = None
            logged_in = False
            if has_api_key:
                auth_method = "api_key"
                logged_in = True
            elif _codex_auth_json_logged_in(config_dir):
                auth_method = "auth_json"
                logged_in = True
            expires_at, expired = _codex_auth_expiry(config_dir)
            self._json_response(200, {
                "loggedIn": logged_in,
                "authMethod": auth_method,
                "codexHome": config_dir,
                "cliAvailable": cli_available,
                "expiresAt": expires_at,
                "expired": expired,
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
        elif self.path == "/admin/codex/login/start":
            self._handle_codex_login_start()
        elif self.path == "/admin/codex/login/complete":
            self._handle_codex_login_complete()
        elif self.path == "/admin/codex/credentials":
            self._handle_codex_credentials()
        elif self.path == "/admin/codex/logout":
            self._handle_codex_logout()
        else:
            self.send_error(404)

    # ── DELETE ──

    def do_DELETE(self):
        if self.path == "/admin/config-dir":
            self._handle_delete_config_dir()
        elif self.path == "/admin/codex/config-dir":
            self._handle_codex_delete_config_dir()
        else:
            self.send_error(404)

    # ── /admin/config-dir ──

    def _handle_delete_config_dir(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        requested = self.headers.get("X-Claude-Config-Dir", "")
        config_dir = _valid_delete_config_dir(requested)
        if config_dir is None:
            self._json_response(400, {"error": {"message": "invalid config dir for delete"}})
            return
        if not os.path.exists(config_dir):
            self._json_response(200, {"ok": True, "existed": False})
            return
        try:
            shutil.rmtree(config_dir)
            self._json_response(200, {"ok": True, "existed": True})
        except Exception as exc:
            self._json_response(500, {"error": {"message": str(exc)}})

    # ── /admin/codex/config-dir ──

    def _handle_codex_delete_config_dir(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        requested = self.headers.get("X-Codex-Config-Dir", "")
        if CODEX_REQUIRE_USER_AUTH:
            selected = _valid_codex_config_dir(requested)
            if selected == os.path.realpath(DEFAULT_CODEX_CONFIG_DIR):
                self._json_response(400, {"error": {"message": "CODEX_USER_CONFIG_DIR_REQUIRED"}})
                return
        config_dir = _valid_codex_delete_config_dir(requested)
        if config_dir is None:
            self._json_response(400, {"error": {"message": "invalid codex config dir for delete"}})
            return
        _cancel_codex_login_sessions_for_home(config_dir)
        if not os.path.exists(config_dir):
            self._json_response(200, {"ok": True, "existed": False})
            return
        try:
            shutil.rmtree(config_dir)
            self._json_response(200, {"ok": True, "existed": True})
        except Exception as exc:
            self._json_response(500, {"error": {"message": str(exc)}})

    # ── /admin/codex/login/* ──

    def _handle_codex_login_start(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        config_dir = self._codex_config_dir_from_request()
        if not config_dir:
            return
        _reap_codex_login_sessions()
        try:
            env, config_dir = _codex_env(config_dir)
            proc = subprocess.Popen(
                ["codex", "login", "--device-auth"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError:
            self._json_response(500, {"error": {"message": "codex CLI not found"}})
            return
        except Exception as exc:
            self._json_response(500, {"error": {"message": str(exc)}})
            return

        session_id = str(uuid.uuid4())
        sess = {
            "proc": proc,
            "codex_home": config_dir,
            "created_at": time.time(),
            "status": "starting",
            "stdout_buf": "",
            "url": None,
            "user_code": None,
            "lock": threading.Lock(),
        }
        with _CODEX_LOGIN_SESSIONS_LOCK:
            _CODEX_LOGIN_SESSIONS[session_id] = sess
        threading.Thread(
            target=_drain_codex_login_output,
            args=(session_id,),
            daemon=True,
        ).start()

        deadline = time.time() + 15
        while time.time() < deadline:
            with sess["lock"]:
                url = sess.get("url")
                user_code = sess.get("user_code")
                status = sess.get("status")
            if url and user_code:
                self._json_response(200, {
                    "session_id": session_id,
                    "url": url,
                    "user_code": user_code,
                })
                return
            if proc.poll() is not None and status != "pending":
                break
            time.sleep(0.1)

        _pop_codex_login_session(session_id, kill_proc=True)
        self._json_response(500, {"error": {"message": "codex login did not produce a device code"}})

    def _handle_codex_login_complete(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        body = self._read_json() or {}
        config_dir = self._codex_config_dir_from_request(body)
        if not config_dir:
            return
        sid = body.get("session_id")
        if not sid:
            self._json_response(422, {"error": {"message": "session_id is required"}})
            return
        _reap_codex_login_sessions()
        sess = _get_codex_login_session(sid)
        if not sess:
            self._json_response(404, {"error": {"message": "login session expired or unknown"}})
            return
        if sess.get("codex_home") != config_dir:
            self._json_response(403, {"error": {"message": "CODEX_HOME mismatch for login session"}})
            return

        deadline = time.time() + 3
        while time.time() < deadline:
            remaining = max(0.2, min(1.0, deadline - time.time()))
            if _codex_login_status(config_dir, timeout=remaining):
                _pop_codex_login_session(sid, kill_proc=True)
                self._json_response(200, {"ok": True, "loggedIn": True})
                return
            if sess["proc"].poll() is not None:
                break
            time.sleep(0.25)

        if sess["proc"].poll() is not None:
            _pop_codex_login_session(sid, kill_proc=False)
            self._json_response(400, {"ok": False, "error": {"message": "codex login did not complete"}})
            return
        self._json_response(202, {"ok": False, "status": "pending"})

    # ── /admin/codex/credentials ──

    def _handle_codex_credentials(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        body = self._read_json() or {}
        config_dir = self._codex_config_dir_from_request(body)
        if not config_dir:
            return

        access_token = body.get("access_token")
        auth_json = body.get("auth_json")
        if access_token:
            try:
                env, _ = _codex_env(config_dir)
                out = subprocess.run(
                    ["codex", "login", "--with-access-token"],
                    input=str(access_token) + "\n",
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                )
            except FileNotFoundError:
                self._json_response(500, {"error": {"message": "codex CLI not found"}})
                return
            except Exception:
                self._json_response(500, {"error": {"message": "codex credential import failed"}})
                return
            if out.returncode != 0:
                self._json_response(400, {"ok": False, "error": {"message": "codex credential import failed"}})
                return
            self._json_response(200, {
                "ok": True,
                "loggedIn": _codex_login_status(config_dir),
            })
            return

        if isinstance(auth_json, dict):
            if not _write_codex_auth_json(config_dir, auth_json):
                self._json_response(500, {"error": {"message": "codex auth_json write failed"}})
                return
            self._json_response(200, {
                "ok": True,
                "loggedIn": _codex_login_status(config_dir),
            })
            return

        self._json_response(422, {"error": {"message": "access_token or auth_json is required"}})

    # ── /admin/codex/logout ──

    def _handle_codex_logout(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        config_dir = self._codex_config_dir_from_request()
        if not config_dir:
            return
        _cancel_codex_login_sessions_for_home(config_dir)
        try:
            env, _ = _codex_env(config_dir)
            subprocess.run(
                ["codex", "logout"],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
        except Exception:
            pass
        # Observed Codex local auth state: auth.json plus log/codex-login.log.
        for name in ("auth.json", os.path.join("log", "codex-login.log")):
            try:
                os.unlink(os.path.join(config_dir, name))
            except FileNotFoundError:
                pass
            except Exception as exc:
                self._json_response(500, {"error": {"message": str(exc)}})
                return
        self._json_response(200, {"ok": True, "loggedIn": False})

    # ── /admin/credentials ──
    # Manual fallback: paste the contents of an existing .credentials.json
    # (e.g. produced by `claude auth login` on another machine). Prefer
    # /admin/oauth/* — this exists for recovery scenarios.
    def _handle_set_credentials(self):
        if not self._check_auth():
            self._json_response(401, {"error": {"message": "Invalid API key"}})
            return
        body = self._read_json() or {}
        config_dir = self._claude_config_dir_from_request(body)
        if not config_dir:
            return
        creds = body.get("credentials")
        if not isinstance(creds, dict):
            self._json_response(422, {"error": {"message": "credentials must be an object"}})
            return
        oauth = creds.get("claudeAiOauth")
        if not isinstance(oauth, dict) or not oauth.get("accessToken"):
            self._json_response(422, {"error": {"message": "credentials.claudeAiOauth.accessToken missing"}})
            return
        ok, err = _write_credentials(creds, config_dir)
        if not ok:
            self._json_response(500, {"error": {"message": f"write failed: {err}"}})
            return
        status = _claude_auth_status(config_dir)
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
        config_dir = self._claude_config_dir_from_request()
        if not config_dir:
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
        config_dir = self._claude_config_dir_from_request(body)
        if not config_dir:
            return
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
        ok, err = _write_credentials(creds, config_dir)
        if not ok:
            self._json_response(500, {"ok": False, "error": {"message": f"write failed: {err}"}})
            return

        final = _claude_auth_status(config_dir)
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
            config_dir = self._claude_config_dir_from_request()
            if not config_dir:
                return
            env, _ = _claude_env(config_dir)
            out = subprocess.run(
                ["claude", "auth", "logout"],
                capture_output=True, text=True, timeout=10, env=env,
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

        provider, cli_model, model_error = _resolve_provider_and_model(body)
        if model_error:
            self._json_response(400, {"error": model_error})
            return

        messages = body.get("messages", [])
        model = body.get("model")
        if model is None:
            model = "codex/default" if provider == "codex" else "sonnet"
        output_files_mode = bool(body.get("output_files", False))
        max_turns = body.get("max_turns")
        request_timeout = body.get("timeout")
        body_allowed_tools = body.get("allowed_tools")
        body_cwd = body.get("cwd")
        body_add_dirs = body.get("add_dirs") or []
        reasoning_effort = _normalize_reasoning_effort(body.get("reasoning_effort"))

        if not messages:
            self._json_response(400, {"error": {"message": "messages is required", "type": "invalid_request_error"}})
            return

        if provider == "codex":
            unsupported = []
            if body_allowed_tools is not None:
                unsupported.append("allowed_tools")
            if max_turns is not None:
                unsupported.append("max_turns")
            if unsupported:
                self._json_response(400, {
                    "error": {
                        "message": f"unsupported field for provider=codex: {', '.join(unsupported)}",
                        "type": "unsupported_field",
                    }
                })
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

        if provider == "codex":
            self._handle_codex_chat(
                body=body,
                model=model,
                cli_model=cli_model,
                prompt=prompt,
                system_prompt=system_prompt,
                output_files_mode=output_files_mode,
                request_timeout=request_timeout,
                body_cwd=body_cwd,
                body_add_dirs=body_add_dirs,
            )
            return

        config_dir = self._claude_config_dir_from_request(body)
        if not config_dir:
            return
        if reasoning_effort and reasoning_effort not in CLAUDE_SUPPORTED_EFFORTS:
            self._json_response(400, {
                "error": {
                    "message": f"unsupported reasoning_effort for provider=claude: {reasoning_effort}",
                    "type": "invalid_request_error",
                }
            })
            return

        # Three modes:
        #   1. text-only (default)            — no tools, OpenAI-compatible.
        #   2. file-output (output_files)     — per-request scratch dir, files
        #                                       returned in JSON, scratch wiped.
        #   3. direct-filesystem (cwd/add_dirs) — caller-managed paths (e.g. a
        #                                         bind-mounted /storage/jobs/<id>);
        #                                         no scratch, no JSON files, no
        #                                         cleanup. Caller owns I/O.
        request_dir = None
        cwd = None
        add_dirs = body_add_dirs or None
        allowed_tools = body_allowed_tools
        dangerously_skip = False

        direct_fs_mode = (not output_files_mode) and bool(
            body_cwd or body_add_dirs or body_allowed_tools
        )

        if output_files_mode:
            request_id = uuid.uuid4().hex[:12]
            request_dir = os.path.join(WORKDIR, f"req-{request_id}")
            try:
                os.makedirs(request_dir, exist_ok=True)
            except OSError as exc:
                self._json_response(500, {"error": {"message": f"workdir create failed: {exc}", "type": "server_error"}})
                return
            cwd = request_dir
            # Claude CLI refuses --dangerously-skip-permissions when running as
            # root (security check). The container runs as root for OAuth token
            # access at /root/.claude. Rely on --allowedTools auto-approval.
            dangerously_skip = False
            if not allowed_tools:
                allowed_tools = ["Write", "Read", "Edit"]
            if max_turns is None:
                max_turns = 50
            system_prompt = (
                _FILES_MODE_INSTRUCTION + ("\n\n" + system_prompt if system_prompt else "")
            )
            # body_add_dirs still respected so the agent can read auxiliary
            # mounted paths even while writing deliverables to the scratch dir.
        elif direct_fs_mode:
            cwd = body_cwd  # may be None — _run_claude falls back to WORKDIR
            # Same root-vs-skip-permissions guard as above.
            dangerously_skip = False
            if not allowed_tools:
                allowed_tools = ["Read", "Write", "Edit"]
            if max_turns is None:
                max_turns = 50

        try:
            ok, output, error, fallback_info = _run_claude_with_retry(
                model=model,
                prompt=prompt,
                system_prompt=system_prompt,
                max_turns=max_turns,
                allowed_tools=allowed_tools,
                cwd=cwd,
                dangerously_skip_permissions=dangerously_skip,
                timeout=request_timeout,
                add_dirs=add_dirs,
                claude_config_dir=config_dir,
                effort=reasoning_effort,
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

            files_payload = _collect_files(request_dir) if output_files_mode else None

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
            if files_payload is not None:
                response["files"] = files_payload
            self._json_response(200, response)
        finally:
            if request_dir:
                try:
                    shutil.rmtree(request_dir)
                except OSError as exc:
                    print(f"[WARN] failed to clean request dir {request_dir}: {exc}",
                          file=sys.stderr)

    def _handle_codex_chat(self, body, model, cli_model, prompt, system_prompt,
                           output_files_mode, request_timeout, body_cwd, body_add_dirs):
        config_dir = self._codex_config_dir_from_request(body)
        if not config_dir:
            return
        if CODEX_REQUIRE_USER_AUTH and not _codex_auth_json_logged_in(config_dir, timeout=3):
            self._json_response(403, {
                "error": {
                    "message": "CODEX_AUTH_REQUIRED",
                    "type": "authentication_error",
                    "code": "CODEX_AUTH_REQUIRED",
                }
            })
            return
        try:
            cli_model, model_meta = _resolve_codex_model(model, config_dir)
        except ValueError as exc:
            self._json_response(400, {
                "error": {
                    "message": str(exc),
                    "type": "invalid_request_error",
                }
            })
            return
        reasoning_effort = _normalize_reasoning_effort(body.get("reasoning_effort"))
        if reasoning_effort and reasoning_effort not in (model_meta.get("supported_efforts") or []):
            self._json_response(400, {
                "error": {
                    "message": f"unsupported reasoning_effort for model {model}: {reasoning_effort}",
                    "type": "invalid_request_error",
                }
            })
            return

        requested_sandbox = body.get("codex_sandbox")
        if requested_sandbox is not None:
            requested_sandbox = str(requested_sandbox)
            if requested_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
                self._json_response(400, {
                    "error": {
                        "message": "codex_sandbox must be read-only, workspace-write, or danger-full-access",
                        "type": "invalid_request_error",
                    }
                })
                return
            if requested_sandbox == "danger-full-access" and not CODEX_ALLOW_DANGER_FULL_ACCESS:
                self._json_response(400, {
                    "error": {
                        "message": "codex_sandbox=danger-full-access is disabled",
                        "type": "invalid_request_error",
                    }
                })
                return

        request_dir = None
        cwd = WORKDIR
        add_dirs = body_add_dirs or None
        sandbox = requested_sandbox or "read-only"

        if output_files_mode:
            request_id = uuid.uuid4().hex[:12]
            request_dir = os.path.join(WORKDIR, f"req-{request_id}")
            try:
                os.makedirs(request_dir, exist_ok=True)
            except OSError as exc:
                self._json_response(500, {"error": {"message": f"workdir create failed: {exc}", "type": "server_error"}})
                return
            cwd = request_dir
            sandbox = requested_sandbox or "workspace-write"
            system_prompt = (
                _FILES_MODE_INSTRUCTION + ("\n\n" + system_prompt if system_prompt else "")
            )
        elif body_cwd or body_add_dirs:
            cwd = body_cwd or WORKDIR
            sandbox = requested_sandbox or "workspace-write"

        try:
            ok, output, error = _run_codex(
                cli_model=cli_model,
                prompt=prompt,
                system_prompt=system_prompt,
                cwd=cwd,
                timeout=request_timeout,
                add_dirs=add_dirs,
                sandbox=sandbox,
                codex_config_dir=config_dir,
                effort=reasoning_effort,
            )

            if not ok:
                err_msg = error or "Generation failed"
                print(f"[ERROR] codex model={model} error={err_msg}", file=sys.stderr)
                if output:
                    print(f"[ERROR] codex stdout={output[:500]}", file=sys.stderr)
                self._json_response(500, {
                    "error": {"message": err_msg, "type": "server_error"}
                })
                return

            files_payload = _collect_files(request_dir) if output_files_mode else None

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
            if files_payload is not None:
                response["files"] = files_payload
            self._json_response(200, response)
        finally:
            if request_dir:
                try:
                    shutil.rmtree(request_dir)
                except OSError as exc:
                    print(f"[WARN] failed to clean request dir {request_dir}: {exc}",
                          file=sys.stderr)

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
    reaper = threading.Thread(target=_codex_login_reaper_loop, daemon=True)
    reaper.start()
    print(f"cc-executor listening on {HOST}:{PORT} (threaded)", file=sys.stderr)
    print(f"  POST /v1/chat/completions", file=sys.stderr)
    print(f"  GET  /v1/models", file=sys.stderr)
    print(f"  GET  /health", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _kill_all_codex_login_sessions()
    server.server_close()


if __name__ == "__main__":
    main()
