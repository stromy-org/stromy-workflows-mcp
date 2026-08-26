"""Chrome-regression tests (ORG-PLAN-086 §4).

CHROME — do not delete. These protect framework contracts that must never
regress: the `fs.py` path-jail (security-critical) and the `fs_read`/`fs_list`
skill-serving tools. `copier update` keeps this file current. Project-specific
tests live in the project-owned `test_server.py`.
"""

import pytest
from fastmcp.exceptions import ToolError


async def test_chrome_metadata(client):
    tools = await client.list_tools()
    tool_names = {t.name for t in tools}
    assert {"fs_read", "fs_list"} <= tool_names


async def test_fs_list_skills(client):
    result = await client.call_tool(name="fs_list", arguments={"path": "skills"})
    names = {entry["name"] for entry in result.data}
    assert "server-guide" in names


async def test_fs_read_skill(client):
    result = await client.call_tool(
        name="fs_read", arguments={"path": "skills/server-guide/SKILL.md"}
    )
    # fs_read returns a page object, not a bare string — the body is in
    # `content`, and `next_offset_chars` is the continuation handle.
    assert result.data["content"]
    assert result.data["next_offset_chars"] is None
    assert result.data["truncated"] is False


async def test_fs_read_traversal_blocked(client):
    with pytest.raises(ToolError):
        await client.call_tool(name="fs_read", arguments={"path": "../pyproject.toml"})


async def test_fs_read_outside_roots_blocked(client):
    with pytest.raises(ToolError):
        await client.call_tool(name="fs_read", arguments={"path": "src/config.py"})
