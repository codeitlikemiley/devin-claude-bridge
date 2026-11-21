import json
from unittest.mock import AsyncMock

import httpx
import pytest

from devin_claude_bridge.bridge import Bridge
from devin_claude_bridge.client import DevinAPIError, DevinClient
from devin_claude_bridge.config import Account, Config, load_config
from devin_claude_bridge.state import State


def root(sid, created=1, **kwargs):
    return {
        "session_id": sid,
        "created_at": created,
        "service_user_id": "service-user-bot",
        "parent_session_id": None,
        "is_archived": False,
        "status": "running",
        "title": "Research",
        **kwargs,
    }


@pytest.fixture
def bridge(tmp_path):
    config = Config(
        {"one": Account("org-one", "secret")},
        {"bot": {"account": "one", "bot_service_user_id": "service-user-bot"}},
    )
    bridge = Bridge(config, State(tmp_path / "state.json"), lambda a: AsyncMock())
    bridge.clients["one"].root_sessions.return_value = [root("old")]
    bridge.clients["one"].get_session.return_value = root("old")
    bridge.clients["one"].messages.return_value = ([], None)
    bridge.clients["one"].sessions_by_id.side_effect = lambda sids: {
        sid: root(sid.removeprefix("devin-")) for sid in sids
    }
    return bridge


async def test_root_session_queries_paginate_and_filter():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["is_archived"] == "false"
        assert request.url.params.get_list("service_user_ids") == ["service-user-bot"]
        assert "qs" not in request.url.params
        if request.url.params.get("after"):
            return httpx.Response(200, json={"items": [root("new", 5)], "has_next_page": False})
        return httpx.Response(
            200,
            json={
                "items": [
                    root("old"),
                    root("child", parent_session_id="old"),
                    root("archived", is_archived=True),
                    root("other", service_user_id="other"),
                ],
                "end_cursor": "page2",
                "has_next_page": True,
            },
        )

    client = DevinClient(Account("org-one", "secret"), httpx.MockTransport(handler))
    assert [s["session_id"] for s in await client.root_sessions("service-user-bot")] == [
        "old",
        "new",
    ]
    assert len(requests) == 2
    assert requests[-1].url.params["after"] == "page2"


async def test_resolution_newest_active_and_cached(bridge):
    client = bridge.clients["one"]
    client.root_sessions.return_value = [
        root("old"),
        root("new", 3),
        root("dead", 4, status="exit"),
    ]
    await bridge.watch_session(target="bot")
    await bridge.send_message("hello", target="bot")
    client.root_sessions.assert_awaited_once_with("service-user-bot")
    client.get_session.assert_awaited_once_with("devin-new")
    client.send_message.assert_awaited_once_with("devin-new", "hello")
    state = json.loads(bridge.state.path.read_text())
    assert state["one"]["devin-new"]["targets"] == ["bot"]
    await bridge.unwatch_session(target="bot")
    assert json.loads(bridge.state.path.read_text())["one"] == {}


async def test_explicit_watch_survives_bot_migration(bridge):
    await bridge.watch_session(account="one", session_id="old")
    await bridge.watch_session(target="bot")
    client = bridge.clients["one"]
    client.get_session.side_effect = DevinAPIError(404, "old root gone")
    client.root_sessions.return_value = [root("new", 2)]
    await bridge.read_messages(target="bot")
    state = json.loads(bridge.state.path.read_text())["one"]
    assert state["devin-old"]["direct"]
    assert state["devin-new"]["targets"] == ["bot"]


@pytest.mark.parametrize("status", [404, 410])
async def test_retry_failed_send_and_follow_inbox(bridge, status):
    client = bridge.clients["one"]
    await bridge.watch_session(target="bot")
    client.root_sessions.return_value = [root("new", 2)]
    client.send_message.side_effect = [DevinAPIError(status, "old gone"), {"session_id": "new"}]
    assert await bridge.send_message("hello", target="bot") == {"session_id": "new"}
    assert [c.args[0] for c in client.send_message.await_args_list] == ["devin-old", "devin-new"]
    client.messages.return_value = ([{"event_id": "reply", "source": "devin"}], "cursor-new")
    inbox = await bridge.check_inbox()
    assert inbox["sessions"][0]["target"] == "bot"
    assert inbox["sessions"][0]["session_id"] == "devin-new"
    client.messages.assert_awaited_with("devin-new", None)
    state = json.loads(bridge.state.path.read_text())["one"]
    assert "devin-old" not in state
    assert state["devin-new"]["cursor"] == "cursor-new"


