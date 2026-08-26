"""Registration status and capability readiness (ORG-PLAN-206 C4 items 5, 6).

The status surface is the one place a value could leak by accident, so the tests
assert what it must NEVER contain as hard as what it must report. The other
property under test is the distinction the surface exists to preserve:
"unprovisioned" and "not registered" are the same boolean and opposite facts.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient
from stromy_byok import (
    InMemoryCredentialStore,
    NullCredentialStore,
    Subject,
    SubjectKind,
)

from stromy_workflows_mcp import credentials, readiness, server, service
from stromy_workflows_mcp.contracts import (
    Contract,
    CredentialRequirements,
    _parse_properties,
)
from stromy_workflows_mcp.scoping import CallerScope

WORKFLOW = "stakeholder_analysis_workflow"
STROMY = CallerScope(frozenset({"stromy"}))
OPERATOR = CallerScope(frozenset(), unrestricted=True)
# A deliberately distinctive fake, so a leak is greppable in the serialized
# payload rather than needing to be reasoned about. S105 is exactly right in
# general and exactly wrong here — these tests are ABOUT credential handling.
SECRET = "sk-do-not-leak-this-value"  # noqa: S105


@pytest.fixture
def declared(monkeypatch: pytest.MonkeyPatch) -> None:
    contract = Contract(
        workflow=WORKFLOW,
        schema={"workflow": WORKFLOW, "properties": {}},
        keys=_parse_properties({}),
        requirements=CredentialRequirements(
            declared=True,
            credentials=("apify-api",),
            resolved_credentials=("openai-api",),
        ),
    )
    monkeypatch.setattr(service, "load_contract", lambda name: contract)


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> InMemoryCredentialStore:
    store = InMemoryCredentialStore(credentials.CATALOGUE)
    monkeypatch.setattr(credentials, "credential_store", lambda: store)
    return store


def _status(scope: CallerScope = STROMY, slug: str = "stromy") -> dict[str, Any]:
    return service.credential_status(WORKFLOW, {"client_slug": slug}, scope)


# --- What it reports ---------------------------------------------------------


def test_reports_each_declared_credential_and_the_billing_policy(
    declared: None, vault: InMemoryCredentialStore
) -> None:
    status = _status()
    assert status["client_slug"] == "stromy"
    assert status["declared"] is True
    assert status["store_provisioned"] is True
    # `stromy` is the self-client and ships on `client` (ORG-PLAN-206 C6), which
    # is what makes this the interesting direction to assert: on `client` an
    # unregistered credential IS an outstanding action, and the tool's contract
    # is that a reader must consult the policy before saying so. The operator
    # reading — "not_registered means the client owes nothing" — is covered by
    # test_operator_policy_reports_no_outstanding_action below.
    assert status["credential_policy"] == "client"
    assert [entry["credential_id"] for entry in status["credentials"]] == [
        "apify-api",
        "openai-api",
    ]
    assert all(entry["status"] == "not_registered" for entry in status["credentials"])
    assert all(entry["signup_url"] for entry in status["credentials"])


def test_operator_policy_reports_no_outstanding_action(
    declared: None, vault: InMemoryCredentialStore
) -> None:
    """The other half of the reading, on a slug that still ships `operator`.

    `not_registered` means opposite things under the two policies, and once the
    self-client moved to `client` nothing else asserted the operator side against
    the SHIPPED registry. Reading `dukestrategies` keeps both branches covered by
    the real file rather than by a fixture that could drift away from it.
    """
    duke = CallerScope(frozenset({"dukestrategies"}))
    status = _status(duke, "dukestrategies")
    assert status["credential_policy"] == "operator"
    # Same literal status as the client-policy case above — which is precisely
    # why a caller must read the policy to know whether it is an action item.
    assert all(entry["status"] == "not_registered" for entry in status["credentials"])


def test_a_registered_key_reads_as_registered_without_returning_it(
    declared: None, vault: InMemoryCredentialStore
) -> None:
    subject = Subject(SubjectKind.CLIENT_SLUG, "stromy")
    vault.put_version("openai-api", subject, SECRET)

    status = _status()
    entry = next(e for e in status["credentials"] if e["credential_id"] == "openai-api")
    assert entry["status"] == "registered"
    assert entry["last_rotated_at"]
    # The whole surface, serialized, must not contain the value anywhere.
    assert SECRET not in json.dumps(status)


def test_a_disconnected_key_reads_as_not_registered(
    declared: None, vault: InMemoryCredentialStore
) -> None:
    subject = Subject(SubjectKind.CLIENT_SLUG, "stromy")
    vault.put_version("openai-api", subject, SECRET)
    vault.disable("openai-api", subject)

    entry = next(e for e in _status()["credentials"] if e["credential_id"] == "openai-api")
    assert entry["status"] == "not_registered"


def test_one_client_never_sees_another_clients_registration(
    declared: None, vault: InMemoryCredentialStore
) -> None:
    vault.put_version("openai-api", Subject(SubjectKind.CLIENT_SLUG, "dukestrategies"), SECRET)

    entry = next(e for e in _status()["credentials"] if e["credential_id"] == "openai-api")
    assert entry["status"] == "not_registered"


# --- Unprovisioned is NOT unregistered ---------------------------------------


def test_an_unprovisioned_store_reports_unavailable_not_unregistered(
    declared: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NullCredentialStore.exists` answers False for everyone.

    That is the same boolean as a truthful "not registered" and the opposite
    fact, so rendering it as such would tell a client who HAS connected a key
    that they have not.
    """
    monkeypatch.setattr(credentials, "credential_store", lambda: NullCredentialStore())
    status = _status()
    assert status["store_provisioned"] is False
    assert all(entry["status"] == "unavailable" for entry in status["credentials"])
    assert "not the same as" in status["note"]


