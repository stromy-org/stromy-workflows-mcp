"""What a client learns when a run dies (ORG-PLAN-300 §5).

Run ``284e0cdf`` failed and returned ``"cannot rebind an active AuditLogger to
another trace"`` verbatim to the client surface — an internal invariant, useless
to the reader and a description of our internals to anyone else. This asserts the
line drawn afterwards: a scoped caller gets *where* it died, whether another run
is worth funding, whose money that would be, and a correlation id to quote; the
free text stays with the operator.
"""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime
from typing import Any

import pytest

from stromy_workflows_mcp import registry, service
from stromy_workflows_mcp.scoping import CallerScope

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)

RAW = "cannot rebind an active AuditLogger to another trace"

CLIENT = CallerScope(frozenset({"dukestrategies"}))
OPERATOR = CallerScope(frozenset(), unrestricted=True)


def _failed_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "workflow": "stakeholder_analysis_workflow",
        "thread_id": "11111111-1111-1111-1111-111111111111",
        "status": "failed",
        "client_slug": "dukestrategies",
        "config_json": {},
        "image_tag": "sha-abc",
        "job_template_json": None,
        "created_at": NOW,
        "updated_at": NOW,
        "interrupt_payload": None,
        "error": RAW,
        "artifacts_json": None,
        "idempotency_key": None,
        "workspace_id": "22222222-2222-2222-2222-222222222222",
        "retry_of": None,
        "attempt_no": 1,
        "dispatch_id": None,
        "lease_owner": None,
        "lease_expires_at": None,
        "heartbeat_at": NOW,
        "progress_json": {"node": "run_driver_discovery", "nodes_completed": 7},
        "error_json": {
            "stage": "graph",
            "error_type": "AdapterError",
            "message": RAW,
            "retryable": False,
            "reason": "deterministic-repeat",
            "spends": "client",
            "correlation_id": "44444444-4444-4444-4444-444444444444",
        },
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


def test_a_client_never_sees_the_raw_exception_text(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _status(_failed_row(), CLIENT, monkeypatch)

    assert RAW not in str(payload)
    assert payload["error"] == "the run failed at stage graph"
    assert "message" not in payload["failure"]


def test_a_client_still_learns_everything_it_can_act_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The redaction narrows what the client learns about US, not about their run.

    Without this the change would be indistinguishable from simply deleting the
    failure block, which is a worse surface than the one being fixed.
    """
    failure = _status(_failed_row(), CLIENT, monkeypatch)["failure"]

    assert failure["stage"] == "graph"
    assert failure["error_type"] == "AdapterError"
    assert failure["retryable"] is False
    assert failure["reason"] == "deterministic-repeat"
    assert failure["spends"] == "client"
    assert failure["correlation_id"] == "44444444-4444-4444-4444-444444444444"


def test_an_operator_sees_the_message_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """NEGATIVE CONTROL. The point is a caller-scope distinction, not a blanket
    deletion — an operator debugging the run needs the frames' own words, and a
    redaction that fired for everyone would pass every client-side assertion here
    while destroying the only surface that can diagnose it."""
    payload = _status(_failed_row(), OPERATOR, monkeypatch)

    assert payload["error"] == RAW
    assert payload["failure"]["message"] == RAW


def test_a_failure_with_no_structured_block_is_still_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1-era row carries ``error`` and no ``error_json``. The raw text is no
    less internal for having no structure around it."""
    payload = _status(_failed_row(error_json=None), CLIENT, monkeypatch)

    assert RAW not in str(payload)
    assert payload["error"] == "the run failed"
    assert "failure" not in payload


def test_a_run_that_has_not_failed_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """NEGATIVE CONTROL for the other direction: a redactor that rewrote `error`
    on every run would put a failure sentence on a healthy one."""
    payload = _status(
        _failed_row(status="running", error=None, error_json=None), CLIENT, monkeypatch
    )

    assert payload["error"] is None
    assert payload["status"] == "running"


def test_every_run_returning_path_goes_through_the_redactor() -> None:
    """The redaction is only as good as its coverage, and the failure mode is a
    call site that was added later and kept ``.public()``. Sessions are a
    different object and legitimately keep theirs.
    """
    import inspect

    source = inspect.getsource(service)
    run_projections = [
        line.strip()
        for line in source.splitlines()
        if ".public()" in line and "session" not in line
    ]

    # The single surviving one is inside ``_run_payload`` itself.
    assert run_projections == ["payload = run.public()"], run_projections