@pytest.mark.parametrize("status", ["exit", "error"])
async def test_inbox_migrates_terminal_root_without_reusing_cursor(bridge, status):
    client = bridge.clients["one"]
    client.messages.return_value = ([{"event_id": "old-reply", "source": "devin"}], "old-cursor")
    await bridge.read_messages(target="bot")
    client.get_session.return_value = root("old", status=status)
    client.root_sessions.return_value = [root("new", 2)]
    client.messages.return_value = ([{"event_id": "new-reply", "source": "devin"}], "new-cursor")
    result = await bridge.check_inbox()
    assert result["sessions"][0]["session_id"] == "devin-new"
    client.messages.assert_awaited_with("devin-new", None)


async def test_read_410_retries_once_and_no_active_root_is_error(bridge):
    client = bridge.clients["one"]
    await bridge.watch_session(target="bot")
    client.root_sessions.return_value = [root("new", 2)]
    client.messages.side_effect = [DevinAPIError(410, "gone"), DevinAPIError(410, "still gone")]
    with pytest.raises(DevinAPIError, match="still gone"):
        await bridge.read_messages(target="bot")
    assert client.messages.await_count == 2
    client.get_session.side_effect = DevinAPIError(410, "gone")
    client.root_sessions.return_value = []
    result = await bridge.check_inbox()
    assert "no active root" in result["sessions"][0]["error"]
    assert result["sessions"][0]["target"] == "bot"
    assert "devin-old" in json.loads(bridge.state.path.read_text())["one"]


async def test_suspended_does_not_resolve_again_and_403_not_retried(bridge):
    client = bridge.clients["one"]
    await bridge.watch_session(target="bot")
    client.get_session.return_value = root("old", status="suspended")
    client.send_message.side_effect = DevinAPIError(403, "denied")
    with pytest.raises(DevinAPIError, match="denied"):
        await bridge.send_message("hello", target="bot")
    assert client.root_sessions.await_count == 1
    assert client.send_message.await_count == 1


@pytest.mark.parametrize("successor", [True, False])
@pytest.mark.parametrize("inbox", [True, False])
async def test_final_reply_survives_termination(bridge, successor, inbox):
    await bridge.watch_session(target="bot")
    client = bridge.clients["one"]
    client.get_session.return_value = root("old", status="exit")
    client.root_sessions.return_value = [root("new", 2)] if successor else []

    async def messages(sid, after):
        if sid == "devin-old" and after is None:
            return ([{"event_id": "final", "source": "devin"}], "final-cursor")
        return ([], after)

    client.messages.side_effect = messages
    result = await bridge.check_inbox() if inbox else await bridge.read_messages(target="bot")
    replies = result["sessions"][0]["messages"] if inbox else result["messages"]
    assert [m["event_id"] for m in replies] == ["final"]
    again = await bridge.check_inbox()
    assert again == {"sessions": []}


async def test_send_migration_keeps_old_reply_for_next_inbox(bridge):
    await bridge.watch_session(target="bot")
    client = bridge.clients["one"]
    client.get_session.return_value = root("old", status="exit")
    client.root_sessions.return_value = [root("new", 2)]
    await bridge.send_message("next", target="bot")
    client.get_session.return_value = root("new", 2)
    client.messages.side_effect = [
        ([{"event_id": "final", "source": "devin"}], "old-last"),
        ([], None),
    ]
    result = await bridge.check_inbox()
    assert result["sessions"][0]["messages"][0]["event_id"] == "final"
    assert result["sessions"][0]["messages"][0]["session_id"] == "devin-old"


async def test_changed_bot_identity_invalidates_persisted_alias_cache(bridge):
    await bridge.watch_session(target="bot")
    state = json.loads(bridge.state.path.read_text())
    assert state["one"]["devin-old"]["target_service_user_ids"]["bot"] == "service-user-bot"
    bridge.config.targets["bot"]["bot_service_user_id"] = "service-user-replacement"
    client = bridge.clients["one"]
    client.root_sessions.return_value = [
        root("replacement", 2, service_user_id="service-user-replacement")
    ]
    await bridge.send_message("for replacement", target="bot")
    client.root_sessions.assert_awaited_with("service-user-replacement")
    client.get_session.assert_not_awaited()
    client.send_message.assert_awaited_once_with("devin-replacement", "for replacement")
    state = json.loads(bridge.state.path.read_text())["one"]
    assert "devin-old" not in state
    assert not state["devin-replacement"].get("previous_sessions")


