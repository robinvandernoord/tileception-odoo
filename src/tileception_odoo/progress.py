"""Private synchronization progress, independent of the cache's transactions."""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from datetime import UTC, datetime

from .config import Paths


class Progress:
    def __init__(self, paths: Paths):
        self.filename = paths.cache / "progress.json"
        self.last_log = 0.0
        self.data = {
            "state": "running",
            "pid": os.getpid(),
            "started": datetime.now(UTC).isoformat(),
            "phase": "starting",
            "processed_records": 0,
            "total_records": None,
            "completed_chunks": 0,
            "total_chunks": 0,
            "embedded_chunks": 0,
            "reused_chunks": 0,
        }

    def update(self, message: str, force: bool = False, **values):
        phase_changed = values.get("phase", self.data["phase"]) != self.data["phase"]
        self.data.update(values, message=message, updated=datetime.now(UTC).isoformat())
        temporary = self.filename.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.filename)
        now = time.monotonic()
        if force or phase_changed or now - self.last_log >= 2:
            print(message, file=sys.stderr, flush=True)
            self.last_log = now


def read_progress(paths: Paths) -> dict | None:
    try:
        data = json.loads((paths.cache / "progress.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            return None
        if data.get("state") == "running":
            # SIGTERM/SIGKILL cannot reliably write a final progress update. The
            # writer lock tells readers whether a recorded run is still active.
            with (paths.cache / "sync.lock").open("r") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return data
            data["state"] = "interrupted"
            data["message"] = "Synchronization interrupted; saved chunks will be reused on the next run"
        return data
    except OSError, ValueError:
        return None
