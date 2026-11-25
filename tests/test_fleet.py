import json

import pydantic_core
import pytest
from fake import Org, make

from devin_claude_bridge.cli import fleet_main
from devin_claude_bridge.config import load_config
from devin_claude_bridge.fleet import FleetStore, is_free, view, working

IDLE = {"status": "running", "status_detail": "waiting_for_user"}


def pinned(*aliases):
    return {alias: {"account": "work", "session_id": f"devin-{alias}"} for alias in aliases}


@pytest.fixture
def org():
    return Org()


def fleet_of(tmp_path, org, machines):
    """A bridge whose targets are these machines, each with the status given."""
    for alias, fields in machines.items():
        org.add(alias, **fields)
    return make(tmp_path, {"work": org}, pinned(*machines))


def rows(result):
    return {line.split(" | ")[0]: line.split(" | ") for line in result["machines"]}


@pytest.mark.parametrize(
    "status,detail,busy",
    [
        ("running", "working", True),
        ("claimed", "", True),
        ("resuming", "", True),
        ("running", "waiting_for_user", False),
        ("suspended", "", False),
        ("running", "finished", True),
    ],
)
def test_working_is_what_devin_reports(status, detail, busy):
    assert working(status, detail) is busy


def test_free_means_unheld_idle_and_reachable():
    saved = {"machines": {"held": {"holder": "s1", "task": "t"}}, "queue": []}
    machines = view(
        saved,
        {
            "idle": ("running", "waiting_for_user"),
            "held": ("running", "waiting_for_user"),
            "busy": ("running", "working"),
            "gone": ("ended", "archived"),
            "lost": ("unknown", ""),
        },
    )
    assert {a for a, m in machines.items() if is_free(m)} == {"idle"}


async def test_fleet_shows_live_status_holder_and_line(tmp_path, org):
    bridge = fleet_of(
        tmp_path,
        org,
        {
            "neon-1": IDLE,
            "buwiz-linux": {"status": "running", "status_detail": "working"},
            "buwiz-macos": {"status": "suspended"},
            "old-win": {"is_archived": True},
        },
    )
    await bridge.claim("buwiz-macos", "s1", "macOS CI for PR #1")
    bridge.queue("linux", "s2", "Linux CI")
    result = await bridge.fleet()
    table = rows(result)
    assert table["neon-1"][1:5] == ["neon", "waiting_for_user", "free", "-"]
    assert table["buwiz-linux"][2:4] == ["working", "busy"]
    assert table["buwiz-macos"][3:7] == ["busy", "s1", "0m", "macOS CI for PR #1"]
    assert table["old-win"][1:4] == ["windows", "ended archived", "busy"]
    assert result["line"] == ["#1 linux s2 0m: Linux CI"]
    assert result["neon"] == "1 of 4 allowed, Linux only"
    free = await bridge.fleet(free_only=True)
    assert list(rows(free)) == ["neon-1"]
    assert list(rows(await bridge.fleet("macos"))) == ["buwiz-macos"]
    with pytest.raises(ValueError, match="need is one of"):
        await bridge.fleet("gpu")


async def test_fleet_stays_small_with_25_machines(tmp_path, org):
    machines = {f"buwiz-forms-{p}-{n}": IDLE for p in ("linux", "macos", "win") for n in range(8)}
    machines["neon-1"] = IDLE
    bridge = fleet_of(tmp_path, org, machines)
    for alias in list(machines)[:20]:
        await bridge.claim(alias, "android-submission", "x" * 300)
        bridge.describe_target(alias, "linux", "free", "Android emulator and more " * 5)
    for n in range(5):
        bridge.queue("macos", f"session-{n}", "y" * 300)
    result = await bridge.fleet()
    assert len(result["machines"]) == 25
    # What the MCP client receives: FastMCP sends a result as JSON with indent 2.
    assert len(pydantic_core.to_json(result, indent=2)) < 4000


