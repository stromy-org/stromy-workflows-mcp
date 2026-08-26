"""Refusing a client-funded run that cannot be funded, before anything exists.

Without this, starting a run with a missing key wrote a run row, prepared an ACA
job, pulled and booted a container, and only then died at stage `credentials` —
a slow, billable round trip to reach an answer the facade already had.

The property under test is therefore about ORDER, not about whether the failure
happens: the runner resolves every credential itself and fails closed, and that
check remains the authoritative one. So the tests assert that nothing was
created, not merely that an error was raised — a pre-flight that refuses AFTER
preparing the job would pass a naive assertion while buying nothing.
"""

from __future__ import annotations

from typing import Any

import pytest
from stromy_byok import InMemoryCredentialStore, NullCredentialStore, Subject, SubjectKind

from stromy_workflows_mcp import credentials, service
from stromy_workflows_mcp.contracts import (
    Contract,
    CredentialRequirements,
    _parse_properties,
)
from stromy_workflows_mcp.scoping import CallerScope

WORKFLOW = "stakeholder_analysis_workflow"
STROMY = CallerScope(frozenset({"stromy"}))
DUKE = CallerScope(frozenset({"dukestrategies"}))
SECRET = "sk-not-a-real-key"  # noqa: S105 - these tests are ABOUT credentials


class RecordingJobClient:
    """Records whether the expensive path was ever entered."""

    def __init__(self) -> None:
        self.prepared = False

    async def prepare(self, run_id: str) -> Any:  # pragma: no cover - guarded against
        self.prepared = True
        raise AssertionError("prepare() must not run when credentials are outstanding")


def _contract(*resolved: str, declared: bool = True) -> Contract:
    return Contract(
        workflow=WORKFLOW,
        schema={"workflow": WORKFLOW, "properties": {}},
        keys=_parse_properties({}),
        requirements=CredentialRequirements(
            declared=declared, resolved_credentials=tuple(resolved)
        ),
    )


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> InMemoryCredentialStore:
    store = InMemoryCredentialStore(credentials.CATALOGUE)
    monkeypatch.setattr(credentials, "credential_store", lambda: store)
    return store


def _register(vault: InMemoryCredentialStore, cid: str, slug: str = "stromy") -> None:
    vault.put_version(cid, Subject(SubjectKind.CLIENT_SLUG, slug), SECRET)


# --- the refusal --------------------------------------------------------------


def test_missing_credential_refuses_and_names_it(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    monkeypatch.setattr(
        service, "load_contract", lambda _n: _contract("deepseek-api", "openai-api")
    )
    _register(vault, "openai-api")
    with pytest.raises(service.CredentialsNotReady) as exc:
        service._preflight_credentials(WORKFLOW, "stromy")
    message = str(exc.value)
    assert "deepseek-api" in message
    # It must point at the remedy, not merely state the problem.
    assert "create_credential_registration_link" in message
    # And never leak which keys the OPERATOR holds (the C4 invariant).
    assert "openai-api" not in message.split("outstanding:")[0]


@pytest.mark.asyncio
async def test_start_run_refuses_before_preparing_anything(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    """The whole point: no run row, no ACA job, no container.

    `RecordingJobClient.prepare` raises if reached, so a pre-flight placed after
    it would fail this test with a different error than the one asserted.
    """
    monkeypatch.setattr(service, "load_contract", lambda _n: _contract("openai-api"))
    monkeypatch.setattr(service, "_validated", lambda *a, **k: ({}, {}))
    client = RecordingJobClient()
    with pytest.raises(service.CredentialsNotReady):
        await service.start_run(
            WORKFLOW, {}, {"client_slug": "stromy"}, None, STROMY, job_client=client
        )
    assert client.prepared is False


def test_an_undeclared_contract_cannot_be_run_on_client_funds(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    """Refused, not skipped.

    With no statement of what the workflow spends there is nothing to inject, so
    letting it through would run on whatever ambient operator keys the container
    happens to hold — the silent fall-through this plane exists to close.
    """
    monkeypatch.setattr(service, "load_contract", lambda _n: _contract(declared=False))
    with pytest.raises(service.CredentialsNotReady, match="declares no credential requirements"):
        service._preflight_credentials(WORKFLOW, "stromy")


def test_catalogue_drift_is_fatal_here_unlike_in_status(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    """Status REPORTS drift so an operator can see it; a run cannot proceed on
    it, because a credential nobody can register is one nobody can fund."""
    monkeypatch.setattr(service, "load_contract", lambda _n: _contract("ghost-api"))
    with pytest.raises(service.CredentialsNotReady, match="catalogue does not know"):
        service._preflight_credentials(WORKFLOW, "stromy")


# --- what it must NOT refuse --------------------------------------------------


def test_a_fully_registered_client_passes(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    monkeypatch.setattr(
        service, "load_contract", lambda _n: _contract("deepseek-api", "openai-api")
    )
    _register(vault, "openai-api")
    _register(vault, "deepseek-api")
    service._preflight_credentials(WORKFLOW, "stromy")  # must not raise


def test_operator_policy_is_never_pre_flighted(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    """`dukestrategies` ships on `operator` and registers nothing. Pre-flighting
    it would ground every operator-funded run in the estate."""
    monkeypatch.setattr(service, "load_contract", lambda _n: _contract("openai-api"))
    service._preflight_credentials(WORKFLOW, "dukestrategies")  # must not raise


def test_an_unreadable_store_lets_the_run_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail OPEN here, uniquely.

    `NullCredentialStore.exists` is False for everyone, so refusing on it would
    ground every client run the moment the vault became unreadable — a far worse
    outage than the wasted container this check exists to save. The runner still
    fails closed, so nothing is spent: the cost of being wrong here is one
    container, and the cost of being wrong the other way is the whole service.
    """
    monkeypatch.setattr(service, "load_contract", lambda _n: _contract("openai-api"))
    monkeypatch.setattr(credentials, "credential_store", lambda: NullCredentialStore())
    service._preflight_credentials(WORKFLOW, "stromy")  # must not raise


def test_the_preflight_is_not_the_enforcement_point() -> None:
    """Documented as an optimisation, in the code, on purpose.

    If a future reader believes this is the guarantee, the runner's own
    resolution becomes 'redundant' and gets deleted — at which point a key
    disabled between check and execution funds a run nobody authorised.
    """
    doc = service._preflight_credentials.__doc__ or ""
    assert "NOT A SECURITY CONTROL" in doc
    assert "TOCTOU" in doc
