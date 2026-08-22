"""What the facade pins to a run at creation, and what it refuses to pin.

The snapshot is the answer to "who pays for this run", written once and never
recomputed. Everything here is about that word *once*: the runner reads this
instead of re-deriving, precisely so an entitlement or a contract edited between
a failure and its retry cannot silently move the bill.

Nothing in the snapshot is a secret — credential *identifiers*, a policy string
and a routing digest. What matters is that none of it is reachable from caller
config, which is asserted directly rather than assumed from where the code sits.
"""

from __future__ import annotations

import pytest

from stromy_workflows_mcp import registry, service
from stromy_workflows_mcp.contracts import load_contract

STAKEHOLDER = "stakeholder_analysis_workflow"
WEEKLY = "weekly_intel_workflow"
CLIENT = "dukestrategies"


def _snapshot(workflow: str = STAKEHOLDER, client: str = CLIENT) -> dict:
    snapshot = service._execution_snapshot(workflow, client)
    assert snapshot is not None
    return snapshot


# --- what gets pinned ---------------------------------------------------------


def test_the_snapshot_carries_the_policy_the_entitlement_table_says() -> None:
    from stromy_workflows_mcp.entitlements import credential_policy

    assert _snapshot()["credential_policy"] == credential_policy(STAKEHOLDER, CLIENT)


@pytest.mark.parametrize(
    ("workflow", "expected"),
    [(STAKEHOLDER, ["deepseek-api", "openai-api"]), (WEEKLY, ["openai-api"])],
)
def test_the_snapshot_carries_the_contracts_resolved_credentials(
    workflow: str, expected: list[str]
) -> None:
    assert _snapshot(workflow)["resolved_credentials"] == expected


def test_the_snapshot_carries_the_semantic_requirements_too() -> None:
    """Not only the concrete answer. The runner re-derives the list against ITS
    own model registry and compares — a snapshot holding just the conclusion
    would be the claim and the evidence at once, and nothing could check it."""
    snapshot = _snapshot()
    assert snapshot["models"]
    assert {model["tier"] for model in snapshot["models"]} == {"cheap", "medium", "smart"}
    assert snapshot["model_registry_digest"].startswith("sha256:")


def test_the_snapshot_holds_no_secret_shaped_material() -> None:
    """Identifiers and labels only. Never a value, never an env alias — the
    alias names are how a scrub list is built, and they belong to the runner's
    catalogue rather than to a row a client can read the public projection of.
    """
    rendered = str(_snapshot())
    for alias in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "APIFY_TOKEN", "sk-"):
        assert alias not in rendered


# --- what does NOT get pinned -------------------------------------------------


def test_an_undeclared_contract_pins_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None``, not an empty snapshot.

    "Nobody has authored requirements yet" and "this workflow needs nothing" are
    opposite policies that look identical once flattened, and the runner reads an
    absent snapshot as "fund it the way runs have always been funded". Writing an
    empty one would turn an unauthored contract into a positive declaration.
    """
    contract = load_contract(STAKEHOLDER)
    stripped = type(contract)(
        workflow=contract.workflow,
        schema=contract.schema,
        keys=contract.keys,
    )
    monkeypatch.setattr(service, "load_contract", lambda _name: stripped)
    assert service._execution_snapshot(STAKEHOLDER, CLIENT) is None


def test_the_snapshot_ignores_caller_config_entirely() -> None:
    """It takes a workflow name and a resolved owner — there is no parameter a
    caller's config could reach. Asserted on the signature so a future refactor
    that threaded config in has to come back through this test."""
    import inspect

    parameters = set(inspect.signature(service._execution_snapshot).parameters)
    assert parameters == {"workflow", "client_slug"}


# --- the version floor --------------------------------------------------------


def test_the_pin_is_gated_on_the_execution_metadata_schema() -> None:
    """A facade deployed ahead of the migration must skip the pin, not fail the
    run. Runs created in that window behave exactly as they did before this
    feature existed."""
    assert registry.EXECUTION_METADATA_SCHEMA == 4
    assert registry.SUPPORTED_SCHEMA_MIN < registry.EXECUTION_METADATA_SCHEMA
