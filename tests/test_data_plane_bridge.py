"""Schema-expansion bridge + adapter entitlement gate (ORG-PLAN-164 WS0).

Two guarantees, both of which only matter *during* a migration and are therefore
easy to leave untested until they break in production:

1. The facade keeps serving while Stromy migrates the registry v1 -> v2. Deploy
   order is migrate-then-deploy, so a facade pinned to a single schema version
   takes the hosted surface down the instant the migration lands.
2. A workflow cannot be entitled to a client while its data plane is a fiction —
   a required input with no upload path, or outputs that never leave the
   container.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from stromy_workflows_mcp import registry
from stromy_workflows_mcp.contracts import load_contract
from stromy_workflows_mcp.entitlements import (
    KNOWN_ARTIFACT_ADAPTERS,
    KNOWN_INPUT_ADAPTERS,
    adapter_problems,
    load_entitlements,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _v1_row(**overrides: Any) -> dict[str, Any]:
    """A row exactly as a schema-v1 ``SELECT *`` returns it."""
    row: dict[str, Any] = {
        "run_id": "11111111-1111-1111-1111-111111111111",
        "workflow": "stakeholder_analysis_workflow",
        "thread_id": "11111111-1111-1111-1111-111111111111",
        "status": "queued",
        "client_slug": "dukestrategies",
        "config_json": {},
        "image_tag": "sha-abc",
        "job_template_json": None,
        "created_at": NOW,
        "updated_at": NOW,
        "interrupt_payload": None,
        "error": None,
        "artifacts_json": None,
        "idempotency_key": None,
    }
    row.update(overrides)
    return row


def _v2_row(**overrides: Any) -> dict[str, Any]:
    row = _v1_row()
    row.update(
        {
            "workspace_id": "22222222-2222-2222-2222-222222222222",
            "retry_of": None,
            "attempt_no": 1,
            "dispatch_id": "33333333-3333-3333-3333-333333333333",
            "lease_owner": "runner-7",
            "lease_expires_at": NOW,
            "progress_json": {"current_node": "scoring", "completed_nodes": ["load"]},
            "error_json": None,
            "heartbeat_at": NOW,
        }
    )
    row.update(overrides)
    return row


# --- Schema bounds -----------------------------------------------------------


def test_supported_schema_range_spans_the_expansion() -> None:
    assert registry.SUPPORTED_SCHEMA_MIN == 1
    assert registry.SUPPORTED_SCHEMA_MAX == registry.DATA_PLANE_SCHEMA == 2


def test_data_plane_features_refuse_v1_with_a_named_error() -> None:
    """v1 is servable; it just cannot record data-plane state."""
    registry.require_data_plane(2, "start_run(inputset)")
    with pytest.raises(registry.SchemaFeatureUnavailable, match="requires registry schema v2"):
        registry.require_data_plane(1, "start_run(inputset)")


def test_feature_unavailable_is_not_a_version_mismatch() -> None:
    """Distinct failure classes — one is 'do not serve', one is 'not yet'."""
    assert issubclass(registry.SchemaFeatureUnavailable, registry.RegistryError)
    assert not issubclass(registry.SchemaFeatureUnavailable, registry.SchemaVersionMismatch)


# --- Row mapping across both schemas -----------------------------------------


def test_v1_row_maps_with_data_plane_fields_absent() -> None:
    run = registry.Run.from_row(_v1_row())
    assert run.workspace_id is None
    assert run.progress_json is None
    # ``attempt_no`` defaults rather than nulling, so it is NOT the v1 marker —
    # ``workspace_id`` is (NOT NULL from migration 0002 onward).
    assert run.attempt_no == 1


def test_v2_row_maps_without_choking_on_new_columns() -> None:
    """The failure this prevents: `cls(**row)` raising on unknown kwargs."""
    run = registry.Run.from_row(_v2_row())
    assert run.workspace_id == "22222222-2222-2222-2222-222222222222"
    assert run.attempt_no == 1
    assert run.progress_json == {"current_node": "scoring", "completed_nodes": ["load"]}


def test_unknown_future_columns_are_ignored_not_fatal() -> None:
    """A v3 column must not take a v2-compiled facade down."""
    run = registry.Run.from_row(_v2_row(some_future_column="whatever"))
    assert run.run_id == "11111111-1111-1111-1111-111111111111"


def test_public_omits_data_plane_keys_on_v1() -> None:
    payload = registry.Run.from_row(_v1_row()).public()
    assert "progress" not in payload
    assert "attempt" not in payload
    assert "heartbeat_at" not in payload
    # The v1 surface itself is untouched.
    assert payload["run_id"] and payload["status"] == "queued"


def test_public_exposes_data_plane_keys_on_v2() -> None:
    payload = registry.Run.from_row(_v2_row()).public()
    assert payload["progress"]["current_node"] == "scoring"
    assert payload["attempt"] == {"attempt_no": 1, "retry_of": None}
    assert payload["heartbeat_at"] == NOW.isoformat()


def test_public_never_leaks_lease_or_dispatch_internals() -> None:
    """Lease ownership is runner bookkeeping, not a client-facing fact."""
    payload = json.dumps(registry.Run.from_row(_v2_row()).public())
    assert "lease_owner" not in payload
    assert "runner-7" not in payload
    assert "dispatch_id" not in payload


# --- Adapter declarations ----------------------------------------------------


def test_shipped_contracts_declare_known_adapters() -> None:
    for workflow in ("stakeholder_analysis_workflow", "weekly_intel_workflow"):
        schema = load_contract(workflow).schema
        assert schema["x-input-adapter"] in KNOWN_INPUT_ADAPTERS
        assert schema["x-artifact-adapter"] in KNOWN_ARTIFACT_ADAPTERS


def test_entitled_workflow_declares_a_real_data_plane() -> None:
    """The live registry must already satisfy the gate."""
    table = load_entitlements()
    for workflow, clients in table.items():
        schema = load_contract(workflow).schema
        assert adapter_problems(workflow, schema, has_clients=bool(clients)) == []


def test_gate_rejects_a_missing_declaration() -> None:
    problems = adapter_problems("demo", {"x-artifact-adapter": "operator"}, has_clients=False)
    assert len(problems) == 1
    assert "declares no x-input-adapter" in problems[0]


def test_gate_rejects_an_unknown_adapter_name() -> None:
    schema = {"x-input-adapter": "inputset", "x-artifact-adapter": "sftp_dropbox"}
    problems = adapter_problems("demo", schema, has_clients=True)
    assert len(problems) == 1
    assert "unknown x-artifact-adapter" in problems[0]


def test_gate_rejects_operator_sentinels_on_a_client_entitled_workflow() -> None:
    """The 'entitled but unusable' case this gate exists for."""
    schema = {"x-input-adapter": "none", "x-artifact-adapter": "operator"}
    problems = adapter_problems("demo", schema, has_clients=True)
    assert len(problems) == 2
    assert all("operator-only" in problem for problem in problems)


def test_gate_allows_operator_sentinels_when_there_are_no_clients() -> None:
    schema = {"x-input-adapter": "none", "x-artifact-adapter": "operator"}
    assert adapter_problems("demo", schema, has_clients=False) == []


def test_check_entitlements_script_passes_against_the_shipped_registry() -> None:
    """Executable proof of the committed CI gate, not a re-implementation."""
    import subprocess  # noqa: PLC0415 - test-local by design
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts" / "check_entitlements.py"
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "in=inputset out=stakeholder_exports" in result.stdout
