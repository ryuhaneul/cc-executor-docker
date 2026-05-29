from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import os
import socket
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

UID_MIN = int(os.environ.get("WD_UID_MIN", "20000"))
UID_MAX = int(os.environ.get("WD_UID_MAX", "60000"))
UID_REGISTRY_PATH = Path(os.environ.get("WD_UID_REGISTRY", "/data/uid_registry.json"))
AUTH_ROOT = Path(os.environ.get("WD_AUTH_ROOT", "/data/auth"))
WS_ROOT = Path(os.environ.get("WD_WS_ROOT", "/data/ws"))
REDIS_URL = os.environ.get("REDIS_URL", "")

REQUIRED_CLAIMS = (
    "slot_id",
    "slot_tenant_id",
    "tenant_id",
    "chatroom_id",
    "requester_id",
    "provider",
    "config_dir",
    "ws_cwd",
    "mode",
    "tools_allowed",
    "owner_private",
    "lease_epoch",
    "fence",
    "exp",
    "jti",
    "kid",
    "body_hash",
    "op",
)


class WDClaimError(ValueError):
    pass


class ReplayStore(Protocol):
    def setnx_ex(self, key: str, ttl_seconds: int) -> bool: ...


class MemoryReplayStore:
    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def setnx_ex(self, key: str, ttl_seconds: int) -> bool:
        now = int(time.time())
        self._seen = {k: exp for k, exp in self._seen.items() if exp > now}
        if key in self._seen:
            return False
        self._seen[key] = now + ttl_seconds
        return True


class RedisReplayStore:
    def __init__(self, redis_url: str) -> None:
        parsed = urllib.parse.urlparse(redis_url)
        if parsed.scheme != "redis":
            raise WDClaimError("REDIS_URL must use redis://")
        self.host = parsed.hostname or "redis"
        self.port = parsed.port or 6379
        self.db = int((parsed.path or "/0").strip("/") or "0")
        self.password = urllib.parse.unquote(parsed.password) if parsed.password else None

    def _command(self, *parts: str) -> bytes:
        out = [f"*{len(parts)}\r\n".encode("ascii")]
        for part in parts:
            raw = part.encode("utf-8")
            out.append(f"${len(raw)}\r\n".encode("ascii"))
            out.append(raw + b"\r\n")
        return b"".join(out)

    def _read_line(self, sock: socket.socket) -> bytes:
        data = bytearray()
        while not data.endswith(b"\r\n"):
            chunk = sock.recv(1)
            if not chunk:
                raise WDClaimError("redis connection closed")
            data.extend(chunk)
        return bytes(data[:-2])

    def _expect_simple_ok(self, sock: socket.socket) -> None:
        line = self._read_line(sock)
        if line != b"+OK":
            raise WDClaimError("redis command failed")

    def setnx_ex(self, key: str, ttl_seconds: int) -> bool:
        with socket.create_connection((self.host, self.port), timeout=2) as sock:
            if self.password:
                sock.sendall(self._command("AUTH", self.password))
                self._expect_simple_ok(sock)
            if self.db:
                sock.sendall(self._command("SELECT", str(self.db)))
                self._expect_simple_ok(sock)
            sock.sendall(self._command("SET", key, "1", "NX", "EX", str(ttl_seconds)))
            line = self._read_line(sock)
        if line == b"+OK":
            return True
        if line == b"$-1":
            return False
        raise WDClaimError("redis SET NX EX failed")


_MEMORY_REPLAY = MemoryReplayStore()


