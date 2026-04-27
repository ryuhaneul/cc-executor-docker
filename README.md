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

`opus` aliases default to the **1M-context** variant. `sonnet` aliases default
to the standard **200K** variant — to use 1M Sonnet, request `sonnet[1m]`
explicitly. The `[1m]` suffix on any name maps to the 1M CLI model verbatim.

| Model ID | CLI Model | Context |
|----------|-----------|---------|
| `opus[1m]` | `opus[1m]` | 1M (explicit) |
| `claude-opus[1m]` | `opus[1m]` | 1M (explicit) |
| `cc-executor/opus[1m]` | `opus[1m]` | 1M (explicit) |
| `sonnet[1m]` | `sonnet[1m]` | 1M (explicit) |
| `claude-sonnet[1m]` | `sonnet[1m]` | 1M (explicit) |
| `cc-executor/sonnet[1m]` | `sonnet[1m]` | 1M (explicit) |
| `opus` | `opus[1m]` | 1M (default) |
| `claude-opus` | `opus[1m]` | 1M (default) |
| `claude-opus-4` | `opus[1m]` | 1M (default) |
| `cc-executor/opus` | `opus[1m]` | 1M (default) |
| `sonnet` | `sonnet` | 200K (default) |
| `claude-sonnet` | `sonnet` | 200K (default) |
| `claude-sonnet-4` | `sonnet` | 200K (default) |
| `cc-executor/sonnet` | `sonnet` | 200K (default) |
| `haiku` | `haiku` | 200K (1M not supported) |
| `claude-haiku` | `haiku` | 200K (1M not supported) |
| `claude-haiku-4` | `haiku` | 200K (1M not supported) |
| `cc-executor/haiku` | `haiku` | 200K (1M not supported) |
| `opus200k` | `opus` | 200K (force 200K) |
| `cc-executor/opus200k` | `opus` | 200K (force 200K) |
| `sonnet200k` | `sonnet` | 200K (alias of `sonnet`) |
| `cc-executor/sonnet200k` | `sonnet` | 200K (alias of `sonnet`) |

See `MODEL_MAP` in `server.py` for the source of truth.

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

The HTTP server is a `ThreadingHTTPServer`, so multiple `/v1/chat/completions`
requests are handled **concurrently** — each in its own thread, each spawning
its own `claude --print` subprocess. A slow request no longer blocks other
callers. Per-request `--session-id` (UUID v4) keeps the per-call jsonl files
isolated from each other.

Non-streaming only.

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

### Tool-enabled modes

By default, `/v1/chat/completions` is text-only — no tools, single-shot prompt
in / model text out. For workloads where one Claude response is too small
(e.g. cleaning up a 900K-token YouTube transcript whose output runs into the
millions of tokens), there are two opt-in tool-enabled modes:

- **File-output mode (`output_files: true`)** — the proxy creates a per-request
  scratch dir, the agent writes deliverables there, and the dir is read back
  into the JSON response and then wiped. Use this when the **caller has only
  HTTP access** (no shared filesystem) and wants the bytes back inline.
- **Direct-filesystem mode (`cwd` / `add_dirs`)** — the caller supplies one or
  more bind-mounted directories, and the agent reads/writes inside them
  directly. The proxy never reads the files itself, never includes them in
  the JSON response, and never deletes them. Use this when the caller and
  cc-executor share a volume (e.g. a sibling service on the same Docker host).

#### How tools are unlocked

Both tool-enabled modes pass the following flags to `claude --print`:

| Flag | Why |
|------|-----|
| `--allow-dangerously-skip-permissions` | Enables the bypass option (the CLI ships with this option gated off). |
| `--dangerously-skip-permissions` | Actually applies the bypass — needed because `--print` is non-interactive, so any tool call requiring approval would otherwise hang or fail. |
| `--allowedTools <tool>` (repeated) | Whitelists the tools the agent may invoke. Defaults to `Read Write Edit` (no `Bash`); override via the `allowed_tools` body field. |
| `--add-dir <path>` (repeated) | Per `add_dirs` body field — extra paths the agent may access on top of `cwd`. |
| `--max-turns N` | Caps tool iterations. Each `Write` call is one turn, so a multi-megabyte deliverable typically needs 20–60 turns. |

