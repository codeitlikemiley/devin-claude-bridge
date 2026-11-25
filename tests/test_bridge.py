import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from devin_claude_bridge.bridge import Bridge
from devin_claude_bridge.config import Account, Config
from devin_claude_bridge.server import create_server
from devin_claude_bridge.state import State


def message(event, source="devin"):
    return {"event_id": event, "source": source, "message": event, "created_at": 1}


@pytest.fixture
def bridge(tmp_path):
    config = Config(
        {"one": Account("org-one", "secret"), "two": Account("org-two", "second-secret")},
        {"bot": {"account": "one", "session_id": "devin-a"}},
    )
    bridge = Bridge(config, State(tmp_path / "state.json"), lambda a: AsyncMock())
    for client in bridge.clients.values():
        client.get_session.return_value = {"status": "running", "is_archived": False}
        client.send_message.return_value = {}
        client.sessions_by_id.side_effect = lambda sids: {
            sid: {"status": "running", "is_archived": False} for sid in sids
        }
    return bridge


async def test_create_send_watch_unwatch(bridge):
    bridge.clients["one"].create_session.return_value = {"session_id": "a"}
    bridge.config.allow_create = True
    await bridge.create_session("one", "prompt", "title", devin_mode="lite")
    await bridge.send_message("hello", target="bot")
    bridge.clients["one"].send_message.assert_awaited_once_with("devin-a", "hello")
    data = json.loads(bridge.state.path.read_text())
    assert "devin-a" in data["one"]
    await bridge.unwatch_session(target="bot")
    assert json.loads(bridge.state.path.read_text())["one"] == {}
    bridge.clients["one"].send_message.side_effect = ValueError("409 blocked")
    with pytest.raises(ValueError, match="409"):
        await bridge.send_message("hello", target="bot")
    assert json.loads(bridge.state.path.read_text())["one"] == {}
    assert "secret" not in json.dumps(bridge.list_accounts())


async def test_read_inbox_positions_and_filtering(bridge):
    client = bridge.clients["one"]
    client.messages.side_effect = [
        ([message("1"), message("2", "user")], "c2"),
        ([], "c2"),
        ([message("3")], "c3"),
        ([message("1"), message("2", "user"), message("3")], "c3"),
    ]
    first = await bridge.read_messages(target="bot")
    assert [m["event_id"] for m in first["messages"]] == ["1"]
    assert await bridge.check_inbox() == {"sessions": []}
    inbox = await bridge.check_inbox("one")
    assert inbox["sessions"][0]["messages"] == [message("3")]
    client.messages.assert_awaited_with("devin-a", "c2")
    replay = await bridge.read_messages(target="bot", only_new=False, include_user=True)
    assert len(replay["messages"]) == 3
    client.messages.assert_awaited_with("devin-a", None)
    position = json.loads(bridge.state.path.read_text())["one"]["devin-a"]
    assert position == {"cursor": "c3", "event_id": "3", "direct": True, "answered": True}


async def test_inbox_multiple_accounts_and_error_retains_cursor(bridge):
    await bridge.watch_session(target="bot")
    await bridge.watch_session(account="two", session_id="a")
    bridge.clients["one"].messages.side_effect = ValueError("403 denied")
    bridge.clients["two"].messages.return_value = ([message("reply")], "c1")
    inbox = await bridge.check_inbox()
    assert inbox["sessions"][0]["error"] == "403 denied"
    assert inbox["sessions"][1]["account"] == "two"
    data = json.loads(bridge.state.path.read_text())
    assert data["one"]["devin-a"]["cursor"] is None
    assert data["two"]["devin-a"]["cursor"] == "c1"
    with pytest.raises(ValueError, match="Unknown account"):
        await bridge.check_inbox("missing")


async def test_cursorless_last_page_dedup(bridge):
    bridge.clients["one"].messages.side_effect = [
        ([message("a")], None),
        ([message("a"), message("b")], None),
        ([message("a"), message("b")], None),
    ]
    assert len((await bridge.read_messages(target="bot"))["messages"]) == 1
    assert (await bridge.read_messages(target="bot"))["messages"] == [message("b")]
    assert (await bridge.read_messages(target="bot"))["messages"] == []


async def test_state_rollback_and_concurrent_transactions(bridge):
    await bridge.watch_session(target="bot")
    original = bridge.state.path.read_text()
    with pytest.raises(ValueError):
        async with bridge.state.transaction() as data:
            data.clear()
            raise ValueError("rollback")
    assert bridge.state.path.read_text() == original
    await asyncio.gather(
        *(bridge.watch_session(account="one", session_id=f"s{i}") for i in range(5))
    )
    assert len(json.loads(bridge.state.path.read_text())["one"]) == 6
    assert bridge.state.path.stat().st_mode & 0o777 == 0o600
    bridge.state.path.write_text("broken")
    with pytest.raises(ValueError):
        await bridge.check_inbox()


async def test_tools_registered(bridge):
    server = create_server(bridge)
    tools = await server.list_tools()
    assert {t.name for t in tools} == {
        "list_accounts",
        "list_bots",
        "list_sessions",
        "create_session",
        "add_target",
        "remove_target",
        "send_message",
        "read_messages",
        "wait_for_reply",
        "check_inbox",
        "watch_session",
        "unwatch_session",
        "fleet",
        "claim",
        "release",
        "queue",
        "unqueue",
        "describe_target",
    }
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("mcp.server.lowlevel.server").level == logging.WARNING
    schemas = {t.name: t.inputSchema for t in tools}
    assert schemas["send_message"]["required"] == ["message"]
