"""Capability readiness for stromy-workflows-mcp (ORG-PLAN-172, ORG-PLAN-206 C4).

WHY THIS EXISTS
---------------
A hosted MCP can lose a whole capability to one unset environment variable and
say nothing anyone reads. This facade has two such surfaces, and both fail in the
quiet direction:

* **client-key-registration** — with `BYOK_KEY_VAULT_URL` absent the credential
  store is a `NullCredentialStore`. Registration links still mint, the page still
  renders, and the POST answers 503 *after* the person has pasted their key.
  Every caller reads as unregistered. Nothing about that is visible from the tool
  surface.
* **client-document-uploads** — with `WORKFLOW_STORAGE_ACCOUNT` absent no SAS can
  be minted, so every input session fails at its first write. This one is not
  hypothetical: the variable was genuinely missing from the live app until
  2026-08-14, which is what motivated declaring it here.

TWO INVARIANTS THIS MODULE MUST NEVER BREAK
-------------------------------------------
1. **Never raise.** A capability whose env is absent reports ``degraded``.
   Raising would convert honest degradation into an outage.
2. **Never echo a value.** Only variable *names* and statuses leave this module.

KEEP THE DECLARATION LITERAL — AN AUDITOR PARSES IT WITHOUT IMPORTING IT
------------------------------------------------------------------------
`stromy-org/scripts/audit-mcp-capability-readiness.py` compares this declaration
against the live Azure Container App env by parsing this file's AST. Every value
inside ``CAPABILITIES`` must stay a plain literal; the ``OWNER_*`` constants are
the one exception the auditor resolves by name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# The two owner classes have OPPOSITE fixes: an absent `operator` var is Stromy's
# provisioning bug, while an absent `caller-byok` var is the correct server state.
OWNER_OPERATOR = "operator"
OWNER_CALLER_BYOK = "caller-byok"


@dataclass(frozen=True)
class CapabilitySpec:
    """One optional capability and the environment it needs to be switched on.

    ``requires`` is ALL-OF; ``requires_any`` is a tuple of ANY-OF groups.
    """

    degraded: str
    requires: tuple[str, ...] = ()
    requires_any: tuple[tuple[str, ...], ...] = ()
    owner: str = OWNER_OPERATOR
    client_facing: bool = True
    notes: str = ""
    external_precondition: str = ""


# Credential-shaped env this server reads that is deliberately NOT an optional
# capability. Every entry needs a REASON in words — `audit-readiness-coverage.py`
# fails a pull request on a credential-shaped read that neither a capability nor
# this dict accounts for.
NON_CAPABILITY_ENV: dict[str, str] = {
    "OAUTH_CLIENT_SECRET": (
        "OAuth transport plumbing (config_base SettingsBase), not an optional "
        "capability: without it the server has no authenticated surface at all, "
        "which is an outage rather than a degradation."
    ),
    "STROMY_PG_DSN": (
        "The run registry itself. Absent, /health answers 503 and every tool "
        "fails — an outage, not a degraded capability. Declared here so the "
        "coverage auditor sees a reason rather than an omission."
    ),
}


CAPABILITIES: dict[str, CapabilitySpec] = {
    "client-key-registration": CapabilitySpec(
        requires=("BYOK_KEY_VAULT_URL",),
        owner=OWNER_OPERATOR,
        client_facing=True,
        degraded=(
            "The per-client credential store is unprovisioned "
            "(NullCredentialStore), so no client can register a key: the /keys "
            "page answers 503 on submit — AFTER the person has pasted their "
            "key, which is the worst place to discover it. Runs still execute "
            "on Stromy's own provider keys, so nothing breaks visibly; what is "
            "lost is client spend attribution, silently. Present-but-EMPTY "
            "counts as absent."
        ),
        notes=(
            "This URL is the CHANNEL and is Stromy's infra to provision (owner "
            "`operator`): kv-stromy-wf-byok, delivered as ACA env because the "
            "Terraform module ignores imported container env and the deploy "
            "workflow only sets --image. The provider keys INSIDE the vault are "
            "owner-class `caller-byok` — one secret per client, registered "
            "through create_credential_registration_link + /keys, never present "
            "in env and never a tool argument. Their absence from the app env "
            "is by design and is not a defect. The facade holds Key Vault "
            "Secrets OFFICER (it writes registrations); the runner holds "
            "Secrets User (it only reads)."
        ),
    ),
    "client-document-uploads": CapabilitySpec(
        requires=("WORKFLOW_STORAGE_ACCOUNT",),
        owner=OWNER_OPERATOR,
        client_facing=True,
        degraded=(
            "No user-delegation SAS can be minted, so create_input_session "
            "fails and any workflow whose contract declares the `inputset` "
            "input adapter cannot be run at all — the client has no way to "
            "supply their documents."
        ),
        notes=(
            "ststromyworkflows. Unlike the other WORKFLOW_* vars this one has "
            "NO safe default — a storage account name cannot be guessed — and "
            "it was genuinely unset on the live app until 2026-08-14. Signed "
            "with an Entra key via the managed identity, never an account key."
        ),
    ),
}


def _present(name: str) -> bool:
    """True only if the variable is set AND non-blank.

    Present-but-empty counts as ABSENT. An empty string is the shape a
    half-finished deploy leaves behind, and a presence-only check reports it
    fine while every call that uses it fails.
    """
    return bool(os.environ.get(name, "").strip())


def missing_vars(spec: CapabilitySpec) -> list[str]:
    """Names that must be provisioned before ``spec`` is ready."""
    missing = [name for name in spec.requires if not _present(name)]
    for group in spec.requires_any:
        if not any(_present(name) for name in group):
            missing.extend(group)
    return missing


def capability_status(name: str, spec: CapabilitySpec) -> dict[str, object]:
    """Status for one capability. Names and statuses only — never a value."""
    missing = missing_vars(spec)
    ready = not missing
    status: dict[str, object] = {
        "capability": name,
        "status": "ready" if ready else "degraded",
        "owner": spec.owner,
        "client_facing": spec.client_facing,
        "missing_env": missing,
        "requires": list(spec.requires),
        "requires_any": [list(group) for group in spec.requires_any],
    }
    if not ready:
        status["degraded"] = spec.degraded
        status["fix_owner"] = (
            "Stromy operator provisions this (ACA env / terraform)"
            if spec.owner == OWNER_OPERATOR
            else "the calling client registers their own key; absence from "
            "server env is the CORRECT state and is not a defect"
        )
    if spec.notes:
        status["notes"] = spec.notes
    if spec.external_precondition:
        status["external_precondition"] = spec.external_precondition
        if ready:
            status["status"] = "ready-blocked"
    return status


def grant_store_problem() -> str:
    """Why the registration grant store would not survive this deployment, if so.

    The in-memory grant store is correct only at one replica: a link minted on
    one process is unresolvable on another, which presents as *intermittently
    invalid links* rather than as a misconfiguration. `stromy_byok` asserts this
    rather than documenting it; this surfaces the same assertion in the report so
    a scale-out is caught by reading, not by a client's broken link.

    Never raises — a readiness report that can fail is not a readiness report.
    """
    try:
        from stromy_byok import GrantError, assert_single_replica_or_durable

        from . import credentials

        replicas = int(os.environ.get("WORKFLOW_MAX_REPLICAS", "1").strip() or "1")
        try:
            assert_single_replica_or_durable(credentials.GRANTS, replicas)
        except GrantError as exc:
            return str(exc)
    except Exception:  # noqa: BLE001 - readiness must never raise
        return ""
    return ""


def readiness_report() -> dict[str, object]:
    """Which optional capabilities are switched on right now, and what is lost.

    Never raises and never echoes a secret value.
    """
    capabilities = [capability_status(name, spec) for name, spec in sorted(CAPABILITIES.items())]
    degraded = [entry for entry in capabilities if entry["status"] == "degraded"]
    blocked = [entry for entry in capabilities if entry["status"] == "ready-blocked"]
    if degraded:
        overall = "degraded"
    elif blocked:
        overall = "ready-blocked"
    else:
        overall = "ready"
    report: dict[str, object] = {
        "server": "stromy-workflows-mcp",
        "overall": overall,
        "degraded_count": len(degraded),
        "blocked_count": len(blocked),
        "capabilities": capabilities,
        "reading": (
            "`degraded` means the capability is switched off and says so here "
            "rather than at the point of use. A `caller-byok` variable missing "
            "from server env is the correct state, never a defect."
        ),
    }
    problem = grant_store_problem()
    if problem:
        report["grant_store"] = problem
        report["overall"] = "degraded"
    return report
