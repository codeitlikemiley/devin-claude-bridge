import argparse
import asyncio
import json
import sys

from .bridge import Bridge
from .config import load_config
from .server import create_server

FLEET_USAGE = """devin-claude-bridge fleet [command]: who is using which Devin machine.

  fleet                                    every machine, live status, holder, task; the line
  fleet free [need]                        machines a new task may go to (neon|linux|macos|windows)
  fleet claim <alias> <session> "<task>"   take a free machine for a new task
        [--follow-up]                      same holder, same piece of work
        [--started]                        record work you already started on an unclaimed machine
  fleet release <alias> <session>          the work is reported; the machine is free again
  fleet queue <need> <session> "<task>"    no free machine of that kind: wait in line
  fleet unqueue <n> <session>              leave the line
  fleet set <alias> <platform> <pool> ["extras"]   describe a machine (pool: neon|free)
"""


def fleet_main(bridge, args):
    """The fleet tools from a shell, for sessions without the MCP tools and for scripts."""
    flags = {a for a in args if a.startswith("--")}
    args = [a for a in args if not a.startswith("--")]
    command, rest = (args[0], args[1:]) if args else ("table", [])
    try:
        if command in ("table", "free") and len(rest) <= 1:
            result = asyncio.run(bridge.fleet(rest[0] if rest else None, command == "free"))
            print(result["columns"])
            print("\n".join(result["machines"]))
            if command == "table":
                print(f"\nNeon (Gemini) machines: {result['neon']}.")
                print("\nWaiting in line:" if result["line"] else "\nNobody is waiting in line.")
                print("\n".join("  " + ticket for ticket in result["line"]))
            return
        if command == "claim" and len(rest) == 3 and flags <= {"--follow-up", "--started"}:
            result = asyncio.run(bridge.claim(*rest, "--follow-up" in flags, "--started" in flags))
            print(f"{result['target']} is yours ({result['holder']}): {result['task']}")
        elif command == "release" and len(rest) == 2 and not flags:
            print(bridge.release(*rest)["message"])
        elif command == "queue" and len(rest) == 3 and not flags:
            result = bridge.queue(*rest)
            print(
                f"#{result['ticket']}: {rest[1]} waits for a {result['need']} machine, "
                f"number {result['position']} in line: {result['task']}"
            )
        elif command == "unqueue" and len(rest) == 2 and rest[0].isdigit() and not flags:
            bridge.unqueue(int(rest[0]), rest[1])
            print(f"#{rest[0]} left the line.")
        elif command == "set" and len(rest) in (3, 4) and not flags:
            result = bridge.describe_target(*rest)
            print(f"{result['target']}: {result['platform']}, {result['pool']}")
        else:
            print(FLEET_USAGE, file=sys.stderr)
            raise SystemExit(1)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def main():
    if sys.argv[1:2] == ["fleet"]:
        try:
            bridge = Bridge(load_config())
        except (ValueError, OSError) as exc:
            print(f"devin-claude-bridge: {exc}", file=sys.stderr)
            raise SystemExit(1) from None
        return fleet_main(bridge, sys.argv[2:])
    parser = argparse.ArgumentParser(description="Multi-account Devin MCP bridge")
    parser.add_argument("command", choices=["serve", "inbox"], default="serve", nargs="?")
    parser.add_argument("--account", help="Only check this account (inbox)")
    args = parser.parse_args()
    try:
        bridge = Bridge(load_config())
        if args.command == "inbox":
            result = asyncio.run(bridge.check_inbox(args.account))
            print(json.dumps(result, indent=2))
            if any("error" in session for session in result["sessions"]):
                raise SystemExit(1)
        else:
            create_server(bridge).run(transport="stdio")
    except (ValueError, OSError) as exc:
        print(f"devin-claude-bridge: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
