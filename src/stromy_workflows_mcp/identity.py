"""Current-request identity helpers."""

from __future__ import annotations

import logging
from typing import Any

from .config import settings
from .scoping import CallerScope, resolve_scope

logger = logging.getLogger(__name__)


def read_claims() -> dict[str, Any]:
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
    except Exception as exc:
        logger.debug("no request token; using local operator scope", exc_info=exc)
        return {}
    return dict(token.claims) if token and token.claims else {}


def caller_scope() -> CallerScope:
    return resolve_scope(
        read_claims(),
        role_prefix=settings.client_role_prefix,
        operator_role=settings.operator_role,
        scope_map=settings.client_scope_map,
    )
