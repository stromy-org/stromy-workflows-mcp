"""Domain tests for the workflow facade."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stromy_workflows_mcp import registry, server, service
from stromy_workflows_mcp.aca import PreparedJob
from stromy_workflows_mcp.config import settings
from stromy_workflows_mcp.contracts import CallerRole, ConfigRejected, load_contract
from stromy_workflows_mcp.scoping import CallerScope, resolve_scope


async def test_server_exposes_workflow_lifecycle_tools(client):
    tools = await client.list_tools()
    names = {item.name for item in tools}
    assert {
        "list_workflows",
        "describe_workflow",
        "validate_config",
        "start_run",
        "run_status",
        "list_runs",
        "resume_run",
        "cancel_run",
        "get_results",
    } <= names
    assert "echo" not in names


def test_client_contract_hides_and_rejects_tier3() -> None:
    contract = load_contract("weekly_intel_workflow")
    visible = {item["name"] for item in contract.describe(CallerRole.CLIENT)["keys"]}
    assert "research.request_text" in visible
    assert "research.model_tier" not in visible
    with pytest.raises(ConfigRejected) as exc:
        contract.validate(
            {"research": {"request_text": "x", "model_tier": "cheap"}},
            CallerRole.CLIENT,
        )
    assert exc.value.code == "tier3_forbidden"


def test_verified_client_roles_are_a_union_and_default_deny() -> None:
    scope = resolve_scope(
        {"sub": "caller", "roles": ["client.dukestrategies", "client.stromy"]},
        role_prefix="client.",
        operator_role="operator",
    )
    assert scope.client_slugs == {"dukestrategies", "stromy"}
    denied = resolve_scope(
        {"sub": "caller", "roles": []},
        role_prefix="client.",
        operator_role="operator",
    )
    assert not denied.unrestricted
    assert not denied.client_slugs


def _run(run_id: str, *, config: dict, template: dict) -> registry.Run:
    now = datetime.now(UTC)
    return registry.Run(
        run_id=run_id,
        workflow="stakeholder_analysis_workflow",
        thread_id=run_id,
        status="queued",
        client_slug="dukestrategies",
        config_json=config,
        image_tag="runner:test",
        job_template_json=template,
        created_at=now,
        updated_at=now,
        interrupt_payload=None,
        error=None,
        artifacts_json=None,
        idempotency_key=None,
    )


@pytest.mark.asyncio
async def test_template_injection_guard(monkeypatch) -> None:
    sentinel = "CALLER-SENTINEL-DO-NOT-LEAK"
    template = {
        "containers": [{"name": "runner", "image": "runner:test", "args": ["--run-id", "fixed"]}]
    }
    captured: dict[str, object] = {}

    class FakeJobClient:
        async def prepare(self, run_id: str) -> PreparedJob:
            captured["prepared_run_id"] = run_id
            return PreparedJob(template=template, image_tag="runner:test")

        async def start(self, template: dict[str, object]) -> dict[str, object]:
            captured["started_template"] = template
            return {"accepted": True}

    def fake_persist(**kwargs):
        captured.update(kwargs)
        return _run(kwargs["run_id"], config=kwargs["normalized"], template=kwargs["template"])

    monkeypatch.setattr(service, "_persist_start", fake_persist)
    result = await service.start_run(
        "stakeholder_analysis_workflow",
        {"decision_summary": sentinel, "inputs_md_folder": "/inputs/duke"},
        {"client_slug": "dukestrategies"},
        None,
        CallerScope(frozenset({"dukestrategies"})),
        job_client=FakeJobClient(),
    )

    assert sentinel in json.dumps(captured["normalized"])
    assert sentinel not in json.dumps(captured["template"])
    assert sentinel not in json.dumps(captured["started_template"])
    assert result["status"] == "queued"


def test_health_fails_loudly_on_schema_mismatch(monkeypatch) -> None:
    @contextmanager
    def fake_connect():
        yield object()

    monkeypatch.setattr(settings, "stromy_pg_dsn", "postgresql://test")
    monkeypatch.setattr(registry, "connect", fake_connect)
    monkeypatch.setattr(
        registry,
        "schema_version",
        lambda conn: (_ for _ in ()).throw(registry.SchemaVersionMismatch("live v99")),
    )
    with TestClient(server.mcp.http_app()) as http:
        response = http.get("/health")
    assert response.status_code == 503
    assert "live v99" in response.json()["error"]


def test_facade_contains_no_schema_ddl() -> None:
    assert registry.__file__ is not None
    source = Path(registry.__file__).read_text()
    assert "CREATE" + " TABLE" not in source
