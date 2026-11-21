"""A Devin organization held in memory, for tests that need more than a mock."""

from devin_claude_bridge.bridge import Bridge
from devin_claude_bridge.client import DevinAPIError
from devin_claude_bridge.config import Account, Config
from devin_claude_bridge.state import State


class Org:
    """One Devin organization held in memory."""

    def __init__(self):
        self.sessions = {}
        self.sent = []
        self.calls = []
        self.down = False
        self.status_gone = set()
        self.status_down = False
        self.on_read = None
        self.reads = 0

    def add(self, sid, **fields):
        self.sessions[f"devin-{sid}"] = {
            "session_id": f"devin-{sid}",
            "title": sid.title(),
            "url": f"https://app.devin.ai/sessions/{sid}",
            "status": "running",
            "is_archived": False,
            "updated_at": len(self.sessions) + 1,
            "messages": [],
            **fields,
        }
        return self.sessions[f"devin-{sid}"]

    def say(self, sid, text):
        messages = self.sessions[f"devin-{sid}"]["messages"]
        messages.append({"event_id": f"{sid}-{len(messages)}", "source": "devin", "message": text})

    def _get(self, sid):
        if self.down:
            raise DevinAPIError(500, "Devin API 500: down")
        if sid not in self.sessions:
            raise DevinAPIError(404, "Devin API 404: Session or organization not found")
        return self.sessions[sid]

    async def get_session(self, sid):
        self.calls.append(("get", sid))
        if sid in self.status_gone:
            raise DevinAPIError(410, "Devin API 410: Session unavailable")
        return {k: v for k, v in self._get(sid).items() if k != "messages"}

    async def sessions_by_id(self, sids):
        self.calls.append(("status", tuple(sids)))
        if self.down or self.status_down:
            raise DevinAPIError(500, "Devin API 500: down")
        rows = {sid: self.sessions[sid] for sid in sids if sid in self.sessions}
        return {
            sid: {k: v for k, v in row.items() if k != "messages"}
            for sid, row in rows.items()
            if sid not in self.status_gone
        }

    async def recent_sessions(self, active_only=True):
        if self.down:
            raise DevinAPIError(500, "Devin API 500: down")
        rows = [{k: v for k, v in s.items() if k != "messages"} for s in self.sessions.values()]
        return [s for s in rows if not (active_only and s["is_archived"])]

    async def messages(self, sid, after=None):
        self.calls.append(("messages", sid))
        self.reads += 1
        if self.on_read:
            await self.on_read(self.reads)
        items = self._get(sid)["messages"]
        return items[int(after or 0) :], str(len(items))

    async def send_message(self, sid, message):
        self.sent.append((sid, message))
        messages = self._get(sid)["messages"]
        messages.append(
            {"event_id": f"{sid}-{len(messages)}", "source": "user", "message": message}
        )
        return {k: v for k, v in self._get(sid).items() if k != "messages"}

    async def create_session(self, prompt, title=None):
        return {k: v for k, v in self.add(f"new{len(self.sessions)}", title=title).items()}


def make(tmp_path, orgs, targets=None):
    config = Config({name: Account(f"org-{name}", "key") for name in orgs}, targets or {})
    return Bridge(config, State(tmp_path / "state.json"), lambda account: orgs[account.org_id[4:]])
