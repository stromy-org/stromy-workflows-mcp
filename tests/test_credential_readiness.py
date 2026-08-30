"""Can this client actually start a client-funded run, and do we say so?

A run resolves EVERY credential a workflow requires before the graph is built
and fails on the first missing one. Per-credential statuses therefore do not
compose the way they look like they should: two `registered` and one
`not_registered` is not "two-thirds ready", it is "cannot run". Until 2026-08-26
the surfaces left that composition to the reader, and a competent reader got it
wrong on the first real run — registering the one credential they recognised and
meeting a failure at stage `credentials` that nothing had predicted.

These tests pin the answer being stated rather than inferable, and pin the two
places it must NOT be stated as a plain boolean: operator policy (nothing is
owed) and an unreadable store (nothing is known).
"""

from __future__ import annotations

from typing import Any

import pytest
from stromy_byok import (
    InMemoryCredentialStore,
    NullCredentialStore,
    Subject,
    SubjectKind,
    ValidationOutcome,
    ValidationStatus,
)

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
def two_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service, "load_contract", lambda _n: _contract("deepseek-api", "openai-api")
    )


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> InMemoryCredentialStore:
    store = InMemoryCredentialStore(credentials.CATALOGUE)
    monkeypatch.setattr(credentials, "credential_store", lambda: store)
    return store


def _register(vault: InMemoryCredentialStore, credential_id: str, slug: str = "stromy") -> None:
    vault.put_version(credential_id, Subject(SubjectKind.CLIENT_SLUG, slug), SECRET)


def _status(scope: CallerScope = STROMY, slug: str = "stromy") -> dict[str, Any]:
    return service.credential_status(WORKFLOW, {"client_slug": slug}, scope)


# --- the readiness answer ----------------------------------------------------