async def test_legacy_cache_without_identity_is_resolved_again(bridge):
    async with bridge.state.transaction() as data:
        data["one"] = {"devin-old": {"cursor": "old-cursor", "targets": ["bot"], "direct": False}}
    client = bridge.clients["one"]
    client.root_sessions.return_value = [root("new", 2)]
    await bridge.send_message("hello", target="bot")
    client.root_sessions.assert_awaited_once_with("service-user-bot")
    client.send_message.assert_awaited_once_with("devin-new", "hello")


@pytest.mark.parametrize("by_alias", [True, False])
async def test_removed_alias_is_skipped_and_can_be_unwatched(bridge, by_alias):
    await bridge.watch_session(target="bot")
    bridge.file_targets.clear()
    result = await bridge.check_inbox()
    assert result["sessions"] == []
    assert result["skipped_targets"][0]["target"] == "bot"
    bridge.clients["one"].messages.assert_not_awaited()
    if by_alias:
        await bridge.unwatch_session(target="bot")
    else:
        await bridge.unwatch_session(account="one", session_id="old")
    assert json.loads(bridge.state.path.read_text())["one"] == {}
    assert await bridge.check_inbox() == {"sessions": []}


async def test_explicit_unwatch_removes_configured_alias_watch(bridge):
    await bridge.watch_session(target="bot")
    await bridge.unwatch_session(account="one", session_id="old")
    assert json.loads(bridge.state.path.read_text())["one"] == {}
    assert await bridge.check_inbox() == {"sessions": []}


async def test_retry_does_not_consume_drained_predecessor_reply(bridge):
    await bridge.watch_session(target="bot")
    client = bridge.clients["one"]
    client.get_session.return_value = root("old", status="exit")
    client.root_sessions.return_value = [root("current", 2)]
    await bridge.send_message("next", target="bot")
    client.get_session.return_value = root("current", 2)
    client.root_sessions.return_value = [root("replacement", 3)]
    calls = []

    async def messages(sid, after):
        calls.append((sid, after))
        if sid == "devin-current":
            raise DevinAPIError(410, "current gone")
        if sid == "devin-old" and after is None:
            return ([{"event_id": "final", "source": "devin"}], "final-cursor")
        return ([], after)

    client.messages.side_effect = messages
    result = await bridge.read_messages(target="bot")
    assert [m["event_id"] for m in result["messages"]] == ["final"]
    assert calls.count(("devin-old", None)) == 2
    assert result["session_id"] == "devin-replacement"


async def test_explicit_session_read_does_not_drain_bot_predecessors(bridge):
    await bridge.watch_session(target="bot")
    client = bridge.clients["one"]
    client.get_session.return_value = root("old", status="exit")
    client.root_sessions.return_value = [root("new", 2)]
    await bridge.send_message("next", target="bot")
    client.messages.return_value = ([{"event_id": "current", "source": "devin"}], "cursor-new")
    result = await bridge.read_messages(account="one", session_id="new")
    assert result["messages"] == [{"event_id": "current", "source": "devin"}]
    client.messages.assert_awaited_once_with("devin-new", None)
    state = json.loads(bridge.state.path.read_text())["one"]
    assert state["devin-old"]["cursor"] is None
    assert state["devin-new"]["previous_sessions"] == ["devin-old"]


async def test_list_bots_grouped_newest_and_empty(bridge):
    client = bridge.clients["one"]
    client.root_sessions.return_value = [
        root("old"),
        root("new", 3),
        root("human", 4, service_user_id=None),
        root("second", 2, service_user_id="service-user-other"),
    ]
    result = await bridge.list_bots("one")
    assert [b["session_id"] for b in result["bots"]] == ["devin-new", "devin-second"]
    assert result["bots"][0]["title"] == "Research"
    assert result["bots"][0]["status"] == "running"
    client.root_sessions.return_value = []
    assert await bridge.list_bots("one") == {"account": "one", "bots": []}
    client.root_sessions.side_effect = DevinAPIError(403, "denied")
    with pytest.raises(DevinAPIError, match="denied"):
        await bridge.list_bots("one")


@pytest.mark.parametrize(
    "fields,valid",
    [
        ('bot_service_user_id="service-user-bot"', True),
        ('bot_service_user_id="invalid"', False),
        ('bot_service_user_id="service-user-bot"\nsession_id="devin-a"', False),
        ('description="no identifier"', False),
    ],
)
def test_bot_config(monkeypatch, tmp_path, fields, valid):
    path = tmp_path / "config.toml"
    path.write_text(
        '[accounts.one]\norg_id="org-one"\napi_key="secret"\n'
        '[targets.bot]\naccount="one"\n' + fields
    )
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(path))
    if valid:
        assert load_config().resolve(target="bot") == ("one", None)
    else:
        with pytest.raises(ValueError):
            load_config()
