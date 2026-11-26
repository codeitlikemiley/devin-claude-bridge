import asyncio
import time
from copy import deepcopy
from datetime import datetime, timezone

from .client import DevinAPIError, DevinClient
from .config import session_id as normalize
from .fleet import (
    NEEDS,
    NEON_CAP,
    PLATFORMS,
    RULE,
    FleetStore,
    ahead_of,
    first_line,
    fleet_path,
    guess,
    is_free,
    kind,
    row,
    ticket_row,
    view,
    working,
)
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
        # Empty for Free (SWE-2) sessions and agent previews, which have no API mode.
        "devin_mode": session.get("devin_mode"),
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
    def __init__(self, config, state=None, client_factory=DevinClient, targets=None, fleet=None):
        self.config = config
        self.state = state or State()
        self.store = targets or TargetStore(store_path(self.state.path))
        self.machines = fleet or FleetStore(fleet_path(self.state.path))
        # Send times live apart from the inbox watches, so unwatching a session
        # does not clear its count.
        self.sends = State(self.state.path.with_name(self.state.path.name + ".sends.json"))
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
            return session
        async with self.state.transaction() as data:
            if sid in data.get(account, {}):
                data[account][sid]["retire"] = True
        named = _named(session)
        if target:
            head = f"Target {target} points to a session that {_what(session, reason)}"
            advice = (
                f'Re-point it with add_target("{target}", <session URL>). For a Free '
                "session, start it in the Devin web app with Free picked."
            )
        else:
            head = f"Session {sid} on account {account} {_what(session, reason)}"
            advice = "Pick another session."
        lines = [f"{head}{': ' + named if named else ''}. Nothing was sent. {advice}"]
        raise ValueError("\n".join(lines + await self._active_lines(account)))

    async def _dead(self, account, sids):
        """Which of these sessions are archived, ended or gone: {sid: (session, reason)}."""
        found = await self._lookup(account, sids)
        dead = {}
        for sid in sids:
            info = found[sid]
            if info == "not found":
                dead[sid] = ({}, "not found")
            elif info is not None and ended(info):
                dead[sid] = (info, ended(info))
        return dead

    async def _lookup(self, account, sids):
        """{sid: session, "not found", or None when Devin could not say}, one list request."""
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
        return found

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

    # The fleet: who holds which machine. Rules in fleet.py.

    async def _live(self, aliases=None):
        """alias -> (status, detail) as Devin reports it now; one request per account.

        A Bot target counts as the conversation the bridge last followed for it.
        `("ended", why)` and `("unknown", "")` mean it cannot take a message now.
        """
        self._refresh()
        aliases = list(self.config.targets) if aliases is None else list(aliases)
        async with self.state.transaction() as data:
            followed = {
                (name, alias): sid
                for name, sessions in data.items()
                for sid, position in sessions.items()
                for alias in position.get("targets", [])
            }
        wanted = {}
        for alias in aliases:
            entry = self.config.targets[alias]
            name = entry["account"]
            sid = entry.get("session_id") or followed.get((name, alias))
            if sid and name in self.clients:
                wanted.setdefault(name, {}).setdefault(sid, []).append(alias)
        out = {alias: ("unknown", "") for alias in aliases}
        names = list(wanted)
        answers = await asyncio.gather(
            *(self._lookup(name, list(wanted[name])) for name in names), return_exceptions=True
        )
        for name, answer in zip(names, answers):
            if isinstance(answer, ValueError):
                continue
            if isinstance(answer, BaseException):
                raise answer
            for sid, info in answer.items():
                if info is None:
                    continue
                reason = "not found" if info == "not found" else ended(info)
                status = (
                    ("ended", reason)
                    if reason
                    else (info.get("status") or "", info.get("status_detail") or "")
                )
                for alias in wanted[name].get(sid, []):
                    out[alias] = status
        return out

    def _alias_of(self, account, sid, target=None):
        """The fleet machine a message goes to: the target, or the alias pinned to the session."""
        if target:
            return target
        pinned = sorted(self._aliases().get((account, sid), []))
        if len(pinned) > 1:
            held = self.machines.read()["machines"]
            pinned.sort(key=lambda alias: not held.get(alias, {}).get("holder"))
        return pinned[0] if pinned else None

    def _take(self, saved, alias, status, holder, task, follow_up=False, started=False):
        """Hold `alias` for `holder`, inside the fleet lock. Returns why not, or None.

        Taking a machine also takes the holder's own first ticket for that kind
        out of the line.
        """
        m = view(saved, {alias: status})[alias]
        need = kind(m)
        if follow_up:
            if m["holder"] != holder:
                return (
                    f"A follow-up is only for the session that holds the machine; {alias} is "
                    f"held by {m['holder'] or 'nobody'}."
                )
        else:
            if m["holder"] == holder:
                return (
                    f"You hold {alias} already, for: {m['task']}. If this continues that work, "
                    "it is a follow-up; a different job is a new task: release it first."
                )
            if m["holder"]:
                return f"{alias} is held by {m['holder']} for: {m['task']}."
            if working(*status) and not started:
                return (
                    f"{alias} is working right now ({' '.join(x for x in status if x)}) and "
                    "nobody holds it. If you started that work yourself, claim it with "
                    "started=true."
                )
            if status[0] in ("ended", "unknown"):
                why = status[1] or "Devin did not report its status"
                return f"{alias} cannot take a message ({status[0]}: {why})."
            ahead = ahead_of(saved["queue"], need, holder)
            if ahead:
                return (
                    f"#{ahead['n']} ({ahead['by']}) is ahead of you in line for a {need} machine."
                )
            mine = next(
                (t for t in saved["queue"] if t["need"] == need and t["by"] == holder), None
            )
            if mine:
                saved["queue"].remove(mine)
        now = time.time()
        since = m["since"] if follow_up and m["since"] else now
        entry = saved["machines"].get(alias, {})
        saved["machines"][alias] = {
            **entry,
            "platform": m["platform"],
            "pool": m["pool"],
            "extras": m["extras"],
            "holder": holder,
            "task": task,
            "since": since,
            # A claim is fresh until the first message after it: that message may
            # start work. Started work counts as already sent.
            **({"sent_since": since} if started else {}),
        }
        return None

    async def _refuse(self, alias, reason, holder, done):
        """Raise the refusal with the free machines of that kind and how to queue."""
        machines = view(self.machines.read(), await self._live())
        need = kind(machines[alias]) if alias in machines else "?"
        free = sorted(a for a, m in machines.items() if kind(m) == need and is_free(m))
        waiting = [t for t in self.machines.read()["queue"] if t["need"] == need]
        lines = [
            f"{reason} {done}",
            f"Free {need} machines: {', '.join(free) if free else 'none'}.",
            (f"{len(waiting)} waiting in line for {need}. " if waiting else "")
            + f'To wait: queue(need="{need}", holder="{holder or "<your session name>"}", '
            'task="<the task in one line>").',
        ]
        raise ValueError("\n".join(lines))

    def _holding(self, holder, kind_):
        """Check the holder and kind of a send; a warning when an old caller left them out."""
        if holder is None and kind_ is None:
            return (
                f"{RULE} Pass holder (your session's name) and kind ('new' or 'follow_up'); "
                "they will become required."
            )
        if not holder or not holder.strip() or kind_ not in ("new", "follow_up"):
            raise ValueError(
                "Pass holder (your Claude session's name) and kind: 'new' for a message that "
                "starts work, 'follow_up' for the same work on a machine you hold. Nothing "
                "was sent."
            )
        return None

    async def fleet(self, need: str | None = None, free_only: bool = False):
        """Every Devin machine in one line: kind, Devin's status, free or busy, holder, task.

        Look here before any message that starts work. The rule: a new task goes
        only to a free machine (claim it, or send with kind="new"); a busy machine
        gets only a follow-up from the session that holds it, on the same work.
        No free machine of the kind you need: queue(), and the first in line gets
        the next one released. Kinds: neon (Devin with Gemini, Linux only, 4 at
        most; for forms work that needs reading pictures or video), linux, macos,
        windows. Then `line`: who waits, oldest first.
        """
        if need is not None and need not in NEEDS:
            raise ValueError(f"need is one of {', '.join(NEEDS)}")
        machines = view(self.machines.read(), await self._live())
        saved = self.machines.read()
        now = time.time()
        rows = [
            row(alias, m, now)
            for alias, m in sorted(machines.items(), key=lambda item: (kind(item[1]), item[0]))
            if not (need and kind(m) != need) and not (free_only and not is_free(m))
        ]
        neon = sum(1 for m in machines.values() if m["pool"] == "neon")
        return {
            "columns": "target | kind | devin status | free | holder | held | task [extras]",
            "machines": rows,
            "line": [ticket_row(t, now) for t in saved["queue"] if not need or t["need"] == need],
            "neon": f"{neon} of {NEON_CAP} allowed, Linux only",
        }

    async def claim(
        self,
        target: str,
        holder: str,
        task: str,
        follow_up: bool = False,
        started: bool = False,
    ):
        """Hold a free Devin machine for a new task, before you message it.

        Refused when another session holds it, when Devin is working on it and
        nobody holds it, or when someone is ahead of you in line for its kind.
        follow_up=true only renames the task of a machine you already hold, for
        the same work. started=true records work you already started on an
        unclaimed machine. `holder` is your Claude session's name.
        """
        self._refresh()
        if target not in self.config.targets:
            raise ValueError(f"Unknown target: {target}")
        holder, task = holder.strip(), first_line(task)
        if not holder or not task:
            raise ValueError("Name the holder (your session's name) and the task in one line")
        live = await self._live([target])
        with self.machines.edit() as saved:
            reason = self._take(saved, target, live[target], holder, task, follow_up, started)
        if reason:
            await self._refuse(target, reason, holder, "Not claimed.")
        return {"target": target, "holder": holder, "task": task, "claimed": True}

    def release(self, target: str, holder: str):
        """Free a machine you hold, once you have read its final report.

        Only the holder may release it. The result names the next ticket in line
        for that kind of machine, whose session may claim it now.
        """
        self._refresh()
        with self.machines.edit() as saved:
            if target not in saved["machines"] and target not in self.config.targets:
                raise ValueError(f"Unknown target: {target}")
            entry = saved["machines"].get(target, {})
            if entry.get("holder") and entry["holder"] != holder:
                raise ValueError(
                    f"{target} is held by {entry['holder']}, not {holder}. Ask that session "
                    "to release it."
                )
            was_held = bool(entry.get("holder"))
            entry = {k: v for k, v in entry.items() if k != "sent_since"}
            saved["machines"][target] = {**entry, "holder": "", "task": "", "since": None}
            need = kind(view(saved, {target: ("", "")})[target])
            waiting = next((t for t in saved["queue"] if t["need"] == need), None)
        result = {"target": target, "released": was_held}
        if waiting:
            result["next_in_line"] = ticket_row(waiting)
            result["message"] = (
                f"{target} is free. Next in line for {need}: #{waiting['n']} "
                f"({waiting['by']}), who may claim it now."
            )
        else:
            result["message"] = f"{target} is free." if was_held else f"Nobody held {target}."
        return result

    def queue(self, need: str, holder: str, task: str):
        """Wait in line for a machine of a kind (neon, linux, macos, windows) when none is free.

        When a machine of that kind is released, the first in line claims it;
        nobody behind you can take it first.
        """
        if need not in NEEDS:
            raise ValueError(f"need is one of {', '.join(NEEDS)}")
        holder, task = holder.strip(), first_line(task)
        if not holder or not task:
            raise ValueError("Name the holder (your session's name) and the task in one line")
        with self.machines.edit() as saved:
            ticket = {"n": saved["next"], "need": need, "by": holder, "task": task}
            ticket["at"] = time.time()
            saved["queue"].append(ticket)
            saved["next"] += 1
            position = sum(1 for t in saved["queue"] if t["need"] == need)
        return {"ticket": ticket["n"], "need": need, "position": position, "task": task}

    def unqueue(self, ticket: int, holder: str):
        """Leave the line: drop your own ticket."""
        with self.machines.edit() as saved:
            keep = [t for t in saved["queue"] if not (t["n"] == ticket and t["by"] == holder)]
            if len(keep) == len(saved["queue"]):
                raise ValueError(f"No ticket #{ticket} of {holder}")
            saved["queue"] = keep
        return {"ticket": ticket, "removed": True}

    def _neon_refusal(self, saved, alias, platform):
        """Why `alias` cannot be a Neon machine, or None: Linux only, four at most."""
        if platform != "linux":
            return "Neon machines are Linux only (platform='linux')."
        neon = {
            a
            for a in self.config.targets
            if (saved["machines"].get(a, {}).get("pool") or guess(a)[1]) == "neon"
        }
        if len(neon | {alias}) > NEON_CAP:
            return f"Only {NEON_CAP} Neon machines are allowed: {', '.join(sorted(neon))}."
        return None

    def describe_target(self, target: str, platform: str, pool: str, extras: str = ""):
        """Say what a machine is: platform (linux, macos, windows) and pool (neon or free).

        Neon is Linux only and capped at four machines. `extras` notes what else
        it has, such as an Android emulator.
        """
        self._refresh()
        if target not in self.config.targets:
            raise ValueError(f"Unknown target: {target}")
        if platform not in PLATFORMS or pool not in ("neon", "free"):
            raise ValueError("platform is linux, macos or windows; pool is neon or free")
        with self.machines.edit() as saved:
            if pool == "neon":
                reason = self._neon_refusal(saved, target, platform)
                if reason:
                    raise ValueError(reason)
            entry = saved["machines"].get(target, {})
            saved["machines"][target] = {
                **entry,
                "platform": platform,
                "pool": pool,
                "extras": extras.strip(),
            }
        return {"target": target, "platform": platform, "pool": pool, "extras": extras.strip()}

    def _held(self, account, sid, target=None):
        """The fleet machine this session is and its entry, when someone holds it."""
        try:
            machines = self.machines.read()["machines"]
        except ValueError:
            return None
        aliases = [target] if target else sorted(self._aliases().get((account, sid), []))
        return next(((a, machines[a]) for a in aliases if machines.get(a, {}).get("holder")), None)

    def _hint(self, account, sid, info, target=None):
        """For a held machine that finished or waits for the user: remind its holder."""
        held = (
            self._held(account, sid, target) if info.get("status_detail") in IDLE_DETAILS else None
        )
        if not held:
            return None
        alias, entry = held
        return (
            f"{alias} is held by {entry['holder']} for: {entry.get('task', '')}; "
            "release when you have read this."
        )

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
        self,
        account: str,
        prompt: str,
        title: str | None = None,
        alias: str | None = None,
        platform: str | None = None,
        repos: list[str] | None = None,
        tags: list[str] | None = None,
        playbook_id: str | None = None,
        secret_ids: list[str] | None = None,
        devin_mode: str | None = None,
        pool: str | None = None,
        holder: str | None = None,
    ):
        """Create and automatically watch a new Devin session. It may be billed.

        Off unless DEVIN_BRIDGE_ALLOW_CREATE=1, and `devin_mode` is required:
        "normal", "fast", "fusion", "lite", "ultra" or "fast-ultra". Devin's API
        refuses Free (SWE-2) sessions; a person starts those in the Devin web app,
        and add_target names them here.

        With `alias`, the new session also becomes that target, and an existing
        alias of that name moves to it. `platform` picks the machine: "linux",
        "macos" or "windows" (an unknown value is refused with the list the
        account accepts); without it the account's default is used. `repos`,
        `tags`, `playbook_id` and `secret_ids` go to Devin unchanged; `secret_ids`
        names secrets that already exist in that account.

        With `alias`, the session is also a fleet machine: `pool` "neon" (Devin
        with Gemini, Linux only, at most 4) or "free" (by default, unless the
        alias starts with neon), held by `holder`, your Claude session's name,
        for the prompt's first line. Start a new session only when no machine of
        the kind you need exists, or all are held and the line is long.
        """
        self._refresh()
        if not self.config.allow_create:
            raise ValueError(
                "create_session is off. Every session it starts is billed: Devin's "
                "API refuses Free (SWE-2) sessions, which only a person can start. "
                "Start a Free session in the Devin web app (or with the "
                "devin-free-session skill in Claude Code) and name it with "
                "add_target, or set DEVIN_BRIDGE_ALLOW_CREATE=1 to allow billed "
                "sessions."
            )
        if not devin_mode:
            raise ValueError(
                "Name the devin_mode for the new session (it decides the cost): normal, "
                "fast, fusion, lite, ultra or fast-ultra. Without it Devin uses the "
                "organization's default mode. Nothing was created."
            )
        warning = self._holding(holder, None if holder is None else "new")
        holder = holder.strip() if holder else None
        if pool not in (None, "neon", "free"):
            raise ValueError("pool is neon or free")
        if pool and alias is None:
            raise ValueError("A pool needs an alias: the machine's name in the fleet")
        if alias is not None:
            self._check_alias(alias)
            if warning and self.config.require_holder:
                raise ValueError(
                    "Name holder (your Claude session's name). "
                    f"{RULE} See fleet(). Nothing was created."
                )
            saved = self.machines.read()
            other = saved["machines"].get(alias, {}).get("holder")
            if other and other != holder:
                raise ValueError(
                    f"{alias} is held by {other}; moving it would take their machine. "
                    "Nothing was created."
                )
            pool = pool or saved["machines"].get(alias, {}).get("pool") or guess(alias)[1]
            if pool == "neon":
                reason = self._neon_refusal(saved, alias, platform)
                if reason:
                    raise ValueError(f"{reason} Nothing was created.")
        result = await self.client(account).create_session(
            prompt,
            title,
            platform=platform,
            repos=repos,
            tags=tags,
            playbook_id=playbook_id,
            secret_ids=secret_ids,
            devin_mode=devin_mode,
        )
        await self.watch_session(account=account, session_id=result["session_id"])
        if alias is None:
            return result
        sid = normalize(result["session_id"])
        replaced = await self._point(alias, account, sid, result, None)
        with self.machines.edit() as saved:
            entry = saved["machines"].get(alias, {})
            now = time.time()
            saved["machines"][alias] = {
                **{k: v for k, v in entry.items() if k != "sent_since"},
                "platform": platform or entry.get("platform") or guess(alias)[0],
                "pool": pool,
                "extras": entry.get("extras", ""),
                "holder": holder or "",
                "task": first_line(prompt) if holder else "",
                "since": now if holder else None,
                # The prompt was the message that started the work.
                **({"sent_since": now} if holder else {}),
            }
        return {
            **result,
            "target": alias,
            **({"replaced": replaced} if replaced else {}),
            **({"warning": warning} if warning else {}),
        }

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
        holder: str | None = None,
        kind: str | None = None,
    ):
        """Send to a target alias OR an account/session pair; automatically watch it.

        The machine rule: kind="new" (a message that starts work) goes only to a
        machine you just claimed or one that is free with nobody ahead of you in
        line, and then claims it for you; kind="follow_up" (the same work) only to
        a machine you hold. `holder` is your Claude session's name. A refusal says
        who holds the machine, which ones are free, and how to queue. See fleet().

        Nothing is sent to a session that is archived or has ended; the error then
        lists the account's active sessions. A suspended session is resumed. Nothing
        is sent to a session that already got the hourly maximum of messages from
        this bridge (DEVIN_BRIDGE_MAX_SENDS_PER_HOUR, 30 by default).
        """
        self._refresh()
        warning = self._holding(holder, kind)
        holder = holder.strip() if holder else None
        bot = target if self._bot(target) else None
        info = None
        if not bot:
            # Resolve a pinned target once: the session that passes the check is the
            # one that gets the message, even if the alias is moved meanwhile.
            account, session_id = self.config.resolve(target, account, session_id)
            info = await self._require_live(target, account, session_id)
            alias = self._alias_of(account, session_id, target)
            target = None
        else:
            alias = bot
        if alias is None:
            # A session that is no fleet machine: the rule has nothing to guard.
            warning = None
        elif warning and self.config.require_holder:
            raise ValueError(
                "Name holder (your Claude session's name) and kind ('new' or 'follow_up'). "
                f"{RULE} See fleet(). Nothing was sent."
            )
        claimed = None
        if alias and holder:
            if info is not None:
                status = (info.get("status") or "", info.get("status_detail") or "")
            else:
                status = (await self._live([alias]))[alias]
            with self.machines.edit() as saved:
                before = (deepcopy(saved["machines"].get(alias)), list(saved["queue"]))
                reason = self._guard(saved, alias, status, holder, kind, message)
                since = saved["machines"].get(alias, {}).get("since")
                if reason is None and since != (before[0] or {}).get("since"):
                    claimed = since
            if reason:
                await self._refuse(alias, reason, holder, "Nothing was sent.")
        try:
            async with self.state.transaction() as data:
                account, sid, result = await self._operate(
                    data,
                    target,
                    account,
                    session_id,
                    lambda name, sid: self._send(name, sid, message),
                )
                position = data.setdefault(account, {}).setdefault(
                    sid, {"cursor": None, "event_id": None}
                )
                position.pop("retire", None)
                position["answered"] = False
                if not self.config.targets.get(target, {}).get("bot_service_user_id"):
                    position["direct"] = True
        except BaseException:
            if claimed:
                # The message did not go: the claim the send made is undone.
                with self.machines.edit() as saved:
                    if saved["machines"].get(alias, {}).get("since") == claimed:
                        entry, queue = before
                        if entry is None:
                            saved["machines"].pop(alias, None)
                        else:
                            saved["machines"][alias] = entry
                        taken = [t for t in queue if t not in saved["queue"]]
                        saved["queue"] = sorted(saved["queue"] + taken, key=lambda t: t["n"])
            raise
        if alias and holder:
            with self.machines.edit() as saved:
                entry = saved["machines"].get(alias, {})
                if entry.get("holder") == holder:
                    entry["sent_since"] = entry.get("since")
            if claimed:
                result = {**result, "claimed": f"{alias} is held by {holder} now"}
        if warning:
            result = {**result, "warning": warning}
        return result

    def _guard(self, saved, alias, status, holder, kind_, message):
        """Whether this send may go, inside the fleet lock: None, or why not.

        A new task on a free machine claims it, with the message's first line as
        the task.
        """
        m = view(saved, {alias: status})[alias]
        if kind_ == "follow_up":
            if m["holder"] != holder:
                return (
                    f"A follow-up is only for the session that holds the machine; {alias} is "
                    f"held by {m['holder'] or 'nobody'}"
                    + (f" for: {m['task']}." if m["holder"] else ". Send it as kind='new'.")
                )
            return None
        if m["holder"] == holder:
            if m["fresh"]:
                return None
            return (
                f"You hold {alias} for: {m['task']}, and it already has that work. If this "
                "continues it, send it as kind='follow_up'; a different job is a new task: "
                "release it, then claim a free machine."
            )
        return self._take(saved, alias, status, holder, first_line(message))

    async def _send(self, account, sid, message):
        """Send one message, unless this session already got the hourly maximum.

        Two agents that answer each other can exchange messages without end. The
        limit stops that loop. Send times are kept in their own state file, so a
        restart or an unwatch does not clear them.
        """
        limit = self.config.max_sends_per_hour
        async with self.sends.transaction() as sends:
            now = time.time()
            for name in list(sends):
                for key in list(sends[name]):
                    kept = [t for t in sends[name][key].get("sent_at", []) if now - t < 3600]
                    if kept:
                        sends[name][key]["sent_at"] = kept
                    else:
                        del sends[name][key]
                if not sends[name]:
                    del sends[name]
            entry = sends.setdefault(account, {}).setdefault(sid, {"sent_at": []})
            recent = entry["sent_at"]
            if limit and len(recent) >= limit:
                wait = max(1, -(-int(3600 - (now - min(recent))) // 60))
                raise ValueError(
                    f"Session {sid} on account {account} got {len(recent)} messages from "
                    f"this bridge in the last hour, the maximum. Nothing was sent. If two "
                    f"agents answer each other in a loop, stop the loop. Otherwise try "
                    f"again in {wait} minutes, or raise DEVIN_BRIDGE_MAX_SENDS_PER_HOUR "
                    f"(0 is no limit)."
                )
            result = await self.client(account).send_message(sid, message)
            recent.append(now)
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
        """Read all pages, advance last-seen position, and watch. Default: new Devin replies.

        When you hold the machine and Devin has finished or waits for you, `hint`
        reminds you to release it once you have read this.
        """
        result = await self._read_messages(target, account, session_id, only_new, include_user)
        if self._held(result["account"], result["session_id"], target):
            # Only a held machine costs this extra look at its status.
            try:
                info = await self.client(result["account"]).get_session(result["session_id"])
            except ValueError:
                info = {}
            hint = self._hint(result["account"], result["session_id"], info, target)
            if hint:
                result["hint"] = hint
        return result

    async def _read_messages(self, target, account, session_id, only_new=True, include_user=False):
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
                                    f'add_target("{alias}", <session URL>). For a Free '
                                    "session, start it in the Devin web app with Free picked."
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
        were collected, or the last read of an ended session fails, the result has
        `error` and the replies collected so far. When you hold the machine and it
        has finished or waits for you, `hint` reminds you to release it once read.
        """
        if not 0 <= timeout_seconds <= 3600 or not 0 < poll_seconds <= 300:
            raise ValueError("timeout_seconds must be 0..3600; poll_seconds must be >0 and <=300")
        deadline = time.monotonic() + timeout_seconds
        held = []
        name = sid = answered = None
        info, reason = {}, None

        async def read(bootstrap=True):
            """Read what is unread; say whether Devin answered the latest user message."""
            nonlocal name, sid, answered
            result = await self._read_messages(target, account, session_id, include_user=True)
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
            if answered is None and bootstrap:
                answered, from_history = await self._answered(name, sid)
                if from_history:
                    # A reply may have arrived between the read above and that look
                    # at the history. Read again, so a reply that was counted is
                    # also in the result.
                    await read(bootstrap=False)

        def result(done, error=None):
            hint = self._hint(name, sid, info, target) if name and not reason else None
            return {
                "account": name,
                "session_id": sid,
                "messages": held,
                "timed_out": not done,
                "status": info.get("status"),
                "status_detail": info.get("status_detail"),
                **({"ended": reason} if reason else {}),
                **({"error": error} if error else {}),
                **({"hint": hint} if hint else {}),
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
            failed = None
            if reason and reason != "not found":
                try:
                    await read()  # what it said between the read above and its end
                except ValueError as exc:
                    failed = str(exc)
            idle = info.get("status") == "suspended" or info.get("status_detail") in IDLE_DETAILS
            done = bool(reason) or bool(answered and (idle if until_idle else held))
            if done or started >= deadline:
                return result(done, failed)
            await asyncio.sleep(max(0, min(poll_seconds, deadline - time.monotonic())))

    async def _answered(self, account, sid):
        """Whether Devin answered the latest user message, when nothing unread shows it.

        Returns the answer and whether it came from a fresh look at the history.
        """
        async with self.state.transaction() as data:
            known = data.get(account, {}).get(sid, {}).get("answered")
        if known is not None:
            return known, False
        # Never recorded for this session: look at the end of its history once.
        items, _ = await self.client(account).messages(sid)
        known = bool(items) and items[-1]["source"] == "devin"
        async with self.state.transaction() as data:
            if sid in data.get(account, {}):
                data[account][sid].setdefault("answered", known)
        return known, True
