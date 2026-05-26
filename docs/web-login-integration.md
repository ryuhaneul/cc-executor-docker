# Web Login Integration for Claude Code and Codex

This document is the implementation handoff for web applications such as
vod-clip that need browser-based login for CLI-backed AI providers.

## Decision

- Web login is required when a web application lets end users connect their own
  Claude Code or Codex account.
- Web login is not the universal default for every deployment. Headless,
  single-user, or automation deployments may still use CLI-prepared auth or API
  keys where supported.
- API keys are a fallback for Codex web-app integrations, not the primary
  vod-clip user flow.
- Web apps must keep credentials in the executor's per-user config directories,
  not in the app database.

## Current Claude Code Web Login

`cc-executor-docker` already exposes Claude Code web-login endpoints:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/status` | Check Claude auth status. |
| `POST` | `/admin/oauth/start` | Start OAuth 2.0 + PKCE login. Returns `{session_id, url}`. |
| `POST` | `/admin/oauth/complete` | Complete login with `{session_id, code}`. |
| `POST` | `/admin/credentials` | Manual fallback for an existing Claude credentials bundle. |
| `POST` | `/admin/logout` | Run `claude auth logout`. |
| `DELETE` | `/admin/config-dir` | Delete one per-user Claude config directory. |

The Claude web flow is:

1. Web app calls `/admin/oauth/start` with `X-Claude-Config-Dir`.
2. Web app opens the returned `url`.
3. User logs in with the Anthropic / Claude account.
4. User copies the returned callback code or URL fragment.
5. Web app calls `/admin/oauth/complete` with the original `session_id` and
   copied `code`.
6. cc-executor writes credentials under the selected Claude config directory.
7. Later Claude requests use the same `X-Claude-Config-Dir`.

Claude per-user isolation:

- Shared default: `/root/.claude`.
- Per-user root: `/root/.claude/users`.
- Web apps should use direct children such as `/root/.claude/users/<user_id>`.
- Nested paths, symlink escapes, and external paths are rejected.

## Current Codex State

`cc-executor-docker` already has additive Codex execution support:

- `provider: "codex"` or `model: "codex/*"` routes to `codex exec`.
- `provider: "codex"` with no model resolves to `codex/default`.
- `/admin/codex/status` reports Codex CLI availability and auth state.
- `X-Codex-Config-Dir` or `codex_config_dir` selects a Codex config directory.
- Valid Codex config directories are only `/root/.codex` or direct children of
  `/root/.codex/users/`.
- `CODEX_API_KEY` can be used for fallback/testing and is passed to Codex as
  both `CODEX_API_KEY` and `OPENAI_API_KEY` when present.
- `CODEX_ACCESS_TOKEN` is intentionally not consumed from environment variables.

What is missing is the Codex web-login layer equivalent to Claude Code
`/admin/oauth/start` and `/admin/oauth/complete`.

## Codex Web Login Endpoint Plan

Add Codex endpoints without changing existing Claude endpoints.

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/admin/codex/status` | Already exists. Keep it as the status source. |
| `POST` | `/admin/codex/login/start` | Start a Codex browser/device login session. |
| `POST` | `/admin/codex/login/complete` | Complete login and write auth state under `CODEX_HOME`. |
| `POST` | `/admin/codex/credentials` | Fallback only. Import an existing Codex `auth.json` or access-token based auth. |
| `POST` | `/admin/codex/logout` | Logout or delete local Codex auth state for the selected config dir. |
| `DELETE` | `/admin/codex/config-dir` | Optional. Delete one per-user Codex config dir. |

Use the existing Bearer auth middleware for all admin endpoints.

Codex per-user isolation:

- Shared default: `/root/.codex`.
- Per-user root: `/root/.codex/users`.
- Web apps should use direct children such as `/root/.codex/users/<user_id>`.
- Nested paths, the users root itself, symlink escapes, and external paths must
  be rejected.

## Login Session State

Use the same security pattern for Claude and Codex web login:

- Generate an opaque `session_id`.
- Bind the session to the selected provider config dir.
- In vod-clip, bind the session to the current app user as well.
- Store session state in process memory with a short TTL, e.g. 10 minutes.
- A container or app restart may invalidate in-flight logins; that is
  acceptable.
- Never log access tokens, refresh tokens, copied codes, or full callback URLs.

## Codex CLI Flow to Validate

Before implementing Codex web login, validate the installed Codex CLI contract
inside the container:

```bash
codex login --help
codex login --device-auth --help
codex login status
```

The desired web flow is:

1. Web app calls `/admin/codex/login/start` with `X-Codex-Config-Dir`.
2. cc-executor starts a non-interactive Codex browser/device login.
3. Response includes the URL plus any user-visible code required by Codex.
4. User completes login in the browser.
5. Web app calls `/admin/codex/login/complete` if the CLI flow requires a pasted
   code or polling finalization.
6. cc-executor verifies with `codex login status`.
7. Later Codex requests use the same `X-Codex-Config-Dir`.

If the installed Codex CLI does not expose a stable machine-readable device
login flow, keep the web-login design as the target and implement a fallback
import route first:

- `POST /admin/codex/credentials` accepts an existing `auth.json` payload and
  writes it to the selected `CODEX_HOME` with mode `0600`.
- Or accept an access token and run `codex login --with-access-token` in a
  subprocess with the selected `CODEX_HOME`.
- In vod-clip UI, label this as fallback / advanced import, not the main login
  button.

## Status Response

Claude status already returns the Claude auth fields from `/admin/status`.

Codex status should continue to return:

