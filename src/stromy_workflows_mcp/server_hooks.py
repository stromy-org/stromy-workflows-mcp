"""Project extension seam for Stromy Workflows MCP (ORG-PLAN-086 §3).

INDIVIDUALITY — this file is yours. It is seeded once by the template and then
never touched by `copier update`, so put every project-specific server tweak
here instead of editing the chrome `server.py`.

`register(mcp)` is called from `server.py` at import time, after the `mcp`
instance is built and the FileSystemProvider has been attached. Use it to:

  * add custom Starlette routes:      @mcp.custom_route("/foo", methods=["GET"])
  * register startup/shutdown hooks
  * attach extra providers or middleware not covered by auto-discovery
  * wire domain settings from your Settings subclass in config.py

Most components (tools/resources/prompts) need NO wiring here — dropping a file
into components/ is enough (FileSystemProvider auto-discovers it). This seam is
only for things that must touch the `mcp` object directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Wire project-specific server behavior. No-op by default."""
    return