async def test_claim_refuses_a_machine_another_session_holds(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE, "mac-b": IDLE})
    bridge.describe_target("mac-a", "macos", "free")
    bridge.describe_target("mac-b", "macos", "free")
    await bridge.claim("mac-a", "s1", "iOS build")
    with pytest.raises(ValueError) as refused:
        await bridge.claim("mac-a", "s2", "other job")
    text = str(refused.value)
    assert "mac-a is held by s1 for: iOS build" in text
    assert "Free macos machines: mac-b." in text
    assert 'queue(need="macos", holder="s2"' in text
    with pytest.raises(ValueError, match="You hold mac-a already"):
        await bridge.claim("mac-a", "s1", "a different job")


async def test_claim_refuses_a_working_machine_nobody_holds(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"linux-a": {"status_detail": "working"}})
    with pytest.raises(ValueError, match="working right now"):
        await bridge.claim("linux-a", "s1", "build")
    started = await bridge.claim("linux-a", "s1", "build I started", started=True)
    assert started["claimed"]


async def test_claim_refuses_when_someone_is_ahead_in_line(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"neon-1": IDLE})
    first = bridge.queue("neon", "s1", "check the 1701Q layout")
    bridge.queue("neon", "s2", "check the 2550M layout")
    with pytest.raises(ValueError, match=f"#{first['ticket']} \\(s1\\) is ahead of you"):
        await bridge.claim("neon-1", "s2", "check the 2550M layout")
    await bridge.claim("neon-1", "s1", "check the 1701Q layout")
    assert [t["by"] for t in FleetStore(tmp_path / "fleet.json").read()["queue"]] == ["s2"]


async def test_queue_order_and_release_names_the_next_ticket(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"win-a": IDLE})
    await bridge.claim("win-a", "s1", "Windows CI")
    one = bridge.queue("windows", "s2", "packaging")
    two = bridge.queue("windows", "s3", "Android emulator")
    assert (one["position"], two["position"]) == (1, 2)
    assert two["ticket"] == one["ticket"] + 1
    with pytest.raises(ValueError, match="held by s1, not s2"):
        bridge.release("win-a", "s2")
    released = bridge.release("win-a", "s1")
    assert released["released"] and "#1 (s2), who may claim it now" in released["message"]
    bridge.unqueue(one["ticket"], "s2")
    with pytest.raises(ValueError, match="No ticket #1 of s2"):
        bridge.unqueue(one["ticket"], "s2")
    await bridge.claim("win-a", "s3", "Android emulator")
    assert FleetStore(tmp_path / "fleet.json").read()["queue"] == []


