import asyncio
import time
from copy import deepcopy
from datetime import datetime, timezone

from .client import DevinAPIError, DevinClient
from .config import session_id as normalize
from .state import State
from .targets import ALIAS, TargetStore, ended, session_from, store_path

# Devin has stopped working and the next move is the user's.
IDLE_DETAILS = ("waiting_for_user", "waiting_for_approval", "finished")


def _what(session, reason):
    if reason == "archived":
        return "is archived"
    if reason == "not found":
        return "no longer exists"
    return f"has ended (status {session.get('status')})"


def _named(session):
    """A session's title and URL for a message, as far as the API gave them."""
    title, url = session.get("title"), session.get("url")
    parts = [f'"{title}"' if isinstance(title, str) and title else "", url if url else ""]
    return " ".join(str(part) for part in parts if part)


def _brief(account, session, aliases=()):
    updated = session.get("updated_at")
    row = {
        "account": account,
        "session_id": normalize(session["session_id"]),
        "title": session.get("title"),
        "status": session.get("status"),
        "status_detail": session.get("status_detail"),
        "is_archived": session.get("is_archived") is True,
        "updated_at": updated,
        "url": session.get("url"),
    }
    if isinstance(updated, (int, float)):
        stamp = datetime.fromtimestamp(updated, timezone.utc)
        row["updated"] = stamp.strftime("%Y-%m-%d %H:%M UTC")
    if session.get("parent_session_id"):
        row["child_of"] = normalize(session["parent_session_id"])
    if aliases:
        row["targets"] = sorted(aliases)
    return row


