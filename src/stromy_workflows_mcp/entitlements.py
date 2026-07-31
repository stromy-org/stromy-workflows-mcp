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


# --- Executable data-plane adapters (ORG-PLAN-164 WS0) -----------------------
#
# Mirror of Stromy's `stromy/runtime/adapters.py` registry. It is a mirror and
# not an import because the runner lives in a private repo this public CI cannot
# reach — the same reason `sync_contracts.py --check` never executes here. The
# contract JSONs travel with the adapter *names* baked in, so an unknown name is
# still catchable from this side alone; `sync_contracts.py --check` asserts this
# mirror matches the live registry wherever both checkouts are present.
#
# The two sentinels declare "this workflow has no client-facing data plane".
# They are legitimate on an operator-only workflow and refused on an entitled
# one — which is the whole "entitled but unusable" failure this gate exists to
# stop: a required input with no upload path, or deliverables that never leave
# ephemeral container disk.
KNOWN_INPUT_ADAPTERS = frozenset({"none", "inputset"})
KNOWN_ARTIFACT_ADAPTERS = frozenset({"operator", "stakeholder_exports"})
OPERATOR_ONLY_INPUT_ADAPTERS = frozenset({"none"})
OPERATOR_ONLY_ARTIFACT_ADAPTERS = frozenset({"operator"})


def adapter_problems(workflow: str, schema: dict[str, Any], *, has_clients: bool) -> list[str]:
    """Return every adapter-declaration problem for one workflow.

    Returns a list rather than raising so the CI gate can report all problems in
    one run instead of one per push.
    """
    problems: list[str] = []
    for field, known, operator_only in (
        ("x-input-adapter", KNOWN_INPUT_ADAPTERS, OPERATOR_ONLY_INPUT_ADAPTERS),
        ("x-artifact-adapter", KNOWN_ARTIFACT_ADAPTERS, OPERATOR_ONLY_ARTIFACT_ADAPTERS),
    ):
        declared = schema.get(field)
        if not isinstance(declared, str) or not declared:
            problems.append(
                f"contract {workflow!r} declares no {field}. Every hosted workflow "
                'must name its data plane — use "none"/"operator" for an '
                "operator-only workflow."
            )
            continue
        if declared not in known:
            problems.append(
                f"contract {workflow!r} names unknown {field} {declared!r}; "
                f"registered: {', '.join(sorted(known))}"
            )
            continue
        if has_clients and declared in operator_only:
            problems.append(
                f"workflow {workflow!r} is entitled to a client but declares "
                f'{field}: {declared!r}, which means "operator-only". A client '
                "entitlement needs a real adapter — otherwise the workflow is "
                "entitled but unusable."
            )
    return problems


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
