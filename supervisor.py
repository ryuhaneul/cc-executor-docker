from __future__ import annotations

import os
import signal
import subprocess
import sys
import time


def main() -> int:
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
