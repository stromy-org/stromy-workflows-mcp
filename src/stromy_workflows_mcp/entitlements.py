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

import dataclasses
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import settings
from .contracts import PROJECT_ROOT, ContractError, list_contracts
from .scoping import _SLUG_RE, CallerScope

logger = logging.getLogger(__name__)


class EntitlementError(ValueError):
    pass


# --- Credential policy (ORG-PLAN-206 C4) -------------------------------------
#
# WHOSE KEYS DOES THIS CLIENT'S RUN SPEND? That is a commercial fact, so it lives
# here in the facade-owned entitlement registry and never in the generated
# contract, which describes technical requirements only. The same reasoning that
# put entitlement here rather than in contract JSON puts billing here too.
#
# `operator` — the run uses Stromy's own provider keys; Stromy carries the spend.
#   The default, and the value every pre-C4 entry migrates to, so introducing this
#   field changes no existing client's billing.
# `client` — the run resolves the client's OWN registered credentials at execution
#   start, with every ambient operator key scrubbed first. A client-mode run whose
#   credentials are unregistered fails at the `credentials` stage rather than
#   silently falling back to operator keys, which would bill Stromy for a run the
#   client believed they were paying for.
POLICY_OPERATOR = "operator"
POLICY_CLIENT = "client"
CREDENTIAL_POLICIES = frozenset({POLICY_OPERATOR, POLICY_CLIENT})
DEFAULT_CREDENTIAL_POLICY = POLICY_OPERATOR


# --- Per-credential funding (ORG-PLAN-300) -----------------------------------
#
# The policy above answers the question once for a whole run, which cannot express
# the shape Stromy actually sells: the client pays for the model tokens their own
# work consumes, while Stromy absorbs the flat-rate subscriptions (search,
# academic index, media archive) that do not meaningfully divide per client. The
# workaround was to leave those keys out of the credential plane entirely — and
# that is exactly how five of the stakeholder workflow's six evidence channels
# came to produce nothing on every hosted run, unregistered, unscrubbed and
# unreported.
#
# So funding is a decision PER CREDENTIAL, carried in the same per-slug object:
#
#     "stromy": {"funding": {"openai-api": "client", "serper-api": "operator"}}
#
# THE DEFAULT IS `operator`, AND THAT IS A DELIBERATE ASYMMETRY. An undecided
# credential must not block a workflow's first deployment on a pricing
# conversation — lending the operator's key is the fast, safe starting state.
# Silently billing a CLIENT for something they never agreed to is the failure
# that matters; silently billing ourselves is the lending we intended.
#
# But the default is never invisible. `decided` travels beside every answer, so
# a defaulted credential renders as "operator (defaulted)" rather than as a
# decision somebody made — the whole defect class here is funding nobody can see.
DEFAULT_CREDENTIAL_FUNDING = POLICY_OPERATOR


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


@dataclass(frozen=True)
class FundingDecision:
    """How one `(workflow, client)` pair funds its credentials.

    Three shapes collapse into one type so every caller resolves through the
    same code path:

    * ``funding`` — an explicit per-credential map. The authored shape.
    * ``shorthand`` — a single ``credential_policy`` covering every declared
      credential. Retained because the registry and this code deploy
      independently, and a file the parser refuses denies every client at once.
    * neither — nothing was decided, so everything defaults to ``operator``
      and says so.

    :meth:`resolve` is the ONLY way to read it, because the answer depends on
    the contract's declared set: a decision is meaningless until you know which
    credentials the workflow actually spends.
    """

    funding: Mapping[str, str] = dataclasses.field(default_factory=dict)
    shorthand: str | None = None

    def resolve(self, declared: Iterable[str]) -> dict[str, FundingEntry]:
        """The effective funder of every declared credential, and whether anyone chose it."""
        answer: dict[str, FundingEntry] = {}
        for credential_id in declared:
            if credential_id in self.funding:
                answer[credential_id] = FundingEntry(self.funding[credential_id], True)
            elif self.shorthand is not None:
                answer[credential_id] = FundingEntry(self.shorthand, True)
            else:
                answer[credential_id] = FundingEntry(DEFAULT_CREDENTIAL_FUNDING, False)
        return answer

    def intends_client_funding(self) -> bool:
        """Does this pair mean to bill the client for anything at all?

        Answerable WITHOUT a declared set, which is the point: a contract that
        declares nothing resolves to "nothing is client-funded" for every pair,
        so a caller that needs to refuse an undeclared contract has to ask the
        intent directly or it will wave the dangerous case through.
        """
        return self.shorthand == POLICY_CLIENT or POLICY_CLIENT in self.funding.values()

    def undeclared_keys(self, declared: Iterable[str]) -> tuple[str, ...]:
        """Funding keys naming a credential this workflow does not spend.

        Always a mistake, and a quiet one: a decision written against the wrong
        workflow looks exactly like a decision that took effect.
        """
        known = set(declared)
        return tuple(sorted(k for k in self.funding if k not in known))