Both `--allow-dangerously-skip-permissions` and `--dangerously-skip-permissions`
are required — the first alone only unlocks the option, and the second alone
is a no-op on builds where the gate is enforced.

#### Body fields (all modes)

| Field | Default | Applies to | Notes |
|-------|---------|-----------|-------|
| `output_files` | `false` | — | `true` enables file-output mode. |
| `cwd` | `/app/workdir` | direct-fs | Working directory for the CLI. Set to a bind-mounted path you control (e.g. `/storage/jobs/<id>`). |
| `add_dirs` | `[]` | direct-fs / file-output | Extra directories the agent may access (passed as repeated `--add-dir`). |
| `allowed_tools` | `["Read","Write","Edit"]` (tool modes) | tool modes | Override the default whitelist. To allow shell access, include `"Bash"` — see security caveat below. |
| `max_turns` | `50` (tool modes) / unset (text mode) | all | Cap on tool-call iterations. |
| `timeout` | `CC_TIMEOUT` (default `300`) | all | Per-request CLI timeout in seconds. Long jobs usually need 1200–1800. |

Mode is selected automatically:

- `output_files: true` → file-output mode (scratch dir + JSON `files`).
- `output_files` absent/false **and** any of `cwd` / `add_dirs` / `allowed_tools`
  is set → direct-filesystem mode (no scratch, no JSON `files`).
- Otherwise → text-only.

#### File-output mode — example (HTTP-only caller)

Clean a YouTube subtitle file and detect song segments, with the deliverables
returned in the JSON response:

```bash
curl http://localhost:9100/v1/chat/completions \
  -H "Authorization: Bearer $CC_API_KEY" \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
{
  "model": "opus[1m]",
  "output_files": true,
  "max_turns": 60,
  "timeout": 1800,
  "messages": [
    {"role": "system", "content": "You are a subtitle post-processor."},
    {"role": "user", "content": "Clean up this auto-generated subtitle file (smooth phrasing, fix punctuation, keep timestamps) and ALSO detect any music sections, listing the song info you can identify.\n\nProduce two files:\n - subtitle.srt — the cleaned-up SRT\n - songs.json — array of {start, end, title, artist} for detected songs.\n\nRaw transcript follows:\n\n<...900K tokens of raw subtitles...>"}
  ]
}
JSON
```

The server creates `/app/workdir/req-<uuid>/` inside the container, prepends a
system-prompt instruction telling the model to write deliverables via `Write`
and only summarize in its text response, runs the CLI with `cwd=<request_dir>`,
then walks the dir into the response and `rmtree`s it. UTF-8 files are inlined
verbatim; binary/undecodable files come back as `data:base64,<…>`.

Response shape:

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "model": "opus[1m]",
  "choices": [{ "index": 0, "message": { "role": "assistant", "content": "Wrote subtitle.srt (cleaned-up SRT, 12,432 cues) and songs.json (3 detected songs)." }, "finish_reason": "stop" }],
  "files": {
    "subtitle.srt": "1\n00:00:00,000 --> 00:00:03,200\n...",
    "songs.json": "[{\"start\":\"00:14:22\",\"end\":\"00:18:05\",\"title\":\"…\",\"artist\":\"…\"},…]"
  }
}
```

#### Direct-filesystem mode — example (shared-volume caller)

When the caller and cc-executor share a bind-mounted volume (e.g. cc-executor
runs alongside another service on the same Docker host and both mount
`./data/jobs:/storage/jobs`), the caller can point the agent at a per-job
directory and let it read input files / write outputs directly — no JSON file
payload, no scratch dir, no auto-deletion:

```bash
curl http://cc-executor:9100/v1/chat/completions \
  -H "Authorization: Bearer $CC_API_KEY" \
  -H "Content-Type: application/json" \
  -d @- <<'JSON'
{
  "model": "opus[1m]",
  "cwd": "/storage/jobs/job-abc123",
  "allowed_tools": ["Read", "Write", "Edit"],
  "max_turns": 60,
  "timeout": 1800,
  "messages": [
    {"role": "user", "content": "Read raw.srt in time-ordered chunks (use the Read tool's offset/limit). For each chunk, append cleaned subtitles to clean.srt and any detected song segments to songs.json. Do not load the whole transcript into context at once."}
  ]
}
JSON
```

Response is just a text summary — the deliverables stay on the shared volume
where the caller can pick them up:

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "model": "opus[1m]",
  "choices": [{ "index": 0, "message": { "role": "assistant", "content": "Processed raw.srt in 12 chunks. Wrote clean.srt (12,401 cues) and songs.json (3 song segments) into /storage/jobs/job-abc123/." }, "finish_reason": "stop" }]
}
```