def replay_store_from_env() -> ReplayStore:
    if REDIS_URL:
        return RedisReplayStore(REDIS_URL)
    return _MEMORY_REPLAY


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def compute_body_hash(body: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def verify_claim(
    token: str,
    secret: str,
    *,
    body: Mapping[str, Any],
    expected_op: str = "wd.run",
    replay_store: ReplayStore | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    if not secret:
        raise WDClaimError("missing WD_CLAIM_SIGNING_SECRET")
    try:
        payload_segment, signature_segment = token.split(".", 1)
    except ValueError as exc:
        raise WDClaimError("malformed claim") from exc

    expected_sig = hmac.new(
        secret.encode("utf-8"), payload_segment.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(signature_segment, _b64url_encode(expected_sig)):
        raise WDClaimError("bad claim signature")

    try:
        claims = json.loads(_b64url_decode(payload_segment))
    except (ValueError, json.JSONDecodeError) as exc:
        raise WDClaimError("bad claim payload") from exc
    if not isinstance(claims, dict):
        raise WDClaimError("bad claim payload")
    _validate_claim_shape(claims)

    current_time = int(time.time()) if now is None else now
    if int(claims["exp"]) <= current_time:
        raise WDClaimError("expired claim")
    if claims["op"] != expected_op:
        raise WDClaimError("claim op mismatch")
    if not hmac.compare_digest(str(claims["body_hash"]), compute_body_hash(body)):
        raise WDClaimError("claim body mismatch")

    store = replay_store if replay_store is not None else replay_store_from_env()
    ttl_seconds = max(1, int(claims["exp"]) - current_time)
    if not store.setnx_ex(f"wd_claim:jti:{claims['jti']}", ttl_seconds):
        raise WDClaimError("replayed claim")

    return claims


def _validate_claim_shape(claims: Mapping[str, Any]) -> None:
    missing = [name for name in REQUIRED_CLAIMS if name not in claims]
    if missing:
        raise WDClaimError(f"missing claim fields: {', '.join(missing)}")

    for name in (
        "slot_id",
        "slot_tenant_id",
        "tenant_id",
        "chatroom_id",
        "requester_id",
        "provider",
        "config_dir",
        "ws_cwd",
        "mode",
        "fence",
        "jti",
        "kid",
        "body_hash",
        "op",
    ):
        if not isinstance(claims[name], str) or not claims[name]:
            raise WDClaimError(f"bad claim field: {name}")
    if claims["provider"] not in {"claude", "codex"}:
        raise WDClaimError("bad claim field: provider")
    if claims["mode"] not in {"A", "B"}:
        raise WDClaimError("bad claim field: mode")
    for name in ("tools_allowed", "owner_private"):
        if not isinstance(claims[name], bool):
            raise WDClaimError(f"bad claim field: {name}")
    for name in ("lease_epoch", "exp"):
        if not isinstance(claims[name], int):
            raise WDClaimError(f"bad claim field: {name}")
    for name in ("slot_id", "slot_tenant_id", "tenant_id", "chatroom_id", "requester_id"):
        try:
            uuid.UUID(str(claims[name]))
        except ValueError as exc:
            raise WDClaimError(f"bad uuid claim field: {name}") from exc


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.root)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise WDClaimError(f"symlink path rejected: {path}")


def _validate_direct_child(path_value: str, root: Path, basename: str, label: str) -> Path:
    try:
        uuid.UUID(basename)
    except ValueError as exc:
        raise WDClaimError(f"{label} basename must be UUID") from exc
    path = Path(path_value)
    if not path.is_absolute():
        raise WDClaimError(f"{label} path must be absolute")
    root_resolved = root.resolve(strict=False)
    expected = root_resolved / basename
    _reject_symlink_components(path)
    if path.resolve(strict=False) != expected:
        raise WDClaimError(f"{label} path mismatch")
    if path.parent.resolve(strict=False) != root_resolved:
        raise WDClaimError(f"{label} must be direct child")
    return expected


def validate_claim_paths(claims: Mapping[str, Any]) -> tuple[Path, Path]:
    provider = str(claims["provider"])
    slot_id = str(claims["slot_id"])
    tenant_id = str(claims["tenant_id"])
    chatroom_id = str(claims["chatroom_id"])

    config_root = AUTH_ROOT / provider / "users"
    ws_owner_root = WS_ROOT / tenant_id
    config_dir = _validate_direct_child(str(claims["config_dir"]), config_root, slot_id, "config_dir")
    ws_cwd = _validate_direct_child(str(claims["ws_cwd"]), ws_owner_root, chatroom_id, "ws_cwd")
    return config_dir, ws_cwd


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def get_or_allocate_uid(slot_id: str) -> int:
    UID_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = UID_REGISTRY_PATH.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if UID_REGISTRY_PATH.exists():
            with open(UID_REGISTRY_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
        else:
            data = {"next_uid": UID_MIN, "slots": {}, "retired": []}

        slots = data.setdefault("slots", {})
        if slot_id in slots:
            return int(slots[slot_id])

        uid = int(data.get("next_uid", UID_MIN))
        if uid > UID_MAX:
            raise WDClaimError("uid registry exhausted")
        slots[slot_id] = uid
        data["next_uid"] = uid + 1
        data.setdefault("retired", [])

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{UID_REGISTRY_PATH.name}.",
            suffix=".tmp",
            dir=str(UID_REGISTRY_PATH.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, sort_keys=True, separators=(",", ":"))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, UID_REGISTRY_PATH)
            _fsync_dir(UID_REGISTRY_PATH.parent)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return uid


def prepare_slot_dirs(slot_id: str, config_dir: Path, ws_cwd: Path, uid: int) -> None:
    for path in (config_dir, ws_cwd):
        path.mkdir(parents=True, exist_ok=True)
        _reject_symlink_components(path)
        # chmod 를 chown 보다 먼저: chown 후엔 root 가 소유자가 아니라
        # CAP_FOWNER 없이 chmod 불가(EPERM). root 소유일 때 chmod 후 chown.
        os.chmod(path, 0o700)
        os.chown(path, uid, uid)
