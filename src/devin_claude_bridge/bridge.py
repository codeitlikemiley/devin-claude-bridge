import asyncio
import time

from .client import DevinClient
from .config import session_id as normalize
from .state import State


class Bridge:
    def __init__(self, config, state=None, client_factory=DevinClient):
        self.config = config
        self.state = state or State()
        self.clients = {name: client_factory(a) for name, a in config.accounts.items()}

    def client(self, account):
        if account not in self.clients:
            raise ValueError(f"Unknown account: {account}")
        return self.clients[account]

    def list_accounts(self):
        """List account names, organization IDs and named targets; never credentials."""
        return {
            "accounts": [{"name": n, "org_id": a.org_id} for n, a in self.config.accounts.items()],
            "targets": self.config.targets,
        }

    async def list_sessions(self, account: str, limit: int = 100):
        """List up to 200 sessions in an account."""
        return await self.client(account).list_sessions(limit)

    async def create_session(self, account: str, prompt: str, title: str | None = None):
        """Create and automatically watch a new Devin session."""
        result = await self.client(account).create_session(prompt, title)
        await self.watch_session(account=account, session_id=result["session_id"])
        return result

    async def watch_session(
        self, target: str | None = None, account: str | None = None, session_id: str | None = None
    ):
        """Watch a session. Its existing messages are initially unread."""
        account, sid = self.config.resolve(target, account, session_id)
        async with self.state.transaction() as data:
            data.setdefault(account, {}).setdefault(sid, {"cursor": None, "event_id": None})
        return {"account": account, "session_id": sid, "watched": True}

    async def unwatch_session(
        self, target: str | None = None, account: str | None = None, session_id: str | None = None
    ):
        """Stop watching and forget the last-seen position."""
        account, sid = self.config.resolve(target, account, session_id)
        async with self.state.transaction() as data:
            data.get(account, {}).pop(sid, None)
        return {"account": account, "session_id": sid, "watched": False}

    async def send_message(
        self,
        message: str,
        target: str | None = None,
        account: str | None = None,
        session_id: str | None = None,
    ):
        """Send to a target alias OR an account/session pair; automatically watch it."""
        account, sid = self.config.resolve(target, account, session_id)
        result = await self.client(account).send_message(sid, message)
        await self.watch_session(account=account, session_id=sid)
        return result

    async def _read(self, data, account, sid, only_new=True, include_user=False):
        position = data.setdefault(account, {}).setdefault(
            normalize(sid), {"cursor": None, "event_id": None}
        )
        items, cursor = await self.client(account).messages(
            sid, position.get("cursor") if only_new else None
        )
        if only_new and position.get("event_id"):
            for index, item in enumerate(items):
                if item["event_id"] == position["event_id"]:
                    items = items[index + 1 :]
                    break
        if cursor:
            position["cursor"] = cursor
        if items:
            position["event_id"] = items[-1]["event_id"]
        return [m for m in items if include_user or m["source"] == "devin"]

    async def read_messages(
        self,
        target: str | None = None,
        account: str | None = None,
        session_id: str | None = None,
        only_new: bool = True,
        include_user: bool = False,
    ):
        """Read all pages, advance last-seen position, and watch. Default: new Devin replies."""
        account, sid = self.config.resolve(target, account, session_id)
        async with self.state.transaction() as data:
            messages = await self._read(data, account, sid, only_new, include_user)
        return {"account": account, "session_id": sid, "messages": messages}

    async def check_inbox(self, account: str | None = None):
        """Read new Devin messages across watched sessions; isolate errors per session."""
        if account:
            self.client(account)
        groups = []
        async with self.state.transaction() as data:
            for name, sessions in data.items():
                if account and name != account:
                    continue
                for sid in sessions:
                    group = {"account": name, "session_id": sid}
                    try:
                        messages = await self._read(data, name, sid)
                        if messages:
                            groups.append({**group, "messages": messages})
                    except ValueError as exc:
                        groups.append({**group, "error": str(exc)})
        return {"sessions": groups}

    async def wait_for_reply(
        self,
        target: str | None = None,
        account: str | None = None,
        session_id: str | None = None,
        timeout_seconds: float = 300,
        poll_seconds: float = 15,
    ):
        """Mark existing messages seen, then wait for new Devin replies (not old unread ones)."""
        if not 0 <= timeout_seconds <= 3600 or not 0 < poll_seconds <= 300:
            raise ValueError("timeout_seconds must be 0..3600; poll_seconds must be >0 and <=300")
        account, sid = self.config.resolve(target, account, session_id)
        deadline = time.monotonic() + timeout_seconds
        await self.read_messages(account=account, session_id=sid)
        while time.monotonic() < deadline:
            await asyncio.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                result = await asyncio.wait_for(
                    self.read_messages(account=account, session_id=sid), timeout=remaining
                )
            except asyncio.TimeoutError:
                break
            if result["messages"]:
                return {**result, "timed_out": False}
        return {"account": account, "session_id": sid, "messages": [], "timed_out": True}
