import sys
from pathlib import Path

import pytest
from fastmcp.client import Client

# Put `src/` on sys.path so tests can `from stromy_workflows_mcp.server import mcp`.
SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from stromy_workflows_mcp.server import mcp  # noqa: E402


@pytest.fixture
async def client():
    async with Client(transport=mcp) as c:
        yield c