# --- Gates, undeclared, and drift -------------------------------------------


def test_status_applies_the_same_gates_as_minting(declared: None) -> None:
    with pytest.raises(PermissionError, match="not authorized for client"):
        _status(STROMY, "dukestrategies")
    with pytest.raises(PermissionError, match="client_slug is required"):
        service.credential_status(WORKFLOW, {}, OPERATOR)


def test_undeclared_requirements_report_nothing_to_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "load_contract",
        lambda name: Contract(
            workflow=WORKFLOW,
            schema={"workflow": WORKFLOW, "properties": {}},
            keys=_parse_properties({}),
            requirements=CredentialRequirements(),
        ),
    )
    status = _status()
    assert status["declared"] is False
    assert status["credentials"] == []


def test_catalogue_drift_is_reported_rather_than_raised(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    """A contract naming an id this catalogue lacks must not blank the whole call.

    Drift is exactly what C5's manifest check exists to catch; a status call that
    dies on it hides every other credential's real state at the same time.
    """
    monkeypatch.setattr(
        service,
        "load_contract",
        lambda name: Contract(
            workflow=WORKFLOW,
            schema={"workflow": WORKFLOW, "properties": {}},
            keys=_parse_properties({}),
            requirements=CredentialRequirements(
                declared=True, resolved_credentials=("openai-api", "ghost-api")
            ),
        ),
    )
    status = _status()
    assert status["catalogue_drift"] == ["ghost-api"]
    assert [entry["credential_id"] for entry in status["credentials"]] == ["openai-api"]


def test_the_status_tool_is_exposed() -> None:
    import asyncio

    from fastmcp import Client

    async def names() -> set[str]:
        async with Client(server.mcp) as client:
            return {tool.name for tool in await client.list_tools()}

    assert "get_credential_status" in asyncio.run(names())


# --- Readiness (item 6) ------------------------------------------------------


def test_registration_capability_is_declared() -> None:
    spec = readiness.CAPABILITIES["client-key-registration"]
    assert spec.requires == ("BYOK_KEY_VAULT_URL",)
    assert spec.owner == readiness.OWNER_OPERATOR
    assert spec.client_facing is True


def test_every_capability_env_read_by_the_code_is_declared() -> None:
    """The declaration must cover what the code actually reads.

    Both of these are read with no safe default, so an absent one switches a
    client-facing capability off silently. That is the drift the org's
    readiness-coverage auditor gates on; asserting it here means this repo does
    not depend on the shared CI job it cannot call.
    """
    declared_env = {name for spec in readiness.CAPABILITIES.values() for name in spec.requires}
    assert {"BYOK_KEY_VAULT_URL", "WORKFLOW_STORAGE_ACCOUNT"} <= declared_env


def test_absent_vault_reports_degraded_and_says_what_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BYOK_KEY_VAULT_URL", raising=False)
    monkeypatch.setenv("WORKFLOW_STORAGE_ACCOUNT", "ststromyworkflows")
    report = readiness.readiness_report()
    assert report["overall"] == "degraded"
    entry = next(
        item
        for item in report["capabilities"]  # type: ignore[union-attr]
        if item["capability"] == "client-key-registration"
    )
    assert entry["status"] == "degraded"
    assert entry["missing_env"] == ["BYOK_KEY_VAULT_URL"]
    assert "attribution" in entry["degraded"]


def test_present_but_empty_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty string is what a half-finished deploy leaves behind.

    A presence-only check calls it provisioned while every call using it fails.
    """
    monkeypatch.setenv("BYOK_KEY_VAULT_URL", "   ")
    assert readiness.missing_vars(readiness.CAPABILITIES["client-key-registration"]) == [
        "BYOK_KEY_VAULT_URL"
    ]


def test_ready_when_both_capabilities_are_provisioned(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYOK_KEY_VAULT_URL", "https://kv-stromy-wf-byok.vault.azure.net/")
    monkeypatch.setenv("WORKFLOW_STORAGE_ACCOUNT", "ststromyworkflows")
    monkeypatch.setenv("WORKFLOW_MAX_REPLICAS", "1")
    assert readiness.readiness_report()["overall"] == "ready"


def test_a_scaled_out_deployment_is_reported_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two replicas plus an in-memory grant store means links fail at random.

    That presents as flaky links rather than as a misconfiguration, so it has to
    be readable here rather than discovered by a client whose link "expired".
    """
    monkeypatch.setenv("BYOK_KEY_VAULT_URL", "https://kv-stromy-wf-byok.vault.azure.net/")
    monkeypatch.setenv("WORKFLOW_STORAGE_ACCOUNT", "ststromyworkflows")
    monkeypatch.setenv("WORKFLOW_MAX_REPLICAS", "2")
    report = readiness.readiness_report()
    assert report["overall"] == "degraded"
    assert "unresolvable on another" in report["grant_store"]


def test_readiness_never_raises_and_never_echoes_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BYOK_KEY_VAULT_URL", SECRET)
    monkeypatch.setenv("WORKFLOW_MAX_REPLICAS", "not-a-number")
    report = readiness.readiness_report()
    assert SECRET not in json.dumps(report)


def test_health_carries_the_readiness_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """The endpoint an operator already curls is where this has to show up."""
    monkeypatch.setattr(
        "stromy_workflows_mcp.config.settings.stromy_pg_dsn", "postgresql://x", raising=False
    )
    monkeypatch.setattr(
        "stromy_workflows_mcp.registry.schema_version", lambda conn: 2, raising=False
    )
    monkeypatch.setattr(
        "stromy_workflows_mcp.migrations.require_applied", lambda conn: 1, raising=False
    )

    import contextlib

    @contextlib.contextmanager
    def fake_connect():  # type: ignore[no-untyped-def]
        yield object()

    monkeypatch.setattr("stromy_workflows_mcp.registry.connect", fake_connect, raising=False)
    with TestClient(server.mcp.http_app()) as client:
        payload = client.get("/health").json()
    assert payload["status"] == "ok"
    assert payload["readiness"]["server"] == "stromy-workflows-mcp"
    assert {"client-key-registration", "client-document-uploads"} == {
        entry["capability"] for entry in payload["readiness"]["capabilities"]
    }
