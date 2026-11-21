import json

import pytest

from devin_claude_bridge.bridge import Bridge
from devin_claude_bridge.client import DevinAPIError
from devin_claude_bridge.config import Account, Config
from devin_claude_bridge.state import State
from devin_claude_bridge.targets import TargetStore, session_from


class Org:
    """One Devin organization held in memory."""

    def __init__(self):
        self.sessions = {}
        self.sent = []
        self.calls = []
        self.down = False

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
        return {k: v for k, v in self._get(sid).items() if k != "messages"}

    async def recent_sessions(self, active_only=True):
        if self.down:
            raise DevinAPIError(500, "Devin API 500: down")
        rows = [{k: v for k, v in s.items() if k != "messages"} for s in self.sessions.values()]
        return [s for s in rows if not (active_only and s["is_archived"])]

    async def messages(self, sid, after=None):
        self.calls.append(("messages", sid))
        items = self._get(sid)["messages"]
        return items[int(after or 0) :], str(len(items))

    async def send_message(self, sid, message):
        self.sent.append((sid, message))
        return await self.get_session(sid)

    async def create_session(self, prompt, title=None):
        return {k: v for k, v in self.add(f"new{len(self.sessions)}", title=title).items()}


@pytest.fixture
def orgs():
    return {"hexuria": Org(), "work": Org()}


def make(tmp_path, orgs, targets=None):
    config = Config({name: Account(f"org-{name}", "key") for name in orgs}, targets or {})
    return Bridge(config, State(tmp_path / "state.json"), lambda account: orgs[account.org_id[4:]])


@pytest.fixture
def bridge(tmp_path, orgs):
    return make(tmp_path, orgs)


@pytest.mark.parametrize(
    "value",
    [
        "abc123",
        "devin-abc123",
        "https://app.devin.ai/sessions/abc123",
        "https://app.devin.ai/sessions/devin-abc123?tab=shell#top",
        "https://app.devin.ai/org/my-org/sessions/abc123/",
        "app.devin.ai/sessions/abc123/files",
    ],
)
def test_session_from_url_or_id(value):
    assert session_from(value) == "devin-abc123"


@pytest.mark.parametrize("value", ["", "https://app.devin.ai/", "https://x/sessions/a b"])
def test_session_from_rejects(value):
    with pytest.raises(ValueError):
        session_from(value)


async def test_add_by_url_finds_the_account_and_watches_from_now(bridge, orgs):
    orgs["work"].add("fix")
    orgs["work"].say("fix", "old history")
    added = await bridge.add_target("work-fix", "https://app.devin.ai/sessions/fix")
    assert (added["account"], added["session_id"], added["title"]) == ("work", "devin-fix", "Fix")
    assert await bridge.check_inbox() == {"sessions": []}
    orgs["work"].say("fix", "new reply")
    inbox = await bridge.check_inbox()
    assert [m["message"] for m in inbox["sessions"][0]["messages"]] == ["new reply"]
    assert inbox["sessions"][0]["targets"] == ["work-fix"]
    await bridge.send_message("hello", target="work-fix")
    assert orgs["work"].sent == [("devin-fix", "hello")]
    listed = bridge.list_accounts()["targets"]["work-fix"]
    assert (listed["account"], listed["session_id"], listed["source"]) == (
        "work",
        "devin-fix",
        "added",
    )


async def test_an_alias_is_shared_by_every_bridge_process(tmp_path, orgs):
    orgs["hexuria"].add("one")
    await make(tmp_path, orgs).add_target("hexuria", "one", account="hexuria")
    other = make(tmp_path, orgs)
    await other.send_message("from the cli", target="hexuria")
    assert orgs["hexuria"].sent == [("devin-one", "from the cli")]
    assert TargetStore(tmp_path / "targets.json").read()["targets"]["hexuria"]["account"] == (
        "hexuria"
    )


@pytest.mark.parametrize(
    "fields,word",
    [({"is_archived": True}, "is archived"), ({"status": "exit"}, "has ended (status exit)")],
)
async def test_add_refuses_a_dead_session_and_lists_active_ones(bridge, orgs, fields, word):
    orgs["work"].add("gone", **fields)
    orgs["work"].add("alive")
    with pytest.raises(ValueError) as error:
        await bridge.add_target("work", "gone")
    assert word in str(error.value) and "cannot be a target" in str(error.value)
    assert '"Alive" https://app.devin.ai/sessions/alive' in str(error.value)
    assert '"Gone"' not in str(error.value).split("Active sessions")[1]
    assert bridge.list_accounts()["targets"] == {}


