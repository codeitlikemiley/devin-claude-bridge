import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_smoke(tmp_path):
    env = {
        **os.environ,
        "DEVIN_API_KEY": "fake-key",
        "DEVIN_ORG_ID": "org-smoke",
        "DEVIN_BRIDGE_CONFIG": str(tmp_path / "config.toml"),
        "DEVIN_BRIDGE_STATE": str(tmp_path / "state.json"),
    }
    (tmp_path / "config.toml").write_text("")
    params = StdioServerParameters(command="devin-claude-bridge", args=["serve"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.list_tools()
        names = sorted(tool.name for tool in result.tools)
        assert len(names) == 10
        response = await session.call_tool("list_accounts", {})
        assert not response.isError
        assert "fake-key" not in str(response)
        print("STDIO initialized; tools: " + ", ".join(names))
        print("STDIO list_accounts: " + response.content[0].text)
