# cc-executor-docker

OpenAI-compatible API proxy for Claude Code CLI. Wraps `claude --print` in a Docker container with Bearer token authentication.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/v1/chat/completions` | Bearer | OpenAI-compatible chat completions |
| GET | `/v1/models` | Bearer | List available models |
| GET | `/health` | None | Health check |

## Quick Start

```bash
# 1. Configure
cp .env.example .env
# Edit .env — set CC_API_KEY

# 2. Start
docker compose up -d --build

# 3. Login to Claude Code (first time only)
bash login.sh

# 4. Test
curl http://localhost:9100/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "sonnet", "messages": [{"role": "user", "content": "Hello"}]}'
```

## Models

Default aliases resolve to the **1M-context** variant (`[1m]` suffix). Use the
`*200k` variants to force the standard 200K window.

| Model ID | CLI Model | Context |
|----------|-----------|---------|
| `opus` | `opus[1m]` | 1M |
| `sonnet` | `sonnet[1m]` | 1M |
| `haiku` | `haiku` | 200K (1M not supported) |
| `opus200k` | `opus` | 200K |
| `sonnet200k` | `sonnet` | 200K |

Aliases like `claude-opus-4`, `claude-sonnet`, and the `cc-executor/<model>`
provider-style names also work (see `MODEL_MAP` in `server.py`).

> **Note on 1M access** — 1M context for Opus is included on the Max plan.
> Sonnet 1M availability depends on account state (see Anthropic docs). If a
> `[1m]` call fails, the proxy automatically retries and falls back to 200K
> (see below).

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CC_API_KEY` | (required) | Bearer token for API authentication |
| `CC_PORT` | `9100` | Host port mapping |
| `CC_TIMEOUT` | `300` | CLI execution timeout (seconds) |

## Authentication

Claude Code login is stored in a named Docker volume (`cc-auth`). Run `bash login.sh` once after first deploy. The session persists across container restarts.

## How It Works

Each request spawns `claude --print --system-prompt "..." "prompt"` as a subprocess. This uses Claude Code's `--print` mode which passes system prompts directly without SDK agent framework interference — system prompt adherence is reliable.

Non-streaming only. Each request blocks until the CLI completes.

### Request Flow

For each `POST /v1/chat/completions`:

1. **Auth check** — verify `Authorization: Bearer <CC_API_KEY>`.
2. **Parse body** — extract `messages`; join `system` messages into
   `--system-prompt` and concatenate `user`/`assistant` turns into the prompt.
3. **Resolve model** — look up the requested model in `MODEL_MAP` to get the
   CLI-level name (e.g. `opus` → `opus[1m]`, `opus200k` → `opus`).
4. **Run with retry/fallback** (`_run_claude_with_retry`):
   1. Invoke `claude --print --setting-sources "" --model <resolved> …`.
   2. On failure, **wait 5 seconds** and retry once with the same model.
   3. If still failing **and** the resolved model ends with `[1m]`, strip the
      suffix (e.g. `sonnet[1m]` → `sonnet`) and try once more on the 200K
      variant.
   4. If all three attempts fail, return HTTP 500 with the last error.
5. **Return** — wrap the CLI stdout in an OpenAI `chat.completion` envelope.
   If the response came from the fallback path, the CLI stdout is **prefixed
   with a visible fallback notice** and the JSON envelope carries an extra
   top-level `fallback` field (see below).

```
request ─► [1m] attempt ──ok──► 200 response
                │
                └─fail──► sleep 5s ─► [1m] retry ──ok──► 200 response
                                         │
                                         └─fail──► 200K fallback ──ok──► 200 response
                                                      │
                                                      └─fail──► 500 error
```

Retry delay is controlled by `RETRY_DELAY_SECONDS` in `server.py` (default: 5).
Fallback is skipped entirely for models that were already 200K (no `[1m]`
suffix after resolution), so `haiku`, `opus200k`, and `sonnet200k` just get a
single retry with no third attempt.

### Fallback notice format

When a fallback happens, the first block of `choices[0].message.content` is a
bracketed notice, followed by a blank line, then the actual model output:

```
[Fallback notice: requested opus[1m] but 1M context was unavailable after retry; served with opus (200K context) instead.]

<actual response body>
```

The JSON envelope also gains a non-standard top-level `fallback` field so
structured clients can detect it without string-matching:

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "model": "opus",
  "choices": [{ "index": 0, "message": {"role": "assistant", "content": "[Fallback notice: …]\n\n…"} }],
  "fallback": { "from": "opus[1m]", "to": "opus" }
}
```

Clients that strictly validate against the OpenAI schema will simply ignore
the extra `fallback` key.

### Notes on `--setting-sources ""`

The CLI is invoked with `--setting-sources ""` which disables loading of
user/project/local `settings.json`. This means settings-based config (CLAUDE.md
auto-discovery, MCP servers, hooks, etc.) is **not** applied — only flags
passed explicitly by this proxy take effect.
