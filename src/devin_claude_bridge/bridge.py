import asyncio
import time
from copy import deepcopy

from .client import DevinAPIError, DevinClient
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

    async def list_bots(self, account: str):
        """List root conversations grouped by service user, with the newest session first."""
        sessions = await self.client(account).root_sessions()
        bots = {}
        for session in sorted(sessions, key=lambda s: s["created_at"], reverse=True):
            bot = session.get("service_user_id")
            if bot:
                bots.setdefault(
                    bot,
                    {
                        "service_user_id": bot,
                        "session_id": normalize(session["session_id"]),
                        "title": session.get("title"),
                        "status": session["status"],
                    },
                )
        return {"account": account, "bots": list(bots.values())}

    async def _resolve(self, data, target, account, session_id, force=False, readable=False):
        account, sid = self.config.resolve(target, account, session_id)
        entry = self.config.targets.get(target, {})
        bot = entry.get("bot_service_user_id")
        if not bot:
            return account, sid
        sessions = data.setdefault(account, {})
        cached = next((s for s, p in sessions.items() if target in p.get("targets", [])), None)
        retargeted = bool(
            cached and sessions[cached].get("target_service_user_ids", {}).get(target) != bot
        )
        terminal = False
        if cached and not force and not retargeted:
            try:
                session = await self.client(account).get_session(cached)
                if session["status"] not in {"exit", "error"} and not session.get("is_archived"):
                    return account, cached
                terminal = True
            except DevinAPIError as exc:
                if exc.status_code not in {404, 410}:
                    raise
        roots = await self.client(account).root_sessions(bot)
        roots = [s for s in roots if s["status"] not in {"exit", "error"}]
        if not roots:
            if cached and terminal and readable:
                return account, cached
            raise ValueError(f"Bot target {target}: no active root conversation found")
        sid = normalize(max(roots, key=lambda s: s["created_at"])["session_id"])
        if cached and cached != sid:
            previous = sessions[cached]
            previous["targets"].remove(target)
        position = sessions.setdefault(sid, {"cursor": None, "event_id": None, "direct": False})
        position.setdefault("direct", not position.get("targets"))
        if cached and cached != sid and not retargeted:
            pending = position.setdefault("previous_sessions", [])
            for previous_sid in [*sessions[cached].get("previous_sessions", []), cached]:
                if previous_sid not in pending and previous_sid != sid:
                    pending.append(previous_sid)
        if target not in position.setdefault("targets", []):
            position["targets"].append(target)
        position.setdefault("target_service_user_ids", {})[target] = bot
        if cached and cached != sid and retargeted:
            previous = sessions[cached]
            if not previous.get("targets") and not previous.get("direct", False):
                del sessions[cached]
        return account, sid

    async def _operate(self, data, target, account, session_id, operation, readable=False):
        unavailable = set()

        async def run(name, sid):
            if not readable:
                return await operation(name, sid)
            sessions = data.setdefault(name, {})
            position = sessions.get(sid, {})
            messages = []
            pending = list(position.get("previous_sessions", []))
            for previous_sid in pending:
                if previous_sid in unavailable:
                    continue
                try:
                    replies = await operation(name, previous_sid)
                    messages.extend({**m, "session_id": previous_sid} for m in replies)
                except DevinAPIError as exc:
                    if exc.status_code not in {404, 410}:
                        raise
            messages.extend(await operation(name, sid))
            position.pop("previous_sessions", None)
            for previous_sid in pending:
                previous = sessions.get(previous_sid, {})
                if not previous.get("targets") and not previous.get("direct", False):
                    sessions.pop(previous_sid, None)
            return messages

        name, sid = await self._resolve(data, target, account, session_id, readable=readable)
        try:
            return name, sid, await run(name, sid)
        except DevinAPIError as exc:
            if exc.status_code not in {404, 410} or not self.config.targets.get(target, {}).get(
                "bot_service_user_id"
            ):
                raise
            unavailable.add(sid)
        name, sid = await self._resolve(data, target, account, session_id, force=True)
        return name, sid, await run(name, sid)

    async def create_session(self, account: str, prompt: str, title: str | None = None):
        """Create and automatically watch a new Devin session."""
        result = await self.client(account).create_session(prompt, title)
        await self.watch_session(account=account, session_id=result["session_id"])
        return result

    async def watch_session(
        self, target: str | None = None, account: str | None = None, session_id: str | None = None
    ):
        """Watch a session. Its existing messages are initially unread."""
        async with self.state.transaction() as data:
            account, sid = await self._resolve(data, target, account, session_id)
            position = data.setdefault(account, {}).setdefault(
                sid, {"cursor": None, "event_id": None}
            )
            if not self.config.targets.get(target, {}).get("bot_service_user_id"):
                position["direct"] = True
        return {"account": account, "session_id": sid, "watched": True}

    async def unwatch_session(
        self, target: str | None = None, account: str | None = None, session_id: str | None = None
    ):
        """Stop watching and forget the last-seen position."""
        async with self.state.transaction() as data:
            if target and target not in self.config.targets and not account and not session_id:
                removed = []
                for name, sessions in data.items():
                    for sid, position in list(sessions.items()):
                        if target not in position.get("targets", []):
                            continue
                        position["targets"].remove(target)
                        position.get("target_service_user_ids", {}).pop(target, None)
                        if not position["targets"] and not position.get("direct", False):
                            del sessions[sid]
                        removed.append({"account": name, "session_id": sid})
                if not removed:
                    raise ValueError(f"Unknown target: {target}")
                return {"target": target, "watched": False, "removed_sessions": removed}
            account, sid = self.config.resolve(target, account, session_id)
            sessions = data.get(account, {})
            if self.config.targets.get(target, {}).get("bot_service_user_id"):
                sid = next((s for s, p in sessions.items() if target in p.get("targets", [])), None)
                if sid:
                    sessions[sid]["targets"].remove(target)
                    sessions[sid].get("target_service_user_ids", {}).pop(target, None)
                    if not sessions[sid]["targets"] and not sessions[sid].get("direct", False):
                        del sessions[sid]
            else:
                sessions.pop(sid, None)
        return {"account": account, "session_id": sid, "watched": False}

    async def send_message(
        self,
        message: str,
        target: str | None = None,
        account: str | None = None,
        session_id: str | None = None,
    ):
        """Send to a target alias OR an account/session pair; automatically watch it."""
        async with self.state.transaction() as data:
            account, sid, result = await self._operate(
                data,
                target,
                account,
                session_id,
                lambda name, sid: self.client(name).send_message(sid, message),
            )
            position = data.setdefault(account, {}).setdefault(
                sid, {"cursor": None, "event_id": None}
            )
            if not self.config.targets.get(target, {}).get("bot_service_user_id"):
                position["direct"] = True
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
        async with self.state.transaction() as data:
            account, sid, messages = await self._operate(
                data,
                target,
                account,
                session_id,
                lambda name, sid: self._read(data, name, sid, only_new, include_user),
                readable=True,
            )
            if not self.config.targets.get(target, {}).get("bot_service_user_id"):
                data[account][sid]["direct"] = True
        return {"account": account, "session_id": sid, "messages": messages}

    async def check_inbox(self, account: str | None = None):
        """Read new Devin messages across watched sessions; isolate errors per session."""
        if account:
            self.client(account)
        groups = []
        skipped = []
        async with self.state.transaction() as data:
            work = []
            for name, sessions in data.items():
                if account and name != account:
                    continue
                for sid, position in sessions.items():
                    for alias in position.get("targets", []):
                        entry = self.config.targets.get(alias, {})
                        if entry.get("bot_service_user_id") and entry.get("account") == name:
                            work.append((alias, None, None))
                        else:
                            skipped.append(
                                {
                                    "target": alias,
                                    "account": name,
                                    "session_id": sid,
                                    "reason": "Alias removed or reconfigured; unwatch to forget it",
                                }
                            )
                    if position.get("direct", not position.get("targets")):
                        work.append((None, name, sid))
            for target, name, sid in work:
                group = {"target": target} if target else {"account": name, "session_id": sid}
                before = deepcopy(data)
                try:
                    name, sid, messages = await self._operate(
                        data, target, name, sid, lambda n, s: self._read(data, n, s), readable=True
                    )
                    if messages:
                        groups.append(
                            {**group, "account": name, "session_id": sid, "messages": messages}
                        )
                except ValueError as exc:
                    data.clear()
                    data.update(before)
                    groups.append({**group, "error": str(exc)})
        return {"sessions": groups, **({"skipped_targets": skipped} if skipped else {})}

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
        deadline = time.monotonic() + timeout_seconds
        result = await self.read_messages(target=target, account=account, session_id=session_id)
        while time.monotonic() < deadline:
            await asyncio.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))
            result = await self.read_messages(target=target, account=account, session_id=session_id)
            if result["messages"]:
                return {**result, "timed_out": False}
        return {**result, "messages": [], "timed_out": True}
