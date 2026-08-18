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
from stromy_byok import build_keys_routes

from . import credentials, migrations, readiness, registry, service, upload_page
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

        def check() -> tuple[int, int]:
            with registry.connect() as conn:
                # Both chains, because this facade needs both: the core's for the
                # run lifecycle, its own for the upload tables. Reporting only the
                # core version would let a deployment look healthy while every
                # upload call fails at its first write.
                return registry.schema_version(conn), migrations.require_applied(conn)

        try:
            version, uploads_version = await asyncio.to_thread(check)
        except Exception as exc:
            return JSONResponse({"status": "error", "error": str(exc)}, status_code=503)
        return JSONResponse(
            {
                "status": "ok",
                "service": "stromy-workflows-mcp",
                "schema_version": version,
                "uploads_schema_version": uploads_version,
                # Optional-capability state rides along rather than living on a
                # second endpoint: this is the URL an operator already curls, and
                # a degraded capability nobody looks at is the failure mode the
                # declaration exists to remove. It is env reads only, so it costs
                # nothing on a liveness poll — and a degraded capability keeps
                # the 200, because degraded is not unhealthy.
                "readiness": readiness.readiness_report(),
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

    # ORG-PLAN-164 WS3 — the browser upload surface. Custom routes sit outside
    # the MCP OAuth boundary by design here: the person uploading holds a
    # capability link, not an Entra app role. See upload_page's module docstring
    # for why that is the narrower model rather than the weaker one.
    mcp.custom_route("/uploads/{session_id}", methods=["GET"])(upload_page.upload_page)
    mcp.custom_route("/uploads/{session_id}/urls", methods=["POST"])(upload_page.upload_urls)
    mcp.custom_route("/uploads/{session_id}/finalize", methods=["POST"])(
        upload_page.upload_finalize
    )

    # ORG-PLAN-206 C4 — the BYOK registration page, from the shared library.
    # Same boundary reasoning as the upload routes above: outside MCP OAuth by
    # design, because the person pasting the key holds a short-lived single-use
    # grant rather than an Entra app role. That is the narrower model, not the
    # weaker one — the grant binds subject, service, credential and action at
    # mint time, so the page's entire input surface is a token and a key.
    #
    # The store is resolved PER REQUEST through the factory, so provisioning the
    # vault after startup starts working without a redeploy.
    keys = build_keys_routes(
        catalogue=credentials.CATALOGUE,
        grant_store=credentials.GRANTS,
        store_factory=credentials.credential_store,
        service=credentials.SERVICE,
        audit_sink=service._audit,
    )
    mcp.custom_route("/keys", methods=["GET"])(keys.get)
    mcp.custom_route("/keys", methods=["POST"])(keys.post)
