import argparse
import asyncio
import json
import sys

from .bridge import Bridge
from .config import load_config
from .server import create_server


def main():
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
