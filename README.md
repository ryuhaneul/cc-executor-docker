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

| Model ID | CLI Model |
|----------|-----------|
| `opus` | Claude Opus |
| `sonnet` | Claude Sonnet |
| `haiku` | Claude Haiku |

Aliases like `claude-opus-4`, `claude-sonnet` also work.

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
