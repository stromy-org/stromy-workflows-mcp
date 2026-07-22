"""Stromy Workflows MCP (FastMCP 3.0).

Uses FileSystemProvider for automatic component discovery from components/.
"""

from pathlib import Path
from typing import Any, cast

from fastmcp import FastMCP
from fastmcp.server.providers import FileSystemProvider
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import build_auth_provider
from .config import settings
from .logging import setup_logging
from .middleware import ToolCallLoggingMiddleware

setup_logging()

PROJECT_ROOT = Path(__file__).parent.parent.parent
COMPONENTS_DIR = PROJECT_ROOT / "components"

mcp = FastMCP(
    name="Stromy Workflows MCP",
    instructions=(
        "Hosted workflow discovery, validation, execution, and lifecycle facade for Stromy\n\n"
        "This server hosts skills (procedural guides) as files under skills/. "
        'Call fs_list("skills") to discover them, then '
        'fs_read("skills/<name>/SKILL.md") to load one and follow its '
        "instructions. Use fs_read/fs_list for any other shipped content too."
    ),
    version="0.1.0",
    providers=[
        FileSystemProvider(COMPONENTS_DIR, reload=settings.mcp_dev_mode),
    ],
    auth=build_auth_provider(),
    middleware=[ToolCallLoggingMiddleware()],
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "stromy-workflows-mcp"})


# --- Project extension seam (ORG-PLAN-086 §3) ---------------------------------
# CHROME — do not hand-edit this file. Add ALL project-specific wiring (custom
# routes, startup handlers, extra providers, non-FileSystemProvider component
# registration) in server_hooks.py, which this file imports. Inverting the
# dependency this way keeps server.py pure template so `copier update` can
# overwrite it freely without ever touching project code.
try:
    from . import server_hooks

    server_hooks.register(mcp)
except ImportError:
    pass  # no project hooks module — fine (the seeded seam is a no-op anyway)


if __name__ == "__main__":
    transport_kwargs: dict[str, Any] = {}
    if settings.fastmcp_transport != "stdio":
        transport_kwargs["host"] = settings.fastmcp_host
        transport_kwargs["port"] = settings.fastmcp_port
    mcp.run(transport=cast(Any, settings.fastmcp_transport), **transport_kwargs)
