"""A degraded run says so on the surface a caller actually reads (ORG-PLAN-300 AC10).

`record_degradations` writes into `runs.execution_metadata_json`, and this facade's
one run projection — `_run_payload` -> `RunRecord.public()` -> `public_execution_metadata`
— filters that column through `PUBLIC_EXECUTION_METADATA_KEYS`, a constant that lives
in `workflow-runtime-core`. So the allowlist a client sees is decided by **this repo's
pin**, not by the library's `main`.

That is not hypothetical. Measured 2026-09-16: core v0.11.1 added `degradations` to the
allowlist and the runner pinned it, while this repo stayed on v0.9.0 — where the set is
`{"credential_sources"}` alone. Every degradation the runner recorded was stripped before
it reached anyone, operator included, and the whole suite stayed green because nothing
here asserted the projection. The proof run then read the absent key as "no degradations
occurred", which it could not have distinguished from "this facade cannot emit it".

Hence these tests. They fail on any pin whose allowlist lacks `degradations`, which is
what makes the bump a gate rather than a version string.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import pytest
from workflow_runtime_core.models import PUBLIC_EXECUTION_METADATA_KEYS

from stromy_workflows_mcp import registry, service
from stromy_workflows_mcp.scoping import CallerScope

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

CLIENT = CallerScope(frozenset({"stromy"}))
OPERATOR = CallerScope(frozenset(), unrestricted=True)

#: One unfunded optional channel, exactly as `binding.py` records it, plus the
#: `credential_sources` entry whose presence is the witness that binding ran at all.
EXECUTION_METADATA: dict[str, Any] = {
    "credential_sources": {"1": {"openai-api": "caller-byok", "serper-api": "operator-env"}},
    "degradations": {"1": [{"kind": "credential_unfunded", "credential_id": "tavily-api"}]},
    # Server-derived and never projected: the funding map, the resolved aliases, the
    # image tag it was pinned against. A caller seeing this would be a regression.
    "pinned": {"funding": {"openai-api": "client", "tavily-api": "operator"}},
}


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_id": "33333333-3333-3333-3333-333333333333",
        "workflow": "stakeholder_analysis_workflow",
        "thread_id": "33333333-3333-3333-3333-333333333333",
        "status": "running",
        "client_slug": "stromy",
        "config_json": {},
        "image_tag": "sha-a3848f0d",
        "job_template_json": None,
        "created_at": NOW,
        "updated_at": NOW,
        "interrupt_payload": None,
        "error": None,
        "artifacts_json": None,
        "idempotency_key": None,
        "workspace_id": "44444444-4444-4444-4444-444444444444",
        "retry_of": None,
        "attempt_no": 1,
        "dispatch_id": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "heartbeat_at": NOW,
        "progress_json": {"node": "run_orchestrated_sourcing", "nodes_completed": 1},
        "error_json": None,
        "execution_metadata_json": EXECUTION_METADATA,
    }
    row.update(overrides)
    return row


def _status(
    row: dict[str, Any], scope: CallerScope, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    run = registry.Run.from_row(row)
    monkeypatch.setattr(registry, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(registry, "get_run", lambda _conn, _run_id: run)
    return service.run_status(run.run_id, scope)


def test_the_pinned_core_can_emit_degradations() -> None:
    """The pin check, stated as an assertion rather than left to a reviewer.

    Deliberately separate from the projection tests below: when the pin regresses,
    this names the cause in one line instead of leaving three payload assertions to
    be read as a service bug.
    """
    assert "degradations" in PUBLIC_EXECUTION_METADATA_KEYS, (
        "the pinned workflow-runtime-core cannot project `degradations`; a degraded run "
        "would look identical to a clean one on every surface this facade serves. "
        "Check [tool.uv.sources] workflow-runtime-core — it needs >= v0.10.1."
    )


@pytest.mark.parametrize("scope", [CLIENT, OPERATOR], ids=["client", "operator"])
def test_a_degradation_reaches_the_caller(
    scope: CallerScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both callers, because `_run_payload` returns ONE projection.

    The operator path is not a courtesy here — `scope.unrestricted` returns early from
    the same `run.public()` payload, so an allowlist that drops the key drops it for
    whoever is debugging the run too.
    """
    execution = _status(_row(), scope, monkeypatch)["execution"]

    assert execution["degradations"]["1"] == [
        {"kind": "credential_unfunded", "credential_id": "tavily-api"}
    ]


@pytest.mark.parametrize("scope", [CLIENT, OPERATOR], ids=["client", "operator"])
def test_the_pinned_block_never_reaches_the_caller(
    scope: CallerScope, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NEGATIVE CONTROL: widening the allowlist must not widen it to everything.

    Without this, a future "just project the whole column" would satisfy the test
    above while publishing the funding map and the resolved credential aliases.
    """
    payload = _status(_row(), scope, monkeypatch)

    assert "pinned" not in payload["execution"]
    assert "funding" not in str(payload)


def test_an_undegraded_run_carries_no_degradations_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absence is only evidence once presence is possible — this pins the other half.

    `record_degradations` returns early on an empty sequence, so a clean attempt writes
    no key at all. With the test above proving the key CAN appear, its absence here is
    readable as "none occurred" rather than "this surface cannot say".
    """
    clean = {"credential_sources": EXECUTION_METADATA["credential_sources"]}
    execution = _status(_row(execution_metadata_json=clean), CLIENT, monkeypatch)["execution"]

    assert execution["credential_sources"], "the binding witness must survive too"
    assert "degradations" not in execution
