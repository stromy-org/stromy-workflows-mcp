"""Domain tests for Stromy Workflows MCP.

INDIVIDUALITY — this file is yours. It is seeded once with a placeholder that
exercises the `echo` example tool; replace these with tests for your real tools
as you build them. The framework-contract tests (fs path-jail, fs_read/fs_list)
live in the chrome `test_chrome.py` — leave those alone.

The `client` fixture is provided by `conftest.py`.
"""


async def test_echo_tool(client):
    result = await client.call_tool(name="echo", arguments={"message": "hi"})
    assert result.data == "hi"


async def test_server_metadata(client):
    tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    assert "echo" in tool_names