async def test_add_errors(tmp_path, orgs):
    bridge = make(tmp_path, orgs, {"filed": {"account": "work", "session_id": "devin-a"}})
    orgs["work"].add("a")
    with pytest.raises(ValueError, match="defined in config.toml"):
        await bridge.add_target("filed", "a")
    with pytest.raises(ValueError, match="letters, digits"):
        await bridge.add_target("bad name", "a")
    with pytest.raises(ValueError, match="No configured account can open devin-zzz.*hexuria.*work"):
        await bridge.add_target("x", "zzz")
    with pytest.raises(ValueError, match="Unknown account"):
        await bridge.add_target("x", "a", account="nope")
    with pytest.raises(ValueError, match="defined in config.toml; remove it there"):
        await bridge.remove_target("filed")
    with pytest.raises(ValueError, match="Unknown target: x"):
        await bridge.remove_target("x")


async def test_send_to_an_archived_alias_sends_nothing_and_says_how_to_fix(bridge, orgs):
    session = orgs["hexuria"].add("task")
    orgs["hexuria"].add("other")
    await bridge.add_target("hexuria", "task")
    session["is_archived"] = True
    with pytest.raises(ValueError) as error:
        await bridge.send_message("still there?", target="hexuria")
    text = str(error.value)
    assert "Target hexuria points to a session that is archived" in text
    assert "Nothing was sent" in text and 'add_target("hexuria"' in text
    assert '"Other" https://app.devin.ai/sessions/other' in text
    assert orgs["hexuria"].sent == []
    assert bridge.list_accounts()["targets"]["hexuria"]["stale"] == "archived"
    session["is_archived"] = False
    await bridge.send_message("back", target="hexuria")
    assert orgs["hexuria"].sent == [("devin-task", "back")]
    assert "stale" not in bridge.list_accounts()["targets"]["hexuria"]


async def test_send_by_id_refuses_dead_and_wakes_suspended(bridge, orgs):
    orgs["work"].add("done", status="exit")
    orgs["work"].add("asleep", status="suspended")
    with pytest.raises(ValueError, match="Session devin-done on account work has ended"):
        await bridge.send_message("x", account="work", session_id="done")
    await bridge.send_message("wake up", account="work", session_id="asleep")
    assert orgs["work"].sent == [("devin-asleep", "wake up")]


async def test_inbox_delivers_last_replies_then_reports_a_dead_alias_once(bridge, orgs):
    session = orgs["hexuria"].add("task")
    await bridge.add_target("hexuria", "task")
    orgs["hexuria"].say("task", "final answer")
    session["is_archived"] = True
    first = await bridge.check_inbox()
    assert [m["message"] for m in first["sessions"][0]["messages"]] == ["final answer"]
    notice = first["notices"][0]
    assert (notice["target"], notice["reason"]) == ("hexuria", "archived")
    assert 'add_target("hexuria"' in notice["message"]
    assert json.loads(bridge.state.path.read_text())["hexuria"] == {}
    orgs["hexuria"].calls.clear()
    assert await bridge.check_inbox() == {"sessions": []}
    assert orgs["hexuria"].calls == []


async def test_inbox_notices_a_deleted_alias_session_and_keeps_others(tmp_path, orgs):
    bridge = make(tmp_path, orgs, {"filed": {"account": "work", "session_id": "devin-a"}})
    orgs["work"].add("a")
    orgs["hexuria"].add("b")
    await bridge.watch_session(target="filed")
    await bridge.add_target("hexuria", "b")
    del orgs["work"].sessions["devin-a"]
    first = await bridge.check_inbox()
    assert first["notices"][0]["target"] == "filed"
    assert first["notices"][0]["reason"] == "not found"
    assert "hexuria" in json.loads(bridge.state.path.read_text())
    second = await bridge.check_inbox()
    assert second == {"sessions": []}


