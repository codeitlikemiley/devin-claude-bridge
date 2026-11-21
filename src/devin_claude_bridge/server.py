import logging

from mcp.server.fastmcp import FastMCP

from .bridge import Bridge
from .config import load_config


def create_server(bridge=None):
    bridge = bridge or Bridge(load_config())
    server = FastMCP("devin-claude-bridge")
    # FastMCP turns on INFO logging. A line per HTTP request and per tool call is
    # noise on stderr, and the request line carries the organization ID.
    for noisy in ("httpx", "mcp.server.lowlevel.server"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in (
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
    ):
        server.tool()(getattr(bridge, name))
    return server
