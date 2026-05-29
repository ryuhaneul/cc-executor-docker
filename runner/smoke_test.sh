#!/usr/bin/env bash
set -euo pipefail

WD_URL="${CC_EXECUTOR_WD_URL:-http://localhost:9101}"
LEGACY_URL="${CC_EXECUTOR_LEGACY_URL:-http://localhost:9100}"
API_KEY="${CC_API_KEY:-test-api-key}"
WD_SECRET="${WD_CLAIM_SIGNING_SECRET:-test-wd-claim-secret-32-bytes-minimum}"

pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1"; exit 1; }

code() {
  python3 - "$@" <<'PY'
import json, sys, urllib.error, urllib.request
method, url, api_key, claim, body = sys.argv[1:]
data = None if body == "-" else body.encode()
headers = {}
if api_key != "-":
    headers["Authorization"] = f"Bearer {api_key}"
if claim != "-":
    headers["X-WD-Claim"] = claim
if data is not None:
    headers["Content-Type"] = "application/json"
req = urllib.request.Request(url, data=data, method=method, headers=headers)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        print(resp.status)
except urllib.error.HTTPError as exc:
    print(exc.code)
PY
}

claim() {
  python3 - "$WD_SECRET" "$1" "$2" "$3" "${4:-$body}" "${5:-normal}" <<'PY'
import base64, hashlib, hmac, json, sys, time
secret, slot_id, chatroom_id, jti, body_json, variant = sys.argv[1:]
body = json.loads(body_json)
payload = {
    "slot_id": slot_id,
    "slot_tenant_id": "11111111-1111-4111-8111-111111111111",
    "tenant_id": "11111111-1111-4111-8111-111111111111",
    "chatroom_id": chatroom_id,
    "requester_id": "22222222-2222-4222-8222-222222222222",
    "provider": "claude",
    "config_dir": f"/data/auth/claude/users/{slot_id}",
    "ws_cwd": f"/data/ws/11111111-1111-4111-8111-111111111111/{chatroom_id}",
    "mode": "A",
    "tools_allowed": False,
    "owner_private": False,
    "lease_epoch": 1,
    "fence": "1",
    "exp": int(time.time()) + 120,
    "jti": jti,
    "kid": "stage0",
    "op": "wd.run",
}
if variant == "expired":
    payload["exp"] = int(time.time()) - 1
if variant == "nested_config":
    payload["config_dir"] = f"/data/auth/claude/users/{slot_id}/nested"
if variant == "other_slot_config":
    payload["config_dir"] = "/data/auth/claude/users/55555555-5555-4555-8555-555555555555"
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
payload["body_hash"] = hashlib.sha256(canonical(body)).hexdigest()
b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
seg = b64(canonical(payload))
sig = b64(hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest())
print(seg + "." + sig)
PY
}

body='{"command":"id","args":["-u"]}'
slot="33333333-3333-4333-8333-333333333333"
chat="44444444-4444-4444-8444-444444444444"
token="$(claim "$slot" "$chat" "jti-smoke-a")"

[[ "$(code POST "$WD_URL/v1/chat" "$API_KEY" - "$body")" =~ ^(404|403)$ ]] || fail "legacy /v1 disabled on WD port"
[[ "$(code POST "$WD_URL/admin/status" "$API_KEY" - "$body")" =~ ^(404|403)$ ]] || fail "legacy /admin disabled on WD port"
pass "legacy disabled on WD port"

[[ "$(code POST "$WD_URL/wd/v1/run" "$API_KEY" "$token" "$body")" == "200" ]] || fail "valid wd claim run"
pass "valid wd claim run"

[[ "$(code POST "$WD_URL/wd/v1/run" "$API_KEY" "${token}x" "$body")" == "403" ]] || fail "forged signature rejected"
pass "forged signature rejected"

[[ "$(code POST "$WD_URL/wd/v1/run" "$API_KEY" "$token" "$body")" == "403" ]] || fail "jti replay rejected"
pass "jti replay rejected"

token_b="$(claim "$slot" "$chat" "jti-smoke-b")"
[[ "$(code POST "$WD_URL/wd/v1/run" "$API_KEY" "$token_b" '{"command":"echo","args":["mutated"]}')" == "403" ]] || fail "body mutation rejected"
pass "body mutation rejected"

token_c="$(claim "$slot" "$chat" "jti-smoke-c" "$body" expired)"
[[ "$(code POST "$WD_URL/wd/v1/run" "$API_KEY" "$token_c" "$body")" == "403" ]] || fail "expired claim rejected"
pass "expired claim rejected"

token_d="$(claim "$slot" "$chat" "jti-smoke-d" "$body" nested_config)"
[[ "$(code POST "$WD_URL/wd/v1/run" "$API_KEY" "$token_d" "$body")" == "403" ]] || fail "nested config_dir rejected"
pass "nested config_dir rejected"

body_with_config='{"command":"id","args":["-u"],"config_dir":"/root/.claude"}'
token_e="$(claim "$slot" "$chat" "jti-smoke-e" "$body_with_config")"
[[ "$(code POST "$WD_URL/wd/v1/run" "$API_KEY" "$token_e" "$body_with_config")" == "200" ]] || fail "body config_dir ignored"
pass "body config_dir ignored"

printf 'INFO legacy golden check: set CC_DISABLE_LEGACY=false and query %s/v1/models separately.\n' "$LEGACY_URL"

if [[ -n "${CC_EXECUTOR_CONTAINER:-}" ]]; then
  docker exec "$CC_EXECUTOR_CONTAINER" python3 - <<'PY' && pass "egress proxy allow/block"
import os, urllib.error, urllib.request
allow = os.environ.get("WD_SMOKE_ALLOW_URL", "https://api.openai.com/")
block = os.environ.get("WD_SMOKE_BLOCK_URL", "https://example.com/")
def fetch(url):
    try:
        urllib.request.urlopen(url, timeout=10).read(1)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False
if not fetch(allow):
    raise SystemExit("allowlist URL was blocked")
if fetch(block):
    raise SystemExit("blocked URL was allowed")
PY
else
  printf 'INFO egress proxy check skipped; set CC_EXECUTOR_CONTAINER for docker exec smoke.\n'
fi