To bind-mount the path, add it to the cc-executor service in your Compose
file:

```yaml
services:
  cc-executor:
    build:
      context: "https://github.com/ryuhaneul/cc-executor-docker.git#main"
    volumes:
      - cc-auth:/root/.claude
      - ./data/jobs:/storage/jobs:rw   # ← shared with the calling service
```

The `cwd` value (`/storage/jobs/job-abc123`) is the path **inside the
cc-executor container**, not the host path.

#### Caveats (both tool-enabled modes)

- **Output token ceiling per turn still applies.** Claude's max output per
  assistant turn is ~64K tokens, so to produce >64K of file content the model
  must call `Write` multiple times (one chunk per turn). Set `max_turns`
  generously — for ~1M tokens of output, 20–60 turns is realistic.
- **Context window is per the resolved model.** 900K-token inputs require an
  `opus[1m]` (or `sonnet[1m]`) request. Bare `sonnet`/`haiku` will reject the
  oversized prompt before the agent runs. For inputs that exceed even 1M,
  use direct-filesystem mode and instruct the model to `Read` the source
  file in offset/limit chunks so the full text is never resident in context.
- **Response size scales with deliverable size (file-output only).** A 1M-token
  output ≈ 4–8 MB of JSON. Make sure your client and any reverse proxy (Nginx
  `client_max_body_size`, `proxy_read_timeout`, etc.) can handle it. Direct-
  filesystem mode sidesteps this — the response is just the text summary.
- **Sandbox / blast radius.** With `--dangerously-skip-permissions` the agent
  can use any whitelisted tool with no further approval. Keep `allowed_tools`
  scoped (the default `Read Write Edit` excludes `Bash`), and in
  direct-filesystem mode mount **only the directory the job needs** — not the
  whole jobs root — so a prompt-injected agent can't reach unrelated jobs.
- **No auto-cleanup in direct-filesystem mode.** The proxy never deletes
  files in `cwd` or `add_dirs`. The caller owns the lifecycle of those paths.

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

## Credits

The OAuth 2.0 + PKCE flow in `/admin/oauth/*` was implemented by
studying **[grll/claude-code-login](https://github.com/grll/claude-code-login)**,
which reverse-engineered the published endpoints and client id used by
the official `claude auth login` command. This project reuses the same
public OAuth client id, authorize URL, token URL, redirect URI, and
scopes — they are Anthropic's published interface, not workarounds or
private APIs. Big thanks to [@grll](https://github.com/grll) for the
reference implementation that made a cleanly self-contained version
possible here.

Specific bits borrowed (structure, not code):

- PKCE derivation: `code_verifier = base64url(os.urandom(32))`,
  `code_challenge = base64url(sha256(code_verifier))`
- Authorize URL parameters (including the `code=true` flag that makes
  Anthropic return the code on a display page rather than redirecting
  into the terminal flow)
- Token endpoint payload shape (JSON, `grant_type=authorization_code`
  + `code_verifier`)
- Credentials file schema (`~/.claude/.credentials.json` with the
  `claudeAiOauth` wrapper object)

## License

[MIT](./LICENSE) — for the proxy code in this repo only. The bundled
`@anthropic-ai/claude-code` CLI is Anthropic software with its own
license; see `https://anthropic.com/legal` for Anthropic's terms.
