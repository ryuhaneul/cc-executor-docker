# cc-executor-docker

OpenAI-compatible API proxy for Claude Code CLI. Wraps `claude --print` in a Docker container with Bearer token authentication.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/v1/chat/completions` | Bearer | OpenAI-compatible chat completions |
| GET | `/v1/models` | Bearer | List available models |
| GET | `/health` | None | Health check |
| GET | `/admin/status` | Bearer | Proxy `claude auth status` → `{loggedIn, authMethod, apiProvider}` |
| POST | `/admin/oauth/start` | Bearer | Begin an OAuth 2.0 + PKCE login. Returns `{session_id, url}` — open the URL in a browser, approve, copy the resulting `code#state` blob. |
| POST | `/admin/oauth/complete` | Bearer | Body `{session_id, code}`. Exchanges the code against Anthropic's token endpoint and writes the bundle to `/root/.claude/.credentials.json` (0600). |
| POST | `/admin/credentials` | Bearer | Manual fallback. Body `{credentials: {claudeAiOauth: {...}}}` — writes the bundle verbatim. Use when a valid `.credentials.json` already exists (e.g. produced by `claude auth login` on another machine). |
| POST | `/admin/logout` | Bearer | Run `claude auth logout` |

### How to authenticate

The **preferred** path is the built-in OAuth flow — the CLI does not have
to exist on the front-end user's machine, and the token never leaves the
container:

1. `POST /admin/oauth/start` — receive `{session_id, url}`.
2. Open `url` in a browser, log in with your Anthropic (Claude Max/Pro)
   account, approve. Anthropic redirects to
   `console.anthropic.com/oauth/code/callback` with the authorization
   code in the URL fragment (`#code#state` style).
3. `POST /admin/oauth/complete` with `{session_id, code}`. The `code`
   can be the bare code, `code#state`, or the full callback URL — the
   server normalizes. Response: `{ok, loggedIn, authMethod, expiresAt,
   scopes}`.

Internally the server uses the published Claude Code OAuth client id and
PKCE endpoints — same as the official `claude auth login` flow:

- Authorize: `https://claude.ai/oauth/authorize`
- Token: `https://console.anthropic.com/v1/oauth/token`
- Client id: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- Redirect URI: `https://console.anthropic.com/oauth/code/callback`
- Scope: `org:create_api_key user:profile user:inference`
- Reference impl: <https://github.com/grll/claude-code-login>

OAuth session state (`session_id`, `code_verifier`, `state`) is kept in
process memory with a 10-minute TTL. That's intentional — the state is
an OAuth nonce, not a persistent credential. A container restart in the
middle of a login just voids the in-flight URL and the user retries.

The **resulting token** is persisted on disk at
`/root/.claude/.credentials.json` inside the `cc-auth` named volume, so
successful logins survive restarts and rebuilds. Token refresh happens
automatically on subsequent `claude --print` calls.

If you already have a `.credentials.json` (e.g. produced by
`claude auth login` on another box, or exported from a past run), you
can POST it to `/admin/credentials` to skip the OAuth dance entirely.

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

## Anthropic Terms of Service

This image bundles the **official** `@anthropic-ai/claude-code` CLI from
npm and authenticates against Anthropic's published OAuth endpoints — the
same endpoints the official `claude auth login` interactive flow uses.
Nothing here exfiltrates the token to a different API client or
re-implements Anthropic's inference protocol.

That said, Anthropic's consumer terms for Free/Pro/Max subscription
credentials prohibit:

- Routing subscription credentials through a third-party product on
  behalf of other end users
- Sharing or redistributing OAuth tokens
- Bulk/automated use that exceeds the product's intended scope

This image is intended for **personal, single-user** deployment — your
own Claude subscription serving your own workloads on your own machine.
For multi-user products or commercial integrations, use the Anthropic
Console API (`ANTHROPIC_API_KEY`) instead, which is governed by the
Commercial Terms.

References:
- <https://code.claude.com/docs/en/legal-and-compliance>
- <https://support.claude.com/en/articles/11145838-using-claude-code-with-your-max-plan>

## License

[MIT](./LICENSE) — for the proxy code in this repo only. The bundled
`@anthropic-ai/claude-code` CLI is Anthropic software with its own
license; see `https://anthropic.com/legal` for Anthropic's terms.