async def test_send_without_holder_works_with_a_warning(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    result = await bridge.send_message("hello", target="mac-a")
    assert org.sent == [("devin-mac-a", "hello")]
    assert "free machine" in result["warning"] and "become required" in result["warning"]


async def test_send_without_holder_is_refused_when_required(tmp_path, org, monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "key")
    monkeypatch.setenv("DEVIN_ORG_ID", "org-work")
    monkeypatch.setenv("DEVIN_BRIDGE_CONFIG", str(tmp_path / "none.toml"))
    (tmp_path / "none.toml").write_text("")
    assert load_config().require_holder is False
    monkeypatch.setenv("DEVIN_BRIDGE_REQUIRE_HOLDER", "1")
    assert load_config().require_holder is True
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    bridge.config.require_holder = True
    with pytest.raises(ValueError, match="Name holder"):
        await bridge.send_message("hello", target="mac-a")
    assert org.sent == []
    await bridge.send_message("build it", target="mac-a", holder="s1", kind="new")
    assert org.sent == [("devin-mac-a", "build it")]


async def test_half_named_sends_are_refused(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    for options in ({"holder": "s1"}, {"kind": "new"}, {"holder": "s1", "kind": "later"}):
        with pytest.raises(ValueError, match="Pass holder"):
            await bridge.send_message("hi", target="mac-a", **options)
    assert org.sent == []


async def test_new_task_on_a_free_machine_claims_it(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    result = await bridge.send_message(
        "\n  Run the macOS CI  \nfor PR #5", "mac-a", None, None, "s1", "new"
    )
    assert result["claimed"] == "mac-a is held by s1 now" and "warning" not in result
    held = FleetStore(tmp_path / "fleet.json").read()["machines"]["mac-a"]
    assert (held["holder"], held["task"]) == ("s1", "Run the macOS CI")
    # The work has started: another new task, even from the holder, is refused.
    with pytest.raises(ValueError, match="send it as kind='follow_up'"):
        await bridge.send_message("another job", target="mac-a", holder="s1", kind="new")
    await bridge.send_message("also run shard 2", target="mac-a", holder="s1", kind="follow_up")
    assert len(org.sent) == 2


async def test_a_claim_lets_its_first_new_message_through(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    await bridge.claim("mac-a", "s1", "iOS screenshots")
    org.sessions["devin-mac-a"]["status_detail"] = "working"
    await bridge.send_message("take the iPad shots", target="mac-a", holder="s1", kind="new")
    with pytest.raises(ValueError, match="kind='follow_up'"):
        await bridge.send_message("now something else", target="mac-a", holder="s1", kind="new")
    assert len(org.sent) == 1


async def test_follow_up_only_from_the_holder(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE, "mac-b": IDLE})
    bridge.describe_target("mac-a", "macos", "free")
    bridge.describe_target("mac-b", "macos", "free")
    await bridge.claim("mac-a", "s1", "iOS build")
    with pytest.raises(ValueError) as refused:
        await bridge.send_message("answer", target="mac-a", holder="s2", kind="follow_up")
    assert "held by s1 for: iOS build" in str(refused.value)
    assert "Nothing was sent" in str(refused.value) and "mac-b" in str(refused.value)
    with pytest.raises(ValueError, match="held by s1"):
        await bridge.send_message("new job", target="mac-a", holder="s2", kind="new")
    with pytest.raises(ValueError, match="held by nobody"):
        await bridge.send_message("answer", target="mac-b", holder="s2", kind="follow_up")
    assert org.sent == []
    await bridge.send_message("yes, go on", target="mac-a", holder="s1", kind="follow_up")
    assert org.sent == [("devin-mac-a", "yes, go on")]


async def test_the_guard_also_covers_a_session_named_by_id(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    await bridge.claim("mac-a", "s1", "iOS build")
    with pytest.raises(ValueError, match="held by s1"):
        await bridge.send_message("hi", account="work", session_id="mac-a", holder="s2", kind="new")
    assert org.sent == []


async def test_a_new_task_waits_for_the_line(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"neon-1": IDLE})
    bridge.queue("neon", "s1", "first")
    with pytest.raises(ValueError, match="ahead of you"):
        await bridge.send_message("second", target="neon-1", holder="s2", kind="new")
    await bridge.send_message("first", target="neon-1", holder="s1", kind="new")
    assert org.sent == [("devin-neon-1", "first")]


async def test_a_failed_send_undoes_its_claim(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    bridge.queue("macos", "s1", "the job")
    bridge.config.max_sends_per_hour = 1
    await bridge.send_message("earlier", target="mac-a")
    with pytest.raises(ValueError, match="maximum"):
        await bridge.send_message("the job", target="mac-a", holder="s1", kind="new")
    saved = FleetStore(tmp_path / "fleet.json").read()
    assert not saved["machines"].get("mac-a", {}).get("holder")
    assert [t["by"] for t in saved["queue"]] == ["s1"]


async def test_neon_is_linux_only_and_four_at_most(tmp_path, org):
    machines = {f"neon-{n}": IDLE for n in range(1, 5)} | {"spare": IDLE, "mac": IDLE}
    bridge = fleet_of(tmp_path, org, machines)
    with pytest.raises(ValueError, match="Linux only"):
        bridge.describe_target("mac", "macos", "neon")
    with pytest.raises(ValueError, match="Only 4 Neon machines"):
        bridge.describe_target("spare", "linux", "neon")
    bridge.describe_target("neon-4", "linux", "free")
    assert bridge.describe_target("spare", "linux", "neon")["pool"] == "neon"
    with pytest.raises(ValueError, match="platform is linux"):
        bridge.describe_target("spare", "android", "free")


async def test_create_session_keeps_the_neon_rules(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {f"neon-{n}": IDLE for n in range(1, 4)})
    bridge.config.allow_create = True
    with pytest.raises(ValueError, match="Linux only"):
        await bridge.create_session(
            "work", "p", alias="neon-4", platform="macos", devin_mode="lite"
        )
    made = await bridge.create_session(
        "work",
        "Check the form\nmore",
        alias="neon-4",
        platform="linux",
        devin_mode="lite",
        holder="s1",
    )
    assert made["target"] == "neon-4" and "warning" not in made
    held = FleetStore(tmp_path / "fleet.json").read()["machines"]["neon-4"]
    assert (held["pool"], held["holder"], held["task"]) == ("neon", "s1", "Check the form")
    with pytest.raises(ValueError, match="Only 4 Neon machines"):
        await bridge.create_session(
            "work", "p", alias="gemini", platform="linux", pool="neon", devin_mode="lite"
        )
    with pytest.raises(ValueError, match="held by s1"):
        await bridge.create_session(
            "work", "p", alias="neon-4", platform="linux", devin_mode="lite"
        )
    assert len(org.created) == 1
    plain = await bridge.create_session(
        "work", "p", alias="lin", platform="linux", devin_mode="lite"
    )
    assert "become required" in plain["warning"]


async def test_old_fleet_file_from_the_script_is_read(tmp_path, org):
    (tmp_path / "fleet.json").write_text(
        json.dumps(
            {
                "machines": {
                    "neon-1": {"platform": "linux", "pool": "neon", "extras": ""},
                    "box": {
                        "platform": "macos",
                        "pool": "free",
                        "extras": "iPhone simulator",
                        "holder": "orchestrator",
                        "task": "macOS CI for PR #310",
                        "since": 1795584456.6,
                    },
                },
                "queue": [{"n": 3, "need": "macos", "by": "s9", "task": "later", "at": 1.0}],
                "next": 4,
            },
            indent=1,
        )
    )
    bridge = fleet_of(tmp_path, org, {"neon-1": IDLE, "box": IDLE})
    table = rows(await bridge.fleet())
    assert table["box"][1:5] == ["macos", "waiting_for_user", "busy", "orchestrator"]
    assert "[iPhone simulator]" in table["box"][6]
    with pytest.raises(ValueError, match="held by orchestrator"):
        await bridge.send_message("hi", target="box", holder="s1", kind="new")
    # A claim made with the script may get its follow-ups.
    await bridge.send_message("go on", target="box", holder="orchestrator", kind="follow_up")
    assert bridge.queue("macos", "s1", "x")["ticket"] == 4
    released = bridge.release("box", "orchestrator")
    assert "#3 (s9)" in released["message"]


async def test_wait_and_read_hint_to_release_a_finished_machine(tmp_path, org):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    await bridge.send_message("build", target="mac-a", holder="s1", kind="new")
    org.say("mac-a", "done, report pushed")
    org.sessions["devin-mac-a"]["status_detail"] = "finished"
    result = await bridge.wait_for_reply(target="mac-a", timeout_seconds=1, poll_seconds=0.01)
    assert result["hint"].endswith("release when you have read this.")
    assert FleetStore(tmp_path / "fleet.json").read()["machines"]["mac-a"]["holder"] == "s1"
    read = await bridge.read_messages(target="mac-a", only_new=False)
    assert "release when you have read this" in read["hint"]
    bridge.release("mac-a", "s1")
    assert "hint" not in await bridge.read_messages(target="mac-a", only_new=False)


def test_cli_fleet_table_and_refusal(tmp_path, org, capsys):
    bridge = fleet_of(tmp_path, org, {"mac-a": IDLE})
    fleet_main(bridge, ["claim", "mac-a", "s1", "iOS build"])
    fleet_main(bridge, [])
    out = capsys.readouterr().out
    assert "mac-a is yours (s1): iOS build" in out and "Nobody is waiting in line." in out
    with pytest.raises(SystemExit, match="2"):
        fleet_main(bridge, ["claim", "mac-a", "s2", "x"])
    assert "refused: mac-a is held by s1" in capsys.readouterr().err
