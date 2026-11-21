import asyncio
import json

import pytest
from fake import Org, make

from devin_claude_bridge.client import DevinAPIError


@pytest.fixture
def org():
    return Org()


@pytest.fixture
async def bridge(tmp_path, org):
    org.add("task")
    bridge = make(tmp_path, {"work": org})
    await bridge.add_target("task", "task")
    org.reads = 0
    org.calls.clear()
    return bridge


def wait(bridge, **options):
    options = {"timeout_seconds": 1, "poll_seconds": 0.01, **options}
    return bridge.wait_for_reply(account="work", session_id="task", **options)


def texts(result):
    return [message["message"] for message in result["messages"]]


async def test_a_reply_that_came_before_the_wait_is_returned_at_once(bridge, org):
    await bridge.send_message("question", account="work", session_id="task")
    org.say("task", "fast answer")
    result = await wait(bridge)
    assert texts(result) == ["fast answer"] and not result["timed_out"]
    assert (result["status"], result["status_detail"]) == ("running", None)
    assert org.reads == 1
    assert await bridge.check_inbox() == {"sessions": []}


async def test_older_unread_replies_are_kept_and_returned_with_the_answer(bridge, org):
    await bridge.watch_session(account="work", session_id="task")
    org.say("task", "old news")
    await bridge.send_message("question", account="work", session_id="task")

    async def answers_on_the_third_read(count):
        if count == 3:
            org.say("task", "the answer")

    org.on_read = answers_on_the_third_read
    result = await wait(bridge)
    assert texts(result) == ["old news", "the answer"] and not result["timed_out"]
    assert org.reads == 3


async def test_no_reply_times_out_with_the_status(bridge, org):
    org.sessions["devin-task"]["status_detail"] = "working"
    await bridge.send_message("question", account="work", session_id="task")
    result = await wait(bridge, timeout_seconds=0.03)
    assert result["timed_out"] and result["messages"] == []
    assert result["status_detail"] == "working" and "ended" not in result


async def test_the_last_read_is_at_the_deadline_even_after_a_slow_first_read(bridge, org):
    async def slow_then_answers(count):
        if count == 1:
            await asyncio.sleep(0.05)
        else:
            org.say("task", "arrived")

    org.on_read = slow_then_answers
    result = await wait(bridge, timeout_seconds=0.01, poll_seconds=15)
    assert texts(result) == ["arrived"] and not result["timed_out"]
    assert org.reads == 2


async def test_zero_timeout_reads_once(bridge, org):
    result = await wait(bridge, timeout_seconds=0)
    assert result["timed_out"] and org.reads == 1


async def test_until_idle_collects_the_whole_answer(bridge, org):
    session = org.sessions["devin-task"]
    session["status_detail"] = "working"
    await bridge.send_message("do the thing", account="work", session_id="task")
    org.say("task", "on it")

    async def finishes_on_the_third_read(count):
        if count == 3:
            org.say("task", "done, here is the result")
            session["status_detail"] = "waiting_for_user"

    org.on_read = finishes_on_the_third_read
    first_line = await wait(bridge)
    assert texts(first_line) == ["on it"] and first_line["status_detail"] == "working"
    org.say("task", "still going")
    whole = await wait(bridge, until_idle=True)
    assert texts(whole) == ["still going", "done, here is the result"]
    assert whole["status_detail"] == "waiting_for_user" and not whole["timed_out"]


async def test_until_idle_does_not_stop_on_an_idle_status_from_before_the_reply(bridge, org):
    org.sessions["devin-task"]["status_detail"] = "waiting_for_user"
    await bridge.send_message("question", account="work", session_id="task")

    async def answers_on_the_second_read(count):
        if count == 2:
            org.say("task", "answer")

    org.on_read = answers_on_the_second_read
    result = await wait(bridge, until_idle=True)
    assert texts(result) == ["answer"] and org.reads == 2


async def test_a_user_message_after_devins_means_no_reply_yet(bridge, org):
    await bridge.watch_session(account="work", session_id="task")
    org.say("task", "earlier")
    await bridge.send_message("follow-up", account="work", session_id="task")
    result = await wait(bridge, timeout_seconds=0.03)
    assert result["timed_out"] and texts(result) == ["earlier"]


@pytest.mark.parametrize(
    "field,value,reason", [("is_archived", True, "archived"), ("status", "exit", "ended")]
)
async def test_the_wait_stops_when_the_session_ends(bridge, org, field, value, reason):
    await bridge.watch_session(account="work", session_id="task")

    async def ends_on_the_second_read(count):
        if count == 2:
            org.say("task", "goodbye")
            org.sessions["devin-task"][field] = value

    org.on_read = ends_on_the_second_read
    result = await wait(bridge, timeout_seconds=30, poll_seconds=0.01)
    assert result["ended"] == reason and not result["timed_out"]
    assert texts(result) == ["goodbye"]


async def test_replies_collected_before_a_failed_read_are_returned_not_lost(bridge, org):
    session = org.sessions["devin-task"]
    session["status_detail"] = "working"
    await bridge.send_message("run them", account="work", session_id="task")
    org.say("task", "Running tests.")

    async def gone_on_the_second_read(count):
        if count == 2:
            raise DevinAPIError(500, "Devin API 500: try later")

    org.on_read = gone_on_the_second_read
    result = await wait(bridge, until_idle=True)
    assert texts(result) == ["Running tests."]
    assert "500" in result["error"] and not result["timed_out"]
    assert await bridge.check_inbox() == {"sessions": []}


async def test_until_idle_right_after_a_plain_wait_sees_devin_finished(bridge, org):
    session = org.sessions["devin-task"]
    session["status_detail"] = "working"
    await bridge.send_message("question", account="work", session_id="task")
    org.say("task", "the answer")
    assert texts(await wait(bridge)) == ["the answer"]
    session["status_detail"] = "finished"
    org.reads = 0
    result = await wait(bridge, until_idle=True)
    assert result["messages"] == [] and not result["timed_out"]
    assert result["status_detail"] == "finished" and org.reads == 1
    await bridge.send_message("another", account="work", session_id="task")
    unanswered = await wait(bridge, until_idle=True, timeout_seconds=0.03)
    assert unanswered["timed_out"]


async def test_a_session_the_bridge_never_read_is_looked_up_once(tmp_path, org):
    org.add("fresh", status_detail="finished")
    org.say("fresh", "all done")
    bridge = make(tmp_path, {"work": org})
    await bridge.watch_session(account="work", session_id="fresh")
    await bridge.read_messages(account="work", session_id="fresh")
    state = json.loads(bridge.state.path.read_text())
    del state["work"]["devin-fresh"]["answered"]  # as a state file from before this field
    bridge.state.path.write_text(json.dumps(state))
    org.calls.clear()
    result = await bridge.wait_for_reply(
        account="work", session_id="fresh", until_idle=True, timeout_seconds=0.02, poll_seconds=0.01
    )
    assert not result["timed_out"] and result["messages"] == []
    assert [c for c in org.calls if c[0] == "messages"] == [("messages", "devin-fresh")] * 2
    assert json.loads(bridge.state.path.read_text())["work"]["devin-fresh"]["answered"] is True


async def test_wait_errors(bridge, org):
    with pytest.raises(ValueError, match="timeout_seconds"):
        await wait(bridge, poll_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        await wait(bridge, timeout_seconds=3601)
    with pytest.raises(ValueError, match="Unknown account"):
        await bridge.wait_for_reply(account="nope", session_id="task")
    del org.sessions["devin-task"]
    with pytest.raises(ValueError, match="404"):
        await wait(bridge)
