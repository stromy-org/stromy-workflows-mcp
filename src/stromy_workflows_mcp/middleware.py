"""Middleware that logs every MCP tool call with input, user identity, and duration."""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext

logger = logging.getLogger(__name__)


def _identify_user() -> str:

    try:
        token = get_access_token()
        if token and token.claims:
            return (
                token.claims.get("email")
                or token.claims.get("preferred_username")
                or token.claims.get("sub")
                or "authenticated"
            )
    except Exception:  # noqa: S110 — best-effort attribution; never break a tool call on a claims lookup
        pass

    return "anonymous"


def _extract_client_slug(tool_input: object) -> str:
    """Best-effort client attribution for the tool_call log (ORG-PLAN-046).

    Reads the canonical top-level ``client_slug`` from the ``brand_context`` the
    caller passes (with a ``meta.client_slug`` fallback). Never raises and never
    alters the tool call — a missing/absent slug logs ``"unknown"`` (a tool that
    takes no ``brand_context``, or an ad-hoc unbranded call)."""
    if not isinstance(tool_input, dict):
        return "unknown"
    bc = cast("dict[str, object]", tool_input).get("brand_context")
    if not isinstance(bc, dict):
        return "unknown"
    bc_map = cast("dict[str, object]", bc)
    slug = bc_map.get("client_slug")
    if not slug:
        meta = bc_map.get("meta")
        if isinstance(meta, dict):
            slug = cast("dict[str, object]", meta).get("client_slug")
    return str(slug) if slug else "unknown"


class ToolCallLoggingMiddleware(Middleware):

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: Callable[[MiddlewareContext], Awaitable[Any]]
    ):
        tool_name = context.message.name
        tool_input = context.message.arguments or {}
        user_email = _identify_user()
        start = time.perf_counter()

        fields = {
            "event": "tool_call",
            "tool": tool_name,
            "input": tool_input,
            "user_email": user_email,
            "client_slug": _extract_client_slug(tool_input),
        }

        try:
            result = await call_next(context)
            duration_ms = round((time.perf_counter() - start) * 1000)
            fields.update({"duration_ms": duration_ms, "status": "ok"})
            logger.info(
                "tool_call %s",
                tool_name,
                extra={"json_fields": fields},
            )
            return result
        except Exception as exc:
            duration_ms = round((time.perf_counter() - start) * 1000)
            fields.update({
                "duration_ms": duration_ms,
                "status": "error",
                "error": str(exc),
            })
            logger.error(
                "tool_call %s failed",
                tool_name,
                extra={"json_fields": fields},
            )
            raise