def test_a_partly_registered_client_is_not_ready(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    """The exact 2026-08-26 situation: one of two registered."""
    _register(vault, "openai-api")
    status = _status()
    assert status["ready_to_run"] is False
    assert status["outstanding"] == ["deepseek-api"]
    # And it must SAY so, not leave it to be worked out from the entries.
    assert "deepseek-api" in status["note"]


def test_registering_the_last_one_flips_it_ready(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    _register(vault, "openai-api")
    _register(vault, "deepseek-api")
    status = _status()
    assert status["ready_to_run"] is True
    assert status["outstanding"] == []


def test_the_green_verdict_states_what_it_does_not_cover(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    """Green needs a note more than red does (ORG-237).

    This surface used to explain itself when it BLOCKED a run and go silent
    when it cleared one — and green is where the dangerous misread lives.
    `ready_to_run` answers "is every credential registered", readers are told to
    trust it first, and it is blind to revocation, expiry and quota: in a
    client-funded model, the ordinary ways a key stops working. Demonstrated,
    not theorised — the operator's deleted key still read healthy on 2026-08-27.

    The earlier version of this test asserted `"note" not in status`. That
    assertion WAS the defect, written down.
    """
    _register(vault, "openai-api")
    _register(vault, "deepseek-api")
    note = _status()["note"]
    # It must say a run will start...
    assert "will start" in note
    # ...and, in the same breath, that this is not a liveness check.
    assert "NOT a liveness check" in note
    assert "revoked" in note
    # And it must tell the reader whose word beats this field.
    assert "believe them over this field" in note


def test_the_green_note_dates_the_registration_it_rests_on(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    """A stamp is only meaningful next to the moment it was taken.

    Without the date, "registered" reads as timeless and a key connected months
    ago looks exactly as fresh as one connected a minute ago.
    """
    _register(vault, "openai-api")
    _register(vault, "deepseek-api")
    status = _status()
    oldest = min(
        e["last_rotated_at"] for e in status["credentials"] if e.get("last_rotated_at")
    )
    assert f"Oldest registration: {oldest}." in status["note"]


def test_another_clients_key_does_not_make_this_client_ready(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    """Readiness is per-subject, like every other read on this surface."""
    _register(vault, "openai-api", slug="dukestrategies")
    _register(vault, "deepseek-api", slug="dukestrategies")
    assert _status()["ready_to_run"] is False
    assert _status()["outstanding"] == ["deepseek-api", "openai-api"]


# --- the two answers that must NOT be a plain boolean ------------------------


def test_operator_policy_is_always_ready_and_owes_nothing(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    """`dukestrategies` ships on `operator`, so nothing is registered and
    nothing is owed. Reporting `outstanding` here would invent a to-do list for
    a client whose runs Stromy pays for."""
    status = _status(DUKE, "dukestrategies")
    assert status["credential_policy"] == "operator"
    assert status["ready_to_run"] is True
    assert status["outstanding"] == []


def test_an_unreadable_store_reports_unknown_not_blocked(
    two_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`None`, never `False`.

    `NullCredentialStore.exists` answers False for everyone, which is the same
    boolean as a truthful "not registered" and the opposite fact. `False` here
    would tell a client who HAS connected both keys that they still owe two —
    the identical bug the `unavailable` status exists to prevent, reintroduced
    one level up where it is easier to believe.
    """
    monkeypatch.setattr(credentials, "credential_store", lambda: NullCredentialStore())
    status = _status()
    assert status["ready_to_run"] is None
    assert status["outstanding"] == []


def test_catalogue_drift_blocks_readiness_without_blaming_the_client(
    monkeypatch: pytest.MonkeyPatch, vault: InMemoryCredentialStore
) -> None:
    """A credential nobody can register cannot be an outstanding client action.

    It still blocks the run — so `ready_to_run` must be False — but naming it in
    `outstanding` would send the client hunting for a key that has no signup page.
    """
    monkeypatch.setattr(service, "load_contract", lambda _n: _contract("openai-api", "ghost-api"))
    _register(vault, "openai-api")
    status = _status()
    assert status["ready_to_run"] is False
    assert status["outstanding"] == []
    assert status["catalogue_drift"] == ["ghost-api"]
    assert "operator problem" in status["note"]


# --- the mint call points at the rest of the work ----------------------------


def test_minting_one_link_reports_what_else_is_owed(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    """One trip to the browser instead of one per key."""
    minted = service.create_credential_registration_link(
        WORKFLOW, "openai-api", {"client_slug": "stromy"}, STROMY
    )
    assert minted["still_outstanding"] == ["deepseek-api"]


def test_the_last_link_reports_nothing_else_owed(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    _register(vault, "deepseek-api")
    minted = service.create_credential_registration_link(
        WORKFLOW, "openai-api", {"client_slug": "stromy"}, STROMY
    )
    assert minted["still_outstanding"] == []


def test_a_store_fault_never_costs_the_caller_their_link(
    two_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`still_outstanding` is advice. The link is the deliverable.

    Failing the mint because the advisory lookup broke would throw away a
    perfectly good single-use token and send the client back for another.
    """

    class Exploding(InMemoryCredentialStore):
        def exists(self, *args: Any, **kwargs: Any) -> bool:
            raise RuntimeError("vault unreachable")

    monkeypatch.setattr(credentials, "credential_store", lambda: Exploding(credentials.CATALOGUE))
    minted = service.create_credential_registration_link(
        WORKFLOW, "openai-api", {"client_slug": "stromy"}, STROMY
    )
    assert minted["registration_url"]
    assert "still_outstanding" not in minted


# --- what "registered" does NOT mean -----------------------------------------


def test_the_validation_result_is_named_for_when_it_was_taken(
    two_credentials: None, vault: InMemoryCredentialStore
) -> None:
    """`registered` means a key was saved, not that it still works.

    The validation outcome is stamped once, beside `rotated_at`, at
    `put_version`; nothing re-checks it. On 2026-08-27 the operator deleted the
    OpenAI key they had registered the day before and this surface went on
    reporting it `valid` — `ready_to_run` would have said true for a run
    destined to fail at the provider with a 401.

    Live re-validation is deliberately NOT the fix: it would bill a provider
    round-trip on every status read and still be stale by the time a run
    starts. The honest fix is a field name that cannot be read as present
    tense, so a bare `validation` key must never come back.
    """
    vault.put_version(
        "openai-api",
        Subject(SubjectKind.CLIENT_SLUG, "stromy"),
        SECRET,
        outcome=ValidationOutcome(status=ValidationStatus.VALID),
    )
    entry = next(
        item
        for item in _status()["credentials"]
        if item["credential_id"] == "openai-api"
    )
    assert entry["status"] == "registered"
    assert entry["validation_at_registration"]
    assert "validation" not in entry
    # The stamp is only meaningful next to the moment it was taken.
    assert entry["last_rotated_at"]