class Bridge:
    def __init__(self, config, state=None, client_factory=DevinClient, targets=None):
        self.config = config
        self.state = state or State()
        self.store = targets or TargetStore(store_path(self.state.path))
        self.file_targets = dict(config.targets)
        self.clients = {name: client_factory(a) for name, a in config.accounts.items()}

    def _refresh(self):
        """Put the aliases made with add_target beside the config file's targets."""
        saved = self.store.read()
        merged = dict(self.file_targets)
        for alias, entry in saved["targets"].items():
            merged.setdefault(alias, {**entry, "source": "added"})
        self.config.targets = merged
        return saved

    def _bot(self, target):
        return self.config.targets.get(target, {}).get("bot_service_user_id")

    def _aliases(self):
        """Target names by the (account, session) they are pinned to."""
        pinned = {}
        for alias, entry in self.config.targets.items():
            if entry.get("session_id"):
                pinned.setdefault((entry["account"], entry["session_id"]), []).append(alias)
        return pinned

    def _note_stale(self, alias, sid, reason):
        """Record, or clear, that an alias points at a session that cannot be messaged."""
        current = self.store.read()["stale"].get(alias)
        wanted = {"session_id": sid, "reason": reason} if reason else None
        if current == wanted:
            return
        with self.store.edit() as saved:
            if wanted:
                saved["stale"][alias] = wanted
            else:
                saved["stale"].pop(alias, None)

    async def _active_lines(self, account, limit=5):
        try:
            sessions = await self.client(account).recent_sessions(True)
            rows = [s for s in sessions if not ended(s)]
            rows.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
        except (ValueError, TypeError):
            return []
        lines = [f"- {_named(s) or normalize(s['session_id'])}" for s in rows[:limit]]
        return [f"Active sessions on account {account}:", *lines] if lines else []

    async def _require_live(self, target, account, sid):
        """Refuse to message a session that is archived or has ended.

        Its watch is then marked to end once the inbox has read its last replies.
        """
        session = await self.client(account).get_session(sid)
        reason = ended(session)
        if target:
            self._note_stale(target, sid, reason)
        if not reason:
            return
        async with self.state.transaction() as data:
            if sid in data.get(account, {}):
                data[account][sid]["retire"] = True
        named = _named(session)
        if target:
            head = f"Target {target} points to a session that {_what(session, reason)}"
            advice = (
                f'Re-point it with add_target("{target}", <session URL>), or start a new '
                f'session with create_session(..., alias="{target}").'
            )
        else:
            head = f"Session {sid} on account {account} {_what(session, reason)}"
            advice = "Pick another session."
        lines = [f"{head}{': ' + named if named else ''}. Nothing was sent. {advice}"]
        raise ValueError("\n".join(lines + await self._active_lines(account)))

    async def _dead(self, account, sids):
        """Which of these sessions are archived, ended or gone: {sid: (session, reason)}."""
        client = self.client(account)
        try:
            found = await client.sessions_by_id(sids)
        except DevinAPIError as exc:
            # A key that may read sessions but not list them: ask one by one.
            if exc.status_code != 403:
                raise
            found = {}

        async def one(sid):
            # Absent from the list. Ask for it by itself before calling it gone.
            try:
                return await client.get_session(sid)
            except DevinAPIError as exc:
                return "not found" if exc.status_code in {404, 410} else None
            except ValueError:
                return None

        missing = [sid for sid in sids if sid not in found]
        for sid, answer in zip(missing, await asyncio.gather(*(one(sid) for sid in missing))):
            found[sid] = answer
        dead = {}
        for sid in sids:
            info = found[sid]
            if info == "not found":
                dead[sid] = ({}, "not found")
            elif info is not None and ended(info):
                dead[sid] = (info, ended(info))
        return dead

    @staticmethod
    def _drop_watch(data, account, sid):
        """Stop watching a session directly; a Bot alias on it keeps its entry."""
        position = data.get(account, {}).get(sid)
        if position is None:
            return False
        if position.get("targets"):
            position["direct"] = False
        else:
            del data[account][sid]
        return True

    def client(self, account):
        if account not in self.clients:
            raise ValueError(f"Unknown account: {account}")
        return self.clients[account]

    def list_accounts(self):
        """List account names, organization IDs and named targets; never credentials.

        A target marked `stale` points at a session that is archived or has ended.
        """
        stale = self._refresh()["stale"]
        targets = {}
        for alias, entry in self.config.targets.items():
            mark = stale.get(alias, {})
            pinned = entry.get("session_id") and mark.get("session_id") == entry["session_id"]
            targets[alias] = {**entry, **({"stale": mark["reason"]} if pinned else {})}
        return {
            "accounts": [{"name": n, "org_id": a.org_id} for n, a in self.config.accounts.items()],
            "targets": targets,
        }

    async def list_sessions(
        self, account: str | None = None, active_only: bool = True, limit: int = 20
    ):
        """List recent sessions, newest activity first, in every account or in one.

        By default only sessions that can still be messaged: not archived and not
        ended. Each one has its title, status, URL and the targets pinned to it; a
        session another session started also has `child_of`, its parent's ID.
        `limit` is per account, 1 to 200, taken from that account's newest 200.
        """
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        self._refresh()
        names = [account] if account else list(self.clients)
        calls = [self.client(name).recent_sessions(active_only) for name in names]
        results = await asyncio.gather(*calls, return_exceptions=True)
        pinned = self._aliases()
        sessions, errors = [], []
        for name, result in zip(names, results):
            if isinstance(result, ValueError):
                errors.append({"account": name, "error": str(result)})
                continue
            if isinstance(result, BaseException):
                raise result
            rows = [s for s in result if not (active_only and ended(s))]
            rows.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
            for session in rows[:limit]:
                key = (name, normalize(session["session_id"]))
                sessions.append(_brief(name, session, pinned.get(key, ())))
        sessions.sort(key=lambda s: s["updated_at"] or 0, reverse=True)
        return {"sessions": sessions, **({"errors": errors} if errors else {})}

    def _check_alias(self, alias):
        if not ALIAS.fullmatch(alias):
            raise ValueError("alias: use letters, digits, - and _")
        if alias in self.file_targets:
            raise ValueError(f"Target {alias} is defined in config.toml; change it there")

    async def _point(self, alias, account, sid, info, description):
        """Save the alias; tidy up the session it pointed at before, if that one is dead."""
        with self.store.edit() as saved:
            previous = saved["targets"].get(alias)
            saved["targets"][alias] = {
                "account": account,
                "session_id": sid,
                "title": info.get("title"),
                "url": info.get("url"),
                "description": description or (previous or {}).get("description", ""),
            }
            saved["stale"].pop(alias, None)
        self._refresh()
        if not previous or (previous["account"], previous["session_id"]) == (account, sid):
            return None
        old_account, old_sid = previous["account"], previous["session_id"]
        watch = "kept"
        if old_account in self.clients and (old_account, old_sid) not in self._aliases():
            try:
                dead = ended(await self.client(old_account).get_session(old_sid))
            except DevinAPIError as exc:
                dead = "not found" if exc.status_code in {404, 410} else None
            except ValueError:
                dead = None
            async with self.state.transaction() as data:
                if dead and old_sid in data.get(old_account, {}):
                    data[old_account][old_sid]["retire"] = True
                    watch = "ends after its last replies are read"
        return {"account": old_account, "session_id": old_sid, "watch": watch}

    async def add_target(
        self,
        alias: str,
        session: str,
        account: str | None = None,
        description: str | None = None,
    ):
        """Give one session a short name, from its Devin session URL or its session ID.

        Afterwards target=<alias> works wherever a target is accepted. Unless you
        name the account, each configured account is asked for the session. An
        archived or ended session is refused. Calling this again with the same
        alias moves it to the new session. A session that was not watched yet is
        watched from now on: the inbox shows replies that arrive after this call,
        not the earlier history. One that was already watched keeps its unread
        replies.
        """
        self._refresh()
        self._check_alias(alias)
        sid = session_from(session)
        if account:
            self.client(account)
        names = [account] if account else list(self.clients)
        found, failures = None, []
        for name in names:
            try:
                found = (name, await self.client(name).get_session(sid))
                break
            except ValueError as exc:
                failures.append(f"{name}: {exc}")
        if not found:
            raise ValueError(f"No configured account can open {sid}. " + "; ".join(failures))
        name, info = found
        reason = ended(info)
        if reason:
            named = _named(info)
            head = f"Session {sid} on account {name} {_what(info, reason)}"
            lines = [f"{head}{': ' + named if named else ''}. It cannot be a target."]
            raise ValueError("\n".join(lines + await self._active_lines(name)))
        async with self.state.transaction() as data:
            position = data.setdefault(name, {}).get(sid)
            if position is None:
                items, cursor = await self.client(name).messages(sid)
                data[name][sid] = {
                    "cursor": cursor,
                    "event_id": items[-1]["event_id"] if items else None,
                    "direct": True,
                    "answered": bool(items) and items[-1]["source"] == "devin",
                }
            else:
                # The session was just seen alive, so an earlier mark to end its
                # watch no longer applies.
                position["direct"] = True
                position.pop("retire", None)
        replaced = await self._point(alias, name, sid, info, description)
        return {
            "target": alias,
            "account": name,
            "session_id": sid,
            "title": info.get("title"),
            "url": info.get("url"),
            "watched": True,
            **({"replaced": replaced} if replaced else {}),
        }

    async def remove_target(self, alias: str, unwatch: bool = True):
        """Forget an alias made with add_target; by default also stop watching its session."""
        saved = self._refresh()
        if alias in self.file_targets:
            raise ValueError(f"Target {alias} is defined in config.toml; remove it there")
        if alias not in saved["targets"]:
            raise ValueError(f"Unknown target: {alias}")
        with self.store.edit() as data:
            entry = data["targets"].pop(alias)
            data["stale"].pop(alias, None)
        self._refresh()
        unwatched = False
        if unwatch and (entry["account"], entry["session_id"]) not in self._aliases():
            async with self.state.transaction() as data:
                unwatched = self._drop_watch(data, entry["account"], entry["session_id"])
        return {
            "target": alias,
            "removed": True,
            "account": entry["account"],
            "session_id": entry["session_id"],
            "unwatched": unwatched,
        }

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
            if not readable or not self.config.targets.get(target, {}).get("bot_service_user_id"):
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
        before = deepcopy(data) if readable else None
        try:
            return name, sid, await run(name, sid)
        except DevinAPIError as exc:
            if exc.status_code not in {404, 410} or not self.config.targets.get(target, {}).get(
                "bot_service_user_id"
            ):
                raise
            if before is not None:
                data.clear()
                data.update(before)
            unavailable.add(sid)
        name, sid = await self._resolve(data, target, account, session_id, force=True)
        return name, sid, await run(name, sid)

    async def create_session(
        self, account: str, prompt: str, title: str | None = None, alias: str | None = None
    ):
        """Create and automatically watch a new Devin session.

        With `alias`, the new session also becomes that target, and an existing
        alias of that name moves to it.
        """
        self._refresh()
        if alias is not None:
            self._check_alias(alias)
        result = await self.client(account).create_session(prompt, title)
        await self.watch_session(account=account, session_id=result["session_id"])
        if alias is None:
            return result
        sid = normalize(result["session_id"])
        replaced = await self._point(alias, account, sid, result, None)
        return {**result, "target": alias, **({"replaced": replaced} if replaced else {})}

    async def watch_session(
        self, target: str | None = None, account: str | None = None, session_id: str | None = None
    ):
        """Watch a session. Its existing messages are initially unread."""
        self._refresh()
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
        self._refresh()
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
        """Send to a target alias OR an account/session pair; automatically watch it.

        Nothing is sent to a session that is archived or has ended; the error then
        lists the account's active sessions. A suspended session is resumed.
        """
        self._refresh()
        if not self._bot(target):
            # Resolve a pinned target once: the session that passes the check is the
            # one that gets the message, even if the alias is moved meanwhile.
            account, session_id = self.config.resolve(target, account, session_id)
            await self._require_live(target, account, session_id)
            target = None
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
            position.pop("retire", None)
            position["answered"] = False
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
            # Whether Devin has answered the latest user message, kept because the
            # messages that show it stop being unread once they are read.
            position["answered"] = items[-1]["source"] == "devin"
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
        self._refresh()
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
        """Read new Devin messages across watched sessions; isolate errors per session.

        `notices` names each watched session that was archived, has ended or is
        gone, with the targets pinned to it. Its last replies are delivered here,
        then it is no longer watched, so the notice appears once. One request per
        account reads the status of all its watched sessions.
        """
        if account:
            self.client(account)
        stale = self._refresh()["stale"]
        pinned = self._aliases()
        groups = []
        skipped = []
        notices = []
        newly_stale = {}
        gone = set()
        async with self.state.transaction() as data:
            # A session's status is read before its messages, so the read that
            # follows covers everything a session seen as archived or ended said.
            watched = {}
            for name, sessions in data.items():
                if name not in self.clients or (account and name != account):
                    continue
                sids = [
                    sid
                    for sid, position in sessions.items()
                    if position.get("direct", not position.get("targets"))
                    and not position.get("retire")
                ]
                if sids:
                    watched[name] = sids
            calls = [self._dead(name, sids) for name, sids in watched.items()]
            answers = await asyncio.gather(*calls, return_exceptions=True)
            for name, answer in zip(watched, answers):
                if isinstance(answer, ValueError):
                    continue
                if isinstance(answer, BaseException):
                    raise answer
                for sid, (info, reason) in answer.items():
                    data[name][sid]["retire"] = True
                    named = _named(info)
                    said = f"{_what(info, reason)}{': ' + named if named else ''}"
                    aliases = sorted(pinned.get((name, sid), []))
                    for alias in aliases:
                        if stale.get(alias, {}).get("session_id") == sid:
                            continue
                        newly_stale[alias] = {"session_id": sid, "reason": reason}
                        notices.append(
                            {
                                "target": alias,
                                "account": name,
                                "session_id": sid,
                                "reason": reason,
                                "message": (
                                    f"Target {alias}: its session {said}. Re-point it with "
                                    f'add_target("{alias}", <session URL>) or '
                                    f'create_session(..., alias="{alias}").'
                                ),
                            }
                        )
                    if not aliases:
                        notices.append(
                            {
                                "account": name,
                                "session_id": sid,
                                "reason": reason,
                                "message": f"Session {sid} {said}. It is no longer watched.",
                            }
                        )
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
                if not target and pinned.get((name, sid)):
                    group["targets"] = sorted(pinned[(name, sid)])
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
                    unreadable = isinstance(exc, DevinAPIError) and exc.status_code in {404, 410}
                    if unreadable and not target:
                        gone.add((name, sid))
            # A watch marked to end goes once its last replies were read, or once
            # Devin says its history is gone too. Any other failed read keeps it.
            failed = {(g.get("account"), g.get("session_id")) for g in groups if "error" in g}
            for name, sessions in data.items():
                for sid, position in list(sessions.items()):
                    if not position.get("retire") or (account and name != account):
                        continue
                    if (name, sid) in gone:
                        groups[:] = [
                            g
                            for g in groups
                            if "error" not in g
                            or (g.get("account"), g.get("session_id")) != (name, sid)
                        ]
                    elif (name, sid) in failed:
                        continue
                    del position["retire"]
                    self._drop_watch(data, name, sid)
        if newly_stale:
            with self.store.edit() as saved:
                saved["stale"].update(newly_stale)
        return {
            "sessions": groups,
            **({"notices": notices} if notices else {}),
            **({"skipped_targets": skipped} if skipped else {}),
        }

    async def wait_for_reply(
        self,
        target: str | None = None,
        account: str | None = None,
        session_id: str | None = None,
        timeout_seconds: float = 300,
        poll_seconds: float = 15,
        until_idle: bool = False,
    ):
        """Wait for Devin's reply in one session; return it with the session's status.

        Returns as soon as a Devin message follows the latest user message. Unread
        replies from before this call count, so a fast answer to a message you
        just sent is not missed. Nothing is discarded: every unread Devin message
        read while waiting is in `messages`.

        With until_idle, keeps collecting until Devin has answered and stops
        working: it waits for you, has finished, or the session is suspended. Use
        it to get the whole answer and not only its first line. If Devin already
        answered and is idle, it returns at once.

        The result has `status` and `status_detail` (working, waiting_for_user,
        finished, ...) and `timed_out`. If the session was archived or has ended,
        the wait stops and the result has `ended`. If a read fails after replies
        were collected, they are returned with `error`, not lost.
        """
        if not 0 <= timeout_seconds <= 3600 or not 0 < poll_seconds <= 300:
            raise ValueError("timeout_seconds must be 0..3600; poll_seconds must be >0 and <=300")
        deadline = time.monotonic() + timeout_seconds
        held = []
        name = sid = answered = None
        info, reason = {}, None

        async def read():
            """Read what is unread; say whether Devin answered the latest user message."""
            nonlocal name, sid, answered
            result = await self.read_messages(
                target=target, account=account, session_id=session_id, include_user=True
            )
            if (name, sid) != (result["account"], result["session_id"]):
                # A Bot target moved to a new conversation: nothing is answered there yet.
                name, sid, answered = result["account"], result["session_id"], None
            for message in result["messages"]:
                if message["source"] == "devin":
                    held.append(message)
                # Replies drained from a Bot's earlier conversation are returned, but
                # they are not the answer of the conversation being waited on.
                if message.get("session_id", sid) == sid:
                    answered = message["source"] == "devin"
            if answered is None:
                answered = await self._answered(name, sid)

        def result(done, error=None):
            return {
                "account": name,
                "session_id": sid,
                "messages": held,
                "timed_out": not done,
                "status": info.get("status"),
                "status_detail": info.get("status_detail"),
                **({"ended": reason} if reason else {}),
                **({"error": error} if error else {}),
            }

        while True:
            # The last read always starts at or after the deadline, however long
            # the reads before it took.
            started = time.monotonic()
            try:
                await read()
            except ValueError as exc:
                if not held:
                    raise
                # The replies read so far are no longer unread: hand them over
                # with the error, so they are not lost.
                return result(True, str(exc))
            try:
                info = await self.client(name).get_session(sid)
                reason = ended(info)
            except DevinAPIError as exc:
                info, reason = {}, "not found" if exc.status_code in {404, 410} else None
            except ValueError:
                info, reason = {}, None
            if reason and reason != "not found":
                try:
                    await read()  # what it said between the read above and its end
                except ValueError:
                    pass
            idle = info.get("status") == "suspended" or info.get("status_detail") in IDLE_DETAILS
            done = bool(reason) or bool(answered and (idle if until_idle else held))
            if done or started >= deadline:
                return result(done)
            await asyncio.sleep(max(0, min(poll_seconds, deadline - time.monotonic())))

    async def _answered(self, account, sid):
        """Whether Devin answered the latest user message, when nothing unread shows it."""
        async with self.state.transaction() as data:
            known = data.get(account, {}).get(sid, {}).get("answered")
        if known is not None:
            return known
        # Never recorded for this session: look at the end of its history once.
        items, _ = await self.client(account).messages(sid)
        known = bool(items) and items[-1]["source"] == "devin"
        async with self.state.transaction() as data:
            if sid in data.get(account, {}):
                data[account][sid].setdefault("answered", known)
        return known