```json
{
  "loggedIn": true,
  "authMethod": "oauth",
  "codexHome": "/root/.codex/users/<user_id>",
  "cliAvailable": true
}
```

Recommended Codex `authMethod` values:

- `oauth` or `chatgpt` for browser login.
- `api_key` for `CODEX_API_KEY`.
- `auth_json` for imported auth state.
- `null` when not logged in.

For web apps, treat `oauth` / `chatgpt` and `auth_json` as user auth. Treat
`api_key` as fallback or shared automation auth.

## vod-clip Mapping

vod-clip already has a Claude web-login shape:

- `backend/services/cc_auth.py`
- `backend/routers/cc_login.py`
- `backend/routers/api_keys.py` provider `"cc"` sentinel behavior
- `backend/static/index.html` CC login button, status badge, and code submit UI

Add Codex in parallel instead of replacing the Claude flow.

### Backend Service

Add `backend/services/codex_auth.py`:

- `CODEX_CONFIG_ROOT = "/root/.codex/users"`
- `codex_config_dir_for_user(user_id) -> /root/.codex/users/<user_id>`
- `codex_headers_for_user(user_id)`:
  - `Authorization: Bearer <CC_API_KEY>`
  - `X-Codex-Config-Dir: /root/.codex/users/<user_id>`
- `codex_user_status(user_id)` calls cc-executor `/admin/codex/status`.
- `codex_user_authenticated(user_id)` returns true only when status is logged in.

### Backend Router

Add `backend/routers/codex_login.py`:

- Prefix: `/api/ai/codex`
- `GET /status`
- `POST /login/start`
- `POST /login/complete`
- Optional fallback: `POST /credentials`
- Optional local cleanup: `POST /logout` and/or `DELETE /config-dir`

Bind Codex login sessions to the current vod-clip user:

- Store `session_id -> (user_id, expires_at)` in memory.
- On complete, reject unknown, expired, or cross-user session IDs.
- Forward `X-Codex-Config-Dir` on start, complete, status, credentials, logout,
  and generation requests.

### Provider Catalog and API Keys

Update `backend/routers/api_keys.py`:

- Add provider `"codex"` if Codex should appear separately from `"cc"`.
- Add a sentinel such as `CODEX_OAUTH_SENTINEL = "__codex_oauth__"`.
- Do not store real Codex tokens in the `api_keys` table.
- Provider availability for `"codex"` should come from
  `codex_user_authenticated(user.id)`.
- Model fetch for `"codex"` should call cc-executor `/v1/models` with
  `X-Codex-Config-Dir`, then filter to `codex/*` model IDs for the UI.
- Test key for `"codex"` should call `/admin/codex/status` and `/v1/models`;
  it should not require an API key.

### LLM Client

Update the LLM request path for provider `"codex"`:

- Use the same cc-executor base URL as Claude Code.
- Add `provider: "codex"` to the `/v1/chat/completions` body.
- Send `X-Codex-Config-Dir` for the current user.
- Do not send `allowed_tools` or `max_turns`; cc-executor rejects those for
  Codex by design.
- For direct filesystem work, use `codex_sandbox: "workspace-write"` and pass
  the intended `cwd` / `add_dirs`.

### Frontend

Update `backend/static/index.html`:

- Keep the current CC login UI for Claude Code.
- Add a separate Codex provider option and Codex login UI.
- Hide the API key input for Codex in normal web-app mode, matching CC behavior.
- Show a Codex status badge and "Codex login" button.
- Add `refreshCodexStatusBadges()`.
- Add `openCodexLogin()` and `submitCodexLoginCode()` mirroring the CC flow.
- Show Codex API-key/import only as an advanced fallback.
- Model picker should show Codex model IDs, preferably filtered to `codex/*`.

Suggested Codex UI text:

> Codex can connect through your OpenAI / ChatGPT account for this web app.
> API key mode is available as an advanced fallback.

## Deployment Plan for vod-clip

Do not keep a separate public cc-executor test stack for product use.

1. Implement Codex web-login endpoints in cc-executor.
2. Rebuild the cc-executor image used by the existing vod-clip / ai-platform
   compose stack.
3. Preserve the existing Claude `cc-auth` volume.
4. Add a persistent `codex-auth` volume mounted at `/root/.codex`.
5. Deploy through the existing vod-clip / ai-platform compose path, replacing
   the internal cc-executor service rather than creating a parallel public
   stack.
6. Run smoke tests:
   - `/health`
   - `/v1/models`
   - `/admin/status`
   - `/admin/codex/status`
   - Claude web login still works
   - Codex web login completes and survives container restart
   - Provider/model conflicts return 400
   - Codex generation with `provider: "codex"` succeeds after login

## Acceptance Criteria

- Existing Claude Code web login continues to work unchanged.
- Existing Claude model defaults and `/v1/models` ordering are unchanged.
- A vod-clip user can log in to Claude Code from the browser.
- A vod-clip user can log in to Codex from the browser.
- Different vod-clip users get different Claude and Codex config dirs.
- Codex API-key fallback still works, but it is not the normal web-app login
  path.
- No Claude or Codex token is written to vod-clip database rows, memory files,
  or logs.
- Claude and Codex auth survive cc-executor container restarts through their
  Docker volumes.
- Removing a user can delete that user's Claude and Codex config directories
  without affecting other users.

## References

- OpenAI Codex CLI sign-in guidance:
  <https://help.openai.com/en/articles/11381614-api-codex-cli-and-sign-in-with-chatgpt>
- Codex CLI repository: <https://github.com/openai/codex>
