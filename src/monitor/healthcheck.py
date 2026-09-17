"""Liveness of the monitor itself, without running an HTTP server.

The monitor rewrites a heartbeat file after every cycle. The Docker healthcheck runs
``python -m monitor.healthcheck``, which fails if the heartbeat is missing or stale
(i.e. the loop is stuck or dead, not just the process alive).
"""

import json
import os
import sys
import time
from pathlib import Path

GRACE_SECONDS = 120.0


def write_heartbeat(path: Path, interval: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "interval": interval}), encoding="utf-8")
    tmp.replace(path)


def check_heartbeat(path: Path, now: float | None = None) -> tuple[bool, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ts, interval = float(data["ts"]), float(data["interval"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, f"heartbeat unreadable: {exc}"
    age = (time.time() if now is None else now) - ts
    max_age = interval * 2 + GRACE_SECONDS
    if age > max_age:
        return False, f"heartbeat is stale: {age:.0f}s old (max {max_age:.0f}s)"
    return True, f"ok: last heartbeat {age:.0f}s ago"


def main() -> None:
    path = Path(os.environ.get("HEARTBEAT_PATH") or "data/heartbeat.json")
    healthy, message = check_heartbeat(path)
    print(message)
    sys.exit(0 if healthy else 1)


if __name__ == "__main__":
    main()
