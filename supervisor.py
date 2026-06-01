from __future__ import annotations

import os
import fcntl
import signal
import subprocess
import sys
import time
import uuid

_LOCK_FH = None


def _acquire_instance_lock() -> None:
    global _LOCK_FH
    lock_path = os.environ.get("WD_INSTANCE_LOCK", "/data/.wd_executor.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    _LOCK_FH = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_FH.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise RuntimeError("another WD executor instance is already running") from exc


def main() -> int:
    workers = int(os.environ.get("WD_UVICORN_WORKERS", "1"))
    if workers != 1:
        print("WD_UVICORN_WORKERS must be 1", file=sys.stderr)
        return 1
    try:
        _acquire_instance_lock()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    os.environ.setdefault("WD_INSTANCE_ID", str(uuid.uuid4()))

    procs: list[subprocess.Popen[bytes]] = []
    wd_host = os.environ.get("WD_EXECUTOR_HOST", os.environ.get("CC_EXECUTOR_HOST", "0.0.0.0"))
    wd_port = os.environ.get("WD_EXECUTOR_PORT", "9101")
    procs.append(
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "wd_server:app",
                "--host",
                wd_host,
                "--port",
                wd_port,
                "--workers",
                "1",
            ],
        ),
    )

    # Even when legacy is disabled, server.py stays up as a deny-only stub so
    # probes against /v1/* and /admin/* receive 404/403 instead of a refused port.
    procs.append(subprocess.Popen([sys.executable, "server.py"]))

    try:
        while True:
            for proc in procs:
                code = proc.poll()
                if code is not None:
                    return code or 1
            time.sleep(0.5)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        deadline = time.time() + 5
        while time.time() < deadline and any(proc.poll() is None for proc in procs):
            time.sleep(0.2)
        for proc in procs:
            if proc.poll() is None:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
