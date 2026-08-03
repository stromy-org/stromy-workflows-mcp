"""Workflow facade use cases, independent of the FastMCP transport wrappers."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from . import registry
from .aca import AcaJobClient, JobStartError, PreparedJob
from .contracts import CallerRole, Contract, load_contract
from .entitlements import require_entitled, require_visible, visible_workflows
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
    return [load_contract(name).summarize(_role(scope)) for name in visible_workflows(scope)]


def describe_workflow(name: str, scope: CallerScope) -> dict[str, Any]:
    require_visible(name, scope)
    return load_contract(name).describe(_role(scope))


def _validated(
    name: str, config: dict[str, Any], scope: CallerScope
) -> tuple[Contract, dict[str, Any]]:
    """Validate and return the FULL effective config, provider pins included.

    This is the internal shape: ``start_run`` persists it for the runner, which
    needs the pins to run the right stages. It must never be handed to a caller —
    route caller-facing returns through ``validate_config`` so ``Contract.project``
    withholds tier 3.
    """
    require_visible(name, scope)
    contract = load_contract(name)
    return contract, contract.validate(config, _role(scope))


def validate_config(
    name: str,
    config: dict[str, Any],
    scope: CallerScope,
    client_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Caller-facing normalized config, plus the identity it would run under.

    ``config`` has provider-locked keys withheld (``Contract.project``).

    When ``client_context`` is supplied this also resolves and returns the run
    OWNER, applying the same two authorization gates ``start_run`` applies. That
    makes this call a true dry run of the submission rather than a check of its
    configuration half. It exists because the owner is the one value a pre-flight
    confirmation block could not verify: every other line came back from here,
    while the client identity — which decides whose brand ships — came from
    whatever the calling agent resolved locally, and nothing checked it until the
    billed call. An echoed owner is a server answer the user can actually confirm.
    """
    contract, effective = _validated(name, config, scope)
    resolved: dict[str, Any] = {"config": contract.project(effective, _role(scope))}
    if client_context is not None:
        owner = require_client(scope, client_context.get("client_slug"))
        require_entitled(name, owner, scope)
        resolved["client_slug"] = owner
    return resolved


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
    # Checked against the RESOLVED owner, not the caller's union of roles: a caller
    # holding two client roles must not start a run owned by the unentitled one.
    # ``require_visible``'s gate is union-scoped and cannot make this distinction.
    # ``validate_config`` runs these same two gates when given a client_context, so
    # the dry run and the billed call agree — but it is optional there and
    # authoritative here, so this stays the enforcement point.
    require_entitled(name, client_slug, scope)
    # The INTERNAL shape: the runner needs the provider pins, so this must not go
    # through the caller-facing projection.
    _, normalized = _validated(name, config, scope)
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
