import asyncio
import fcntl
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


class State:
    def __init__(self, path=None):
        self.path = Path(
            path or os.getenv("DEVIN_BRIDGE_STATE", "~/.local/state/devin-claude-bridge/state.json")
        ).expanduser()

    @asynccontextmanager
    async def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_suffix(".lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            try:
                data = json.loads(self.path.read_text()) if self.path.exists() else {}
                if not isinstance(data, dict) or any(
                    not isinstance(sessions, dict)
                    or any(
                        not isinstance(position, dict)
                        or any(
                            position.get(key) is not None and not isinstance(position[key], str)
                            for key in ("cursor", "event_id")
                        )
                        for position in sessions.values()
                    )
                    for sessions in data.values()
                ):
                    raise ValueError("Invalid state file: expected accounts, sessions and cursors")
                yield data
                fd, name = tempfile.mkstemp(dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(data, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