@dataclass(frozen=True)
class FundingEntry:
    """Who funds one credential, and whether that was decided or defaulted."""

    funded_by: str
    decided: bool


def entitlements_path() -> Path:
    return (PROJECT_ROOT / settings.entitlements_file).resolve()


def _parse_clients(name: str, clients: Any) -> dict[str, FundingDecision]:
    """Read one entry's clients in either registry shape.

    v1 is a LIST of slugs; v2 is an OBJECT keyed by slug whose value carries
    `credential_policy` or, since ORG-PLAN-300, a per-credential `funding` map.
    All three are accepted on purpose. The registry is authored in
    this repo, so a hard cutover would be *possible* — but the deployed artifact and
    the code roll out independently, and a v1 file meeting a v2-only parser would
    fail closed and deny every client at once. Reading v1 as "every slug on the
    default policy" makes that window a no-op instead of an outage, and is exactly
    the migration default the plan specifies.
    """
    if isinstance(clients, list):
        return {_client_slug(name, slug): FundingDecision() for slug in clients}
    if not isinstance(clients, dict):
        raise EntitlementError(f"entitlement entry {name!r} has a non-list/object 'clients'")

    table: dict[str, FundingDecision] = {}
    for slug, config in clients.items():
        key = _client_slug(name, slug)
        if config is None:
            table[key] = FundingDecision()
            continue
        if not isinstance(config, dict):
            raise EntitlementError(
                f"entitlement entry {name!r} client {slug!r} must be an object or null"
            )
        table[key] = _parse_funding(name, slug, config)
    return table


def _parse_funding(name: str, slug: Any, config: dict[str, Any]) -> FundingDecision:
    """One client's funding decision, in either the scalar or the per-credential shape.

    Both are accepted, never both at once. A file carrying `credential_policy`
    AND `funding` for one slug states the answer twice, and the two can disagree
    — so it is refused rather than resolved by precedence, which would make the
    billing answer depend on which field a reader happened to consult.
    """
    shorthand = config.get("credential_policy")
    raw = config.get("funding")

    if shorthand is not None and raw is not None:
        raise EntitlementError(
            f"entitlement entry {name!r} client {slug!r} declares BOTH "
            "credential_policy and funding. Keep one: the scalar means 'every "
            "declared credential on this policy', the map decides per credential."
        )

    if shorthand is not None:
        # An unrecognised policy is a hard error, never a silent fall back to
        # `operator`: a typo'd "cient" would otherwise bill Stromy for runs the
        # registry was edited specifically to bill the client for.
        if shorthand not in CREDENTIAL_POLICIES:
            raise EntitlementError(
                f"entitlement entry {name!r} client {slug!r} has unknown "
                f"credential_policy {shorthand!r} (expected one of "
                f"{', '.join(sorted(CREDENTIAL_POLICIES))})"
            )
        return FundingDecision(shorthand=shorthand)

    if raw is None:
        return FundingDecision()
    if not isinstance(raw, dict):
        raise EntitlementError(
            f"entitlement entry {name!r} client {slug!r} has a non-object 'funding'"
        )

    funding: dict[str, str] = {}
    for credential_id, value in raw.items():
        if not isinstance(credential_id, str) or not credential_id:
            raise EntitlementError(
                f"entitlement entry {name!r} client {slug!r} has an invalid "
                f"funding key {credential_id!r}"
            )
        if value not in CREDENTIAL_POLICIES:
            raise EntitlementError(
                f"entitlement entry {name!r} client {slug!r} funds "
                f"{credential_id!r} as {value!r} (expected one of "
                f"{', '.join(sorted(CREDENTIAL_POLICIES))})"
            )
        funding[credential_id] = value
    return FundingDecision(funding=funding)


