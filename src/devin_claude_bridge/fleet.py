"""Who holds which Devin machine, and who waits for one.

Several Claude sessions share the same Devin machines. Without a record of who
is using which, one session sends a new task to a machine that is in the middle
of another session's work, and both jobs are spoiled. The rule:

* A message that starts work goes only to a free machine.
* A busy machine gets only a follow-up, from the session that holds it, on the
  same piece of work.
* With no free machine of the needed kind, the task waits in line; the first in
  line gets the next machine of that kind that is released.

Machines come in two kinds: `neon` (Devin with Gemini, Linux only, at most four)
for forms work that needs reading pictures and video, and the free machines by
platform: `linux`, `macos` and `windows`.

The record is `fleet.json` in the state file's folder, the file the
`devin-fleet` script kept before this module existed, so claims made with it
carry over. Its format is unchanged: `machines` (alias -> platform, pool,
extras, holder, task, since), `queue` (tickets n, need, by, task, at) and `next`.
"""

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

PLATFORMS = ("linux", "macos", "windows")
NEEDS = ("neon", *PLATFORMS)
NEON_CAP = 4
# Devin is doing something: a new task now would interrupt it.
WORKING = ("running", "claimed", "resuming")
# Devin cannot be messaged at all, or the bridge could not find out.
UNREACHABLE = ("ended", "unknown")

RULE = (
    "Rule: a message that starts work goes only to a free machine; a busy machine gets "
    "only a follow-up from the session that holds it, on the same work; with no free "
    "machine of the needed kind, queue and the first in line gets the next one released."
)


def fleet_path(state_path) -> Path:
    """`fleet.json` beside the state file, where the devin-fleet script kept it."""
    return Path(state_path).with_name("fleet.json")


def guess(alias):
    """Platform and pool for a machine nobody described yet, from its name."""
    if alias.startswith("neon"):
        return "linux", "neon"
    for word, platform in (("macos", "macos"), ("win", "windows"), ("linux", "linux")):
        if word in alias:
            return platform, "free"
    return "?", "free"


def kind(machine):
    """What a machine can be asked for: Neon machines only as neon, the rest by platform."""
    return "neon" if machine["pool"] == "neon" else machine["platform"]


def working(status, detail):
    """Whether Devin reports the machine busy. Waiting for the user is not working."""
    return status in WORKING and detail != "waiting_for_user"


def is_free(machine):
    """Nobody holds it, Devin is not working on it, and it can be messaged."""
    return (
        not machine["holder"]
        and not working(machine["status"], machine["detail"])
        and machine["status"] not in UNREACHABLE
    )


def minutes(since, now=None):
    return int(((now or time.time()) - since) // 60) if since else None


def first_line(text, limit=200):
    """The task a message stands for: its first non-empty line, trimmed."""
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def view(saved, statuses):
    """alias -> machine: what fleet.json says merged with Devin's live status."""
    machines = {}
    for alias, (status, detail) in statuses.items():
        entry = saved["machines"].get(alias, {})
        platform, pool = guess(alias)
        machines[alias] = {
            "platform": entry.get("platform") or platform,
            "pool": entry.get("pool") or pool,
            "extras": entry.get("extras", ""),
            "holder": entry.get("holder") or "",
            "task": entry.get("task") or "",
            "since": entry.get("since"),
            # Claimed and not messaged since: the next message may start work.
            "fresh": bool(entry.get("holder")) and entry.get("sent_since") != entry.get("since"),
            "status": status,
            "detail": detail,
        }
    return machines


def ahead_of(queue, need, holder):
    """The first ticket for `need` that is not `holder`'s and is ahead of `holder`'s own."""
    tickets = [t for t in queue if t["need"] == need]
    for ticket in tickets:
        if ticket["by"] == holder:
            return None
        return ticket
    return None


class FleetStore:
    """fleet.json under a lock, the same way as the aliases file."""

    def __init__(self, path):
        self.path = Path(path)

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"machines": {}, "queue": [], "next": 1}
        valid = (
            isinstance(data, dict)
            and isinstance(data.get("machines", {}), dict)
            and all(isinstance(m, dict) for m in data.get("machines", {}).values())
            and isinstance(data.get("queue", []), list)
            and all(
                isinstance(t, dict) and isinstance(t.get("n"), int) and t.get("need") in NEEDS
                for t in data.get("queue", [])
            )
            and isinstance(data.get("next", 1), int)
        )
        if not valid:
            raise ValueError(f"Invalid fleet file: {self.path}")
        queue = data.get("queue", [])
        top = max((t["n"] for t in queue), default=0)
        return {
            "machines": data.get("machines", {}),
            "queue": queue,
            "next": max(data.get("next", 1), top + 1),
        }

    @contextmanager
    def edit(self):
        """Change the file under a lock. Never await inside: the lock blocks."""
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self.read()
                yield data
                fd, name = tempfile.mkstemp(dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(data, stream, indent=1, ensure_ascii=False)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


def clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def said(status, detail):
    """Devin's status in few characters: a running session by its detail alone."""
    if status == "running" and detail:
        return detail
    return " ".join(x for x in (status, detail) if x) or "?"


def row(alias, machine, now=None):
    """One line per machine, so 25 of them stay a few thousand characters."""
    held = minutes(machine["since"], now)
    task = clip(machine["task"], 40) or "-"
    if machine["extras"]:
        task += f" [{clip(machine['extras'], 16)}]"
    return " | ".join(
        [
            alias,
            kind(machine),
            said(machine["status"], machine["detail"]),
            "free" if is_free(machine) else "busy",
            machine["holder"] or "-",
            f"{held}m" if held is not None else "-",
            task,
        ]
    )


def ticket_row(ticket, now=None):
    waited = minutes(ticket.get("at"), now)
    return (
        f"#{ticket['n']} {ticket['need']} {ticket.get('by', '?')}"
        f" {waited if waited is not None else '?'}m: {clip(ticket.get('task', ''), 40)}"
    )
