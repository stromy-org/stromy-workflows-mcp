"""Workflow facade use cases, independent of the FastMCP transport wrappers."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from . import registry
from .aca import AcaJobClient, JobStartError, PreparedJob
from .contracts import CallerRole, list_contracts, load_contract
from .scoping import CallerScope, require_client


class JobClient(Protocol):
    async def prepare(self, run_id: str) -> PreparedJob: ...

    async def start(self, template: dict[str, Any]) -> dict[str, Any]: ...


def _role(scope: CallerScope) -> CallerRole:
    return CallerRole.OPERATOR if scope.unrestricted else CallerRole.CLIENT


def _require_run_scope(run: registry.Run | None, scope: CallerScope) -> registry.Run:
    if run is None:
        raise registry.RegistryError("run not found")
    if not scope.unrestricted and run.client_slug not in scope.client_slugs:
        raise PermissionError("run is outside the caller's client scope")
    return run


def list_workflows(scope: CallerScope) -> list[dict[str, Any]]:
    return [load_contract(name).describe(_role(scope)) for name in list_contracts()]


def describe_workflow(name: str, scope: CallerScope) -> dict[str, Any]:
    return load_contract(name).describe(_role(scope))


def validate_config(name: str, config: dict[str, Any], scope: CallerScope) -> dict[str, Any]:
    return load_contract(name).validate(config, _role(scope))


def _persist_start(
    *,
    run_id: str,
    name: str,
    normalized: dict[str, Any],
    client_slug: str,
    template: dict[str, Any],
    image_tag: str | None,
    idempotency_key: str | None,
) -> registry.Run:
    with registry.connect() as conn:
        registry.schema_version(conn)
        return registry.create_run(
            conn,
            run_id=run_id,
            workflow=name,
            config=normalized,
            client_slug=client_slug,
            job_template=template,
            image_tag=image_tag,
            idempotency_key=idempotency_key,
        )


def _mark_failed(run_id: str, error: str) -> None:
    with registry.connect() as conn:
        registry.mark_failed(conn, run_id, error)


async def start_run(
    name: str,
    config: dict[str, Any],
    client_context: dict[str, Any] | None,
    idempotency_key: str | None,
    scope: CallerScope,
    *,
    job_client: JobClient | None = None,
) -> dict[str, Any]:
    context = client_context or {}
    client_slug = require_client(scope, context.get("client_slug"))
    normalized = validate_config(name, config, scope)
    run_id = registry.new_run_id()
    client = job_client or AcaJobClient()
    prepared = await client.prepare(run_id)
    run = await asyncio.to_thread(
        _persist_start,
        run_id=run_id,
        name=name,
        normalized=normalized,
        client_slug=client_slug,
        template=prepared.template,
        image_tag=prepared.image_tag,
        idempotency_key=idempotency_key,
    )
    if run.run_id != run_id:
        return {**run.public(), "idempotent_replay": True}
    try:
        await client.start(prepared.template)
    except JobStartError as exc:
        await asyncio.to_thread(_mark_failed, run_id, str(exc))
        raise
    return run.public()


def run_status(run_id: str, scope: CallerScope) -> dict[str, Any]:
    with registry.connect() as conn:
        run = _require_run_scope(registry.get_run(conn, run_id), scope)
    return run.public()


def list_runs(scope: CallerScope, limit: int = 50) -> list[dict[str, Any]]:
    slugs = None if scope.unrestricted else sorted(scope.client_slugs)
    with registry.connect() as conn:
        runs = registry.list_runs(conn, client_slugs=slugs, limit=min(max(limit, 1), 100))
    return [run.public() for run in runs]


async def resume_run(
    run_id: str,
    resume_payload: Any,
    scope: CallerScope,
    *,
    job_client: JobClient | None = None,
) -> dict[str, Any]:
    def _request() -> tuple[registry.Run, dict[str, Any]]:
        with registry.connect() as conn:
            run = _require_run_scope(registry.get_run(conn, run_id), scope)
            if not run.job_template_json:
                raise registry.RegistryError(f"run {run_id} has no stored job template")
            resumed = registry.request_resume(conn, run_id, resume_payload)
            return resumed, run.job_template_json

    resumed, template = await asyncio.to_thread(_request)
    client = job_client or AcaJobClient()
    try:
        await client.start(template)
    except JobStartError as exc:
        await asyncio.to_thread(_mark_failed, run_id, str(exc))
        raise
    return resumed.public()


def cancel_run(run_id: str, scope: CallerScope) -> dict[str, Any]:
    with registry.connect() as conn:
        _require_run_scope(registry.get_run(conn, run_id), scope)
        cancelled = registry.cancel_run(conn, run_id)
    return cancelled.public()


def get_results(run_id: str, scope: CallerScope) -> dict[str, Any]:
    with registry.connect() as conn:
        run = _require_run_scope(registry.get_run(conn, run_id), scope)
    return {
        "run_id": run.run_id,
        "status": run.status,
        "artifacts": run.artifacts_json or {},
        "error": run.error,
    }