def _client_slug(name: str, slug: Any) -> str:
    # Same slug grammar as a verified `client.<slug>` role, so a typo here
    # can never resolve against a role shape that scoping.py would reject.
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise EntitlementError(f"entitlement entry {name!r} has invalid slug {slug!r}")
    return slug


def _parse(raw: Any) -> dict[str, dict[str, FundingDecision]]:
    workflows = raw.get("workflows") if isinstance(raw, dict) else None
    if not isinstance(workflows, dict):
        raise EntitlementError("entitlements registry has no 'workflows' object")
    table: dict[str, dict[str, FundingDecision]] = {}
    for name, entry in workflows.items():
        if not isinstance(entry, dict):
            raise EntitlementError(f"entitlement entry {name!r} must be an object")
        table[name] = _parse_clients(name, entry.get("clients", []))
    return table


def load_entitlements() -> dict[str, dict[str, FundingDecision]]:
    """Parse the registry, raising on any problem. Callers decide the failure posture."""
    path = entitlements_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EntitlementError(f"cannot load entitlements from {path}: {exc}") from exc
    return _parse(raw)


def _table() -> dict[str, dict[str, FundingDecision]]:
    """Fail closed: an unreadable registry denies every client, never allows one."""
    try:
        return load_entitlements()
    except EntitlementError as exc:
        logger.error("workflow entitlements unreadable; denying all client access: %s", exc)
        return {}


def entitled_clients(workflow: str) -> frozenset[str]:
    """Client slugs granted this workflow. Empty means operator-only."""
    return frozenset(_table().get(workflow, {}))


def funding_decision(workflow: str, client_slug: str) -> FundingDecision:
    """This pair's raw funding decision, before it meets a declared credential set.

    Defaults to "nothing decided" for an unknown pairing rather than raising:
    entitlement is decided by the `require_*` gates above, and duplicating that
    decision here would make the billing answer depend on which of two checks
    ran first. An unentitled caller never reaches a funding question at all.
    """
    return _table().get(workflow, {}).get(client_slug) or FundingDecision()


def credential_funding(
    workflow: str, client_slug: str, declared: Iterable[str]
) -> dict[str, FundingEntry]:
    """Who funds each declared credential for this pair, and whether it was decided."""
    return funding_decision(workflow, client_slug).resolve(declared)


def credential_policy(workflow: str, client_slug: str, declared: Sequence[str] = ()) -> str:
    """The COARSE answer: does any part of this run spend the client's keys?

    Retained for the surfaces that ask a yes/no question — does this run need a
    scrub, does the client owe a registration. `client` whenever ANY declared
    credential is client-funded, because a run with one client-funded credential
    is a client-funded run as far as those surfaces are concerned.

    With no declared set it can only report the shorthand, which is the honest
    answer for a caller that has not told us what the workflow spends.
    """
    decision = funding_decision(workflow, client_slug)
    if declared:
        resolved = decision.resolve(declared)
        if any(entry.funded_by == POLICY_CLIENT for entry in resolved.values()):
            return POLICY_CLIENT
        return POLICY_OPERATOR
    return decision.shorthand or DEFAULT_CREDENTIAL_POLICY


def visible_workflows(scope: CallerScope) -> list[str]:
    if scope.unrestricted:
        return list_contracts()
    table = _table()
    return [
        name for name in list_contracts() if frozenset(table.get(name, {})) & scope.client_slugs
    ]


def require_visible(workflow: str, scope: CallerScope) -> None:
    """Discovery gate — any of the caller's roles entitles them (union)."""
    if scope.unrestricted:
        return
    if not frozenset(_table().get(workflow, {})) & scope.client_slugs:
        raise ContractError(f"unknown workflow {workflow!r}")


def require_entitled(workflow: str, client_slug: str, scope: CallerScope) -> None:
    """Start gate — the RESOLVED run owner must be entitled, not merely some role.

    A caller holding `client.a` and `client.b`, entitled only via `a`, must not be
    able to start a run *owned by `b`*. The union check in `require_visible` cannot
    catch that, because it has no owner to check against.
    """
    if scope.unrestricted:
        return
    entitled = frozenset(_table().get(workflow, {}))
    if not entitled & scope.client_slugs:
        # Not visible at all — stay indistinguishable from a nonexistent workflow.
        raise ContractError(f"unknown workflow {workflow!r}")
    if client_slug not in entitled:
        raise PermissionError(f"client {client_slug!r} is not entitled to workflow {workflow!r}")
