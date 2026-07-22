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

import asyncio
from typing import TYPE_CHECKING

from starlette.requests import Request
from starlette.responses import JSONResponse

from . import registry
from .config import settings

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    """Replace the template liveness route with registry-aware readiness."""

    async def health(request: Request) -> JSONResponse:
        del request
        if not settings.stromy_pg_dsn:
            return JSONResponse(
                {"status": "error", "error": "STROMY_PG_DSN is unset"}, status_code=503
            )

        def check() -> int:
            with registry.connect() as conn:
                return registry.schema_version(conn)

        try:
            version = await asyncio.to_thread(check)
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=503)
        return JSONResponse(
            {
                "status": "ok",
                "service": "stromy-workflows-mcp",
                "schema_version": version,
            }
        )

    # The template owns server.py and seeds a generic /health route before this
    # extension seam runs. Replace that route here rather than editing template
    # chrome; `_additional_http_routes` is FastMCP's backing list for
    # `custom_route` and is the only place the seeded route exists.
    mcp._additional_http_routes[:] = [
        route for route in mcp._additional_http_routes if getattr(route, "path", None) != "/health"
    ]
    mcp.custom_route("/health", methods=["GET"])(health)
