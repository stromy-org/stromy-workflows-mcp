"""Per-client workflow entitlement — which workflows a client role may see and start.

The fourth isolation layer of the execution plane, beside authentication
(`scoping.resolve_scope`), run tenancy (`service._require_run_scope`), and config
tiering (`contracts.Contract`). Tenancy answers "whose *rows* are these"; this
module answers "whose *workflows* are these".

Authority lives in an authored registry, `components/resources/entitlements.json`
— deliberately NOT in the contract JSON, which is generated read-only from Stromy.
Entitlement is a commercial fact owned by this authorization layer, not a
workflow-definition fact owned by the execution layer.

Three invariants this module must keep:

1. The operator bypasses before the registry is ever read, so a malformed file
   can never lock the operator out of their own estate.
2. Clients fail closed. An unreadable registry denies everything; it never
   degrades to "allow", which would silently reopen the whole catalog.
3. For a client, an unknown workflow and an unentitled one are indistinguishable
   — `components/tools/workflows.py` forwards `str(exc)` verbatim to the caller,
   so distinct messages would let a client enumerate the catalog by diffing
   errors. The operator keeps the diagnostic message.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .config import settings
from .contracts import PROJECT_ROOT, ContractError, list_contracts
from .scoping import _SLUG_RE, CallerScope

logger = logging.getLogger(__name__)


class EntitlementError(ValueError):
    pass


def entitlements_path() -> Path:
    return (PROJECT_ROOT / settings.entitlements_file).resolve()


def _parse(raw: Any) -> dict[str, frozenset[str]]:
    workflows = raw.get("workflows") if isinstance(raw, dict) else None
    if not isinstance(workflows, dict):
        raise EntitlementError("entitlements registry has no 'workflows' object")
    table: dict[str, frozenset[str]] = {}
    for name, entry in workflows.items():
        if not isinstance(entry, dict):
            raise EntitlementError(f"entitlement entry {name!r} must be an object")
        clients = entry.get("clients", [])
        if not isinstance(clients, list):
            raise EntitlementError(f"entitlement entry {name!r} has a non-list 'clients'")
        for slug in clients:
            # Same slug grammar as a verified `client.<slug>` role, so a typo here
            # can never resolve against a role shape that scoping.py would reject.
            if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
                raise EntitlementError(f"entitlement entry {name!r} has invalid slug {slug!r}")
        table[name] = frozenset(clients)
    return table


def load_entitlements() -> dict[str, frozenset[str]]:
    """Parse the registry, raising on any problem. Callers decide the failure posture."""
    path = entitlements_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EntitlementError(f"cannot load entitlements from {path}: {exc}") from exc
    return _parse(raw)


def _table() -> dict[str, frozenset[str]]:
    """Fail closed: an unreadable registry denies every client, never allows one."""
    try:
        return load_entitlements()
    except EntitlementError as exc:
        logger.error("workflow entitlements unreadable; denying all client access: %s", exc)
        return {}


def entitled_clients(workflow: str) -> frozenset[str]:
    """Client slugs granted this workflow. Empty means operator-only."""
    return _table().get(workflow, frozenset())


def visible_workflows(scope: CallerScope) -> list[str]:
    if scope.unrestricted:
        return list_contracts()
    table = _table()
    return [name for name in list_contracts() if table.get(name, frozenset()) & scope.client_slugs]


def require_visible(workflow: str, scope: CallerScope) -> None:
    """Discovery gate — any of the caller's roles entitles them (union)."""
    if scope.unrestricted:
        return
    if not _table().get(workflow, frozenset()) & scope.client_slugs:
        raise ContractError(f"unknown workflow {workflow!r}")


def require_entitled(workflow: str, client_slug: str, scope: CallerScope) -> None:
    """Start gate — the RESOLVED run owner must be entitled, not merely some role.

    A caller holding `client.a` and `client.b`, entitled only via `a`, must not be
    able to start a run *owned by `b`*. The union check in `require_visible` cannot
    catch that, because it has no owner to check against.
    """
    if scope.unrestricted:
        return
    entitled = _table().get(workflow, frozenset())
    if not entitled & scope.client_slugs:
        # Not visible at all — stay indistinguishable from a nonexistent workflow.
        raise ContractError(f"unknown workflow {workflow!r}")
    if client_slug not in entitled:
        raise PermissionError(f"client {client_slug!r} is not entitled to workflow {workflow!r}")