async def test_inbox_keeps_the_watch_when_the_last_read_failed(bridge, orgs):
    session = orgs["hexuria"].add("task")
    await bridge.add_target("hexuria", "task")
    session["is_archived"] = True

    async def broken(sid, after=None):
        raise DevinAPIError(500, "Devin API 500: try later")

    reading, orgs["hexuria"].messages = orgs["hexuria"].messages, broken
    result = await bridge.check_inbox()
    assert "error" in result["sessions"][0] and result["notices"][0]["target"] == "hexuria"
    assert "devin-task" in json.loads(bridge.state.path.read_text())["hexuria"]
    orgs["hexuria"].messages = reading


async def test_repointing_moves_the_alias_and_drops_a_dead_old_watch(bridge, orgs):
    old = orgs["hexuria"].add("old")
    orgs["hexuria"].add("new")
    await bridge.add_target("hexuria", "old", description="main task")
    old["is_archived"] = True
    moved = await bridge.add_target("hexuria", "https://app.devin.ai/sessions/new")
    assert moved["replaced"] == {
        "account": "hexuria",
        "session_id": "devin-old",
        "still_watched": False,
    }
    state = json.loads(bridge.state.path.read_text())["hexuria"]
    assert list(state) == ["devin-new"]
    target = bridge.list_accounts()["targets"]["hexuria"]
    assert (target["session_id"], target["description"]) == ("devin-new", "main task")
    orgs["hexuria"].add("third")
    again = await bridge.add_target("hexuria", "third")
    assert again["replaced"]["still_watched"] is True


async def test_remove_target(bridge, orgs):
    orgs["work"].add("a")
    await bridge.add_target("one", "a")
    await bridge.add_target("two", "a")
    kept = await bridge.remove_target("one")
    assert kept["unwatched"] is False
    assert "devin-a" in json.loads(bridge.state.path.read_text())["work"]
    gone = await bridge.remove_target("two")
    assert gone["unwatched"] is True
    assert json.loads(bridge.state.path.read_text())["work"] == {}
    with pytest.raises(ValueError, match="Unknown target: two"):
        await bridge.send_message("x", target="two")


async def test_create_session_with_alias(tmp_path, orgs):
    bridge = make(tmp_path, orgs, {"filed": {"account": "work", "session_id": "devin-a"}})
    with pytest.raises(ValueError, match="defined in config.toml"):
        await bridge.create_session("work", "do it", alias="filed")
    assert orgs["work"].sessions == {}
    created = await bridge.create_session("work", "do it", title="Task", alias="work")
    assert created["target"] == "work"
    await bridge.send_message("more", target="work")
    assert orgs["work"].sent == [(created["session_id"], "more")]
    second = await bridge.create_session("work", "next", alias="work")
    assert second["replaced"]["session_id"] == created["session_id"]
    assert bridge.list_accounts()["targets"]["work"]["session_id"] == second["session_id"]


async def test_list_sessions_across_accounts(bridge, orgs):
    orgs["work"].add("a", updated_at=10)
    orgs["work"].add("archived", is_archived=True, updated_at=50)
    orgs["work"].add("done", status="exit", updated_at=40)
    orgs["hexuria"].add("b", updated_at=30, status="suspended", status_detail="inactivity")
    await bridge.add_target("hexuria", "b")
    listed = await bridge.list_sessions()
    assert [(s["account"], s["title"]) for s in listed["sessions"]] == [
        ("hexuria", "B"),
        ("work", "A"),
    ]
    assert listed["sessions"][0]["targets"] == ["hexuria"]
    assert listed["sessions"][0]["status_detail"] == "inactivity"
    assert listed["sessions"][0]["updated"] == "1970-01-01 00:00 UTC"
    everything = await bridge.list_sessions(account="work", active_only=False, limit=2)
    assert [s["title"] for s in everything["sessions"]] == ["Archived", "Done"]
    orgs["work"].down = True
    partial = await bridge.list_sessions()
    assert [s["account"] for s in partial["sessions"]] == ["hexuria"]
    assert partial["errors"][0]["account"] == "work"
    with pytest.raises(ValueError, match="limit"):
        await bridge.list_sessions(limit=0)
    with pytest.raises(ValueError, match="Unknown account"):
        await bridge.list_sessions(account="nope")
