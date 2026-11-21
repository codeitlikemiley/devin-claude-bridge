from mcp.server.fastmcp import FastMCP

from .bridge import Bridge
from .config import load_config


def create_server(bridge=None):
    bridge = bridge or Bridge(load_config())
    server = FastMCP("devin-claude-bridge")
    for name in (
        "list_accounts",
        "list_sessions",
        "create_session",
        "send_message",
        "read_messages",
        "wait_for_reply",
        "check_inbox",
        "watch_session",
        "unwatch_session",
    ):
        server.tool()(getattr(bridge, name))
    return server
