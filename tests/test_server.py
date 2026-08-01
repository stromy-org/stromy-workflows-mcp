"""Domain tests for the workflow facade."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from stromy_workflows_mcp import aca, entitlements, registry, server, service
from stromy_workflows_mcp.aca import PreparedJob
from stromy_workflows_mcp.config import settings
from stromy_workflows_mcp.contracts import (
    CallerRole,
    ConfigRejected,
    ContractError,
    load_contract,
)
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


def _run(
    run_id: str,
    *,
    config: dict,
    template: dict,
    client_slug: str = "dukestrategies",
) -> registry.Run:
    now = datetime.now(UTC)
    return registry.Run(
        run_id=run_id,
        workflow="stakeholder_analysis_workflow",
        thread_id=run_id,
        status="queued",
        client_slug=client_slug,
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


def test_client_scope_filters_lists_and_denies_cross_tenant_reads(monkeypatch) -> None:
    duke = _run("duke-run", config={}, template={})
    stromy = _run("stromy-run", config={}, template={}, client_slug="stromy")
    scope = CallerScope(frozenset({"dukestrategies"}))

    @contextmanager
    def fake_connect():
        yield object()

    def fake_list_runs(conn, *, client_slugs, limit):
        del conn, limit
        assert client_slugs == ["dukestrategies"]
        return [duke]

    monkeypatch.setattr(registry, "connect", fake_connect)
    monkeypatch.setattr(registry, "list_runs", fake_list_runs)
    assert [item["run_id"] for item in service.list_runs(scope)] == ["duke-run"]

    monkeypatch.setattr(registry, "get_run", lambda conn, run_id: stromy)
    with pytest.raises(PermissionError, match="outside the caller's client scope"):
        service.run_status("stromy-run", scope)


def _entitlements(monkeypatch, table: dict[str, frozenset[str]]) -> None:
    monkeypatch.setattr(entitlements, "load_entitlements", lambda: table)


def _broken_entitlements(monkeypatch) -> None:
    def explode() -> dict[str, frozenset[str]]:
        raise entitlements.EntitlementError("registry is corrupt")

    monkeypatch.setattr(entitlements, "load_entitlements", explode)


def test_list_workflows_filters_to_entitled_workflows(monkeypatch) -> None:
    _entitlements(
        monkeypatch,
        {"stakeholder_analysis_workflow": frozenset({"dukestrategies"})},
    )
    scope = CallerScope(frozenset({"dukestrategies"}))
    assert [item["workflow"] for item in service.list_workflows(scope)] == [
        "stakeholder_analysis_workflow"
    ]
    # The operator still sees the whole catalog, including the operator-only one.
    operator = CallerScope(frozenset(), unrestricted=True)
    assert "weekly_intel_workflow" in {
        item["workflow"] for item in service.list_workflows(operator)
    }


def test_unlisted_workflow_defaults_to_deny(monkeypatch) -> None:
    _entitlements(monkeypatch, {})
    scope = CallerScope(frozenset({"dukestrategies"}))
    assert service.list_workflows(scope) == []
    with pytest.raises(ContractError):
        service.describe_workflow("weekly_intel_workflow", scope)


def test_describe_uses_the_union_of_the_callers_roles(monkeypatch) -> None:
    _entitlements(
        monkeypatch,
        {"stakeholder_analysis_workflow": frozenset({"dukestrategies"})},
    )
    scope = CallerScope(frozenset({"dukestrategies", "amaris"}))
    described = service.describe_workflow("stakeholder_analysis_workflow", scope)
    assert described["workflow"] == "stakeholder_analysis_workflow"


def test_unknown_and_unentitled_workflows_are_indistinguishable(monkeypatch) -> None:
    """A client must not be able to enumerate the catalog by diffing error strings."""
    _entitlements(
        monkeypatch,
        {"stakeholder_analysis_workflow": frozenset({"dukestrategies"})},
    )
    scope = CallerScope(frozenset({"amaris"}))
    with pytest.raises(ContractError) as unentitled:
        service.describe_workflow("stakeholder_analysis_workflow", scope)
    with pytest.raises(ContractError) as nonexistent:
        service.describe_workflow("no_such_workflow", scope)
    assert str(unentitled.value) == "unknown workflow 'stakeholder_analysis_workflow'"
    assert str(nonexistent.value) == "unknown workflow 'no_such_workflow'"


def test_client_fails_closed_when_the_registry_is_unreadable(monkeypatch) -> None:
    _broken_entitlements(monkeypatch)
    scope = CallerScope(frozenset({"dukestrategies"}))
    assert service.list_workflows(scope) == []
    with pytest.raises(ContractError):
        service.describe_workflow("stakeholder_analysis_workflow", scope)


def test_operator_bypasses_an_unreadable_registry(monkeypatch) -> None:
    """A bad deploy must never lock the operator out of their own estate."""
    _broken_entitlements(monkeypatch)
    operator = CallerScope(frozenset(), unrestricted=True)
    assert {item["workflow"] for item in service.list_workflows(operator)} == {
        "stakeholder_analysis_workflow",
        "weekly_intel_workflow",
    }
    assert service.describe_workflow("weekly_intel_workflow", operator)


def test_fs_roots_never_widen_beyond_skills() -> None:
    """The skills jail has no CallerScope awareness (fs_tools.py).

    Widening it to ``components`` would hand every contract and the entitlements
    registry itself to any authenticated caller of any role.
    """
    assert settings.fs_roots == ["skills"]


@pytest.mark.asyncio
async def test_start_run_denies_a_non_entitled_resolved_owner(monkeypatch) -> None:
    """The escalation regression: entitlement follows the run OWNER, not the union.

    A caller holding both ``client.dukestrategies`` and ``client.amaris``, entitled
    only via Duke, must not be able to start a run *owned by amaris*. ``validate_config``
    is union-scoped and cannot catch this, so ``start_run`` checks the resolved owner.
    """
    _entitlements(
        monkeypatch,
        {"stakeholder_analysis_workflow": frozenset({"dukestrategies"})},
    )
    monkeypatch.setattr(service, "_persist_start", lambda **kwargs: pytest.fail("run persisted"))
    scope = CallerScope(frozenset({"dukestrategies", "amaris"}))

    with pytest.raises(PermissionError, match="not entitled to workflow"):
        await service.start_run(
            "stakeholder_analysis_workflow",
            {"decision_summary": "x", "inputs_md_folder": "/inputs/amaris"},
            {"client_slug": "amaris"},
            None,
            scope,
        )


@pytest.mark.asyncio
async def test_start_run_denies_an_operator_only_workflow(monkeypatch) -> None:
    _entitlements(monkeypatch, {"weekly_intel_workflow": frozenset()})
    monkeypatch.setattr(service, "_persist_start", lambda **kwargs: pytest.fail("run persisted"))
    with pytest.raises(ContractError):
        await service.start_run(
            "weekly_intel_workflow",
            {"research": {"request_text": "x"}},
            {"client_slug": "dukestrategies"},
            None,
            CallerScope(frozenset({"dukestrategies"})),
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
        {
            "decision_summary": sentinel,
            # ORG-PLAN-164: evidence arrives as an ownership-checked handle, not
            # as a server-side path (see the raw-path test below).
            "inputs_md_folder": "inputset:11111111-1111-1111-1111-111111111111",
        },
        {"client_slug": "dukestrategies"},
        None,
        CallerScope(frozenset({"dukestrategies"})),
        job_client=FakeJobClient(),
    )

    assert sentinel in json.dumps(captured["normalized"])
    assert sentinel not in json.dumps(captured["template"])
    assert sentinel not in json.dumps(captured["started_template"])
    assert result["status"] == "queued"
    # The handle reaches _persist_start, which attaches it in the run's own
    # transaction — a run can never be dispatched referencing evidence it does
    # not own.
    assert captured["input_handle"] == "inputset:11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_a_server_side_path_is_no_longer_accepted_as_evidence() -> None:
    """A raw path would name a folder on a share every runner can see.

    Before ORG-PLAN-164 ``inputs_md_folder`` was a free string, so a client
    could point a run at any path the runner could reach. The contract pattern
    is what closes that; the ownership check on the handle is what replaces it.
    """
    with pytest.raises(ConfigRejected) as exc:
        service.validate_config(
            "stakeholder_analysis_workflow",
            {"decision_summary": "x", "inputs_md_folder": "/mnt/runs/othercli/secrets"},
            CallerScope(frozenset({"dukestrategies"})),
        )
    assert exc.value.code == "schema_invalid"


@pytest.mark.asyncio
async def test_prepare_emits_a_top_level_job_execution_template(monkeypatch) -> None:
    """ARM's ``jobs/start`` takes a JobExecutionTemplate, not the job's own template.

    Regression for the live 400 seen on 2026-07-26:
    ``Unknown properties template in StartJobExecutionTemplate are not supported``.
    The body must be ``{"containers": [...]}`` at the top level, carrying only the
    keys the execution schema accepts — ``probes``/``volumes``/``volumeMounts`` are
    job-template members that ARM rejects here.
    """
    job_template = {
        "containers": [
            {
                "name": "stromy-runner",
                "image": "ghcr.io/stromy-org/stromy-runner:sha-deadbeef",
                "env": [{"name": "STROMY_PG_DSN", "secretRef": "stromy-pg-dsn", "value": ""}],
                "resources": {"cpu": 1, "memory": "2Gi"},
                "probes": [],
                "volumeMounts": [{"volumeName": "v", "mountPath": "/mnt"}],
            }
        ],
        "initContainers": None,
        "volumes": [],
    }

    class FakeResponse:
        is_error = False

        @staticmethod
        def json() -> dict[str, object]:
            return {"properties": {"template": job_template}}

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *args, **kwargs):
            del args, kwargs
            return FakeResponse()

    monkeypatch.setattr(aca.settings, "azure_subscription_id", "sub-id")
    monkeypatch.setattr(aca, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(aca.httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(
        aca.AcaJobClient, "_headers", lambda self: _immediate({"Authorization": "Bearer x"})
    )

    prepared = await aca.AcaJobClient().prepare("run-123")

    # Top-level JobExecutionTemplate — never a {"template": ...} envelope.
    assert set(prepared.template) == {"containers"}
    container = prepared.template["containers"][0]
    assert set(container) == {"image", "name", "env", "resources", "command", "args"}
    assert container["args"] == ["-m", "stromy.runtime.worker", "--run-id", "run-123"]
    # secretRef env survives the override, or the run starts without its DSN.
    assert container["env"] == job_template["containers"][0]["env"]
    assert prepared.image_tag == "ghcr.io/stromy-org/stromy-runner:sha-deadbeef"


@pytest.mark.asyncio
async def test_start_posts_the_template_unwrapped(monkeypatch) -> None:
    """The POST body IS the JobExecutionTemplate — no ``{"template": ...}`` envelope."""
    template = {"containers": [{"name": "runner", "image": "runner:test"}]}
    sent: dict[str, object] = {}

    class FakeResponse:
        is_error = False
        content = b"{}"

        @staticmethod
        def json() -> dict[str, object]:
            return {"name": "exec-1"}

    class FakeHttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, *, params, headers, json):
            sent.update(url=url, params=params, json=json)
            return FakeResponse()

    monkeypatch.setattr(aca.settings, "azure_subscription_id", "sub-id")
    monkeypatch.setattr(aca, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(aca.httpx, "AsyncClient", lambda *a, **k: FakeHttpClient())
    monkeypatch.setattr(
        aca.AcaJobClient, "_headers", lambda self: _immediate({"Authorization": "Bearer x"})
    )

    assert await aca.AcaJobClient().start(template) == {"name": "exec-1"}
    body = sent["json"]
    assert isinstance(body, dict)
    assert body == template
    assert "template" not in body
    assert str(sent["url"]).endswith("/start")


async def _immediate(value):
    return value


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
