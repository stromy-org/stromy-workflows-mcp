"""Default-deny Entra app-role scoping for workflow runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class CallerScope:
    client_slugs: frozenset[str]
    unrestricted: bool = False


def _slug(value: str) -> str:
    if not _SLUG_RE.fullmatch(value):
        raise PermissionError(f"invalid client slug in verified role: {value!r}")
    return value


def _roles(claims: dict[str, Any]) -> list[str]:
    raw = claims.get("roles")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(role) for role in raw]
    return []


def resolve_scope(
    claims: dict[str, Any],
    *,
    role_prefix: str,
    operator_role: str,
    scope_map: str = "",
) -> CallerScope:
    """Resolve authority only from verified claims.

    Tokenless stdio/tests are the local operator. A token with no recognized
    app role is denied; it never inherits the local fallback.
    """
    if not claims:
        return CallerScope(frozenset(), unrestricted=True)
    if not claims.get("sub"):
        return CallerScope(frozenset())
    roles = _roles(claims)
    if operator_role and operator_role in roles:
        return CallerScope(frozenset(), unrestricted=True)
    slugs = frozenset(
        _slug(role[len(role_prefix) :]) for role in roles if role.startswith(role_prefix)
    )
    if slugs:
        return CallerScope(slugs)
    subject = str(claims["sub"])
    for row in scope_map.split(","):
        key, sep, value = row.strip().partition("=")
        if sep and key == subject:
            return CallerScope(frozenset({_slug(value.strip())}))
    return CallerScope(frozenset())


def require_client(scope: CallerScope, requested: str | None) -> str:
    """Resolve one run owner, rejecting cross-client, ambiguous, or implied requests.

    The owner is not bookkeeping: the runner derives the run's ``brand_slug`` from
    it, so this string decides whose brand the deliverable ships in. Identity-shaped
    resolution therefore fails closed — it never substitutes a literal tenant slug
    for an answer the caller did not give. An operator may name *any* client (the
    deliberate cross-brand override), but must name one; the previous
    ``requested or "stromy"`` silently produced a Stromy-branded deliverable for a
    run the caller believed was someone else's.

    The cron path never reaches here — ``runtime.scheduled`` registers runs with no
    owner at all, which leaves ``brand_slug`` null and fails the brand gate visibly.
    """
    if scope.unrestricted:
        if not requested:
            raise PermissionError(
                "client_slug is required: it selects the run owner, which determines "
                "the deliverable's brand, and an operator implies no single client"
            )
        return _slug(requested)
    if requested:
        slug = _slug(requested)
        if slug not in scope.client_slugs:
            raise PermissionError(f"caller is not authorized for client {slug!r}")
        return slug
    if len(scope.client_slugs) != 1:
        raise PermissionError("client_slug is required when the caller has zero or multiple roles")
    return next(iter(scope.client_slugs))
