"""Aliases added at run time, kept beside the state file.

`config.toml` stays the user's own file. Aliases made with `add_target` live in
`targets.json`, together with a note of which aliases point at a session that
is archived or has ended, so that is reported once and not on every poll.
"""

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .config import session_id

ALIAS = re.compile(r"[A-Za-z0-9_-]+")


def session_from(value: str) -> str:
    """The session ID in a Devin session URL, or in a bare ID with or without `devin-`."""
    value = value.strip()
    if "/" in value:
        path = urlsplit(value).path if "://" in value else value.split("?")[0].split("#")[0]
        parts = [part for part in path.split("/") if part]
        if "sessions" in parts[:-1]:
            value = parts[parts.index("sessions") + 1]
        elif parts:
            value = parts[-1]
        else:
            raise ValueError("No session ID in that URL")
    return session_id(value)


def ended(session) -> str | None:
    """Why a session can no longer be messaged, or None while it can.

    A suspended session is not ended: Devin resumes it when a message arrives.
    """
    if session.get("is_archived") is True:
        return "archived"
    if session.get("status") in ("exit", "error"):
        return "ended"
    return None


class TargetStore:
    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"targets": {}, "stale": {}}
        valid = (
            isinstance(data, dict)
            and isinstance(data.get("targets", {}), dict)
            and isinstance(data.get("stale", {}), dict)
            and all(
                isinstance(entry, dict)
                and isinstance(entry.get("account"), str)
                and isinstance(entry.get("session_id"), str)
                for entry in data.get("targets", {}).values()
            )
            and all(isinstance(entry, dict) for entry in data.get("stale", {}).values())
        )
        if not valid:
            raise ValueError(f"Invalid targets file: {self.path}")
        return {"targets": data.get("targets", {}), "stale": data.get("stale", {})}

    @contextmanager
    def edit(self):
        """Change the file under a lock. Never await inside: the lock blocks."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self.read()
                yield data
                fd, name = tempfile.mkstemp(dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(data, stream, indent=2)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
