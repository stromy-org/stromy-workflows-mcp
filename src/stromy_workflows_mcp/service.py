"""Workflow facade use cases, independent of the FastMCP transport wrappers."""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

from . import input_sessions, registry
from .aca import AcaJobClient, JobStartError, PreparedJob
from .contracts import CallerRole, load_contract
from .dispatch import (
    Dispatcher,
    DispatchError,
    QueueDispatcher,
    build_dispatcher,
    new_dispatch_id,
)
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
    return [load_contract(name).describe(_role(scope)) for name in visible_workflows(scope)]


def describe_workflow(name: str, scope: CallerScope) -> dict[str, Any]:
    require_visible(name, scope)
    return load_contract(name).describe(_role(scope))


def validate_config(name: str, config: dict[str, Any], scope: CallerScope) -> dict[str, Any]:
    require_visible(name, scope)
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
    dispatch_id: str | None = None,
    input_handle: str | None = None,
    scope: CallerScope | None = None,
) -> registry.Run:
    with registry.connect() as conn:
        version = registry.schema_version(conn)
        run = registry.create_run(
            conn,
            run_id=run_id,
            workflow=name,
            config=normalized,
            client_slug=client_slug,
            job_template=template,
            image_tag=image_tag,
            idempotency_key=idempotency_key,
        )
        # The row must already know its dispatch id when the message lands, or a
        # fast runner could claim against a row that has not been told which
        # dispatch it belongs to — and reject its own message. Recorded inside
        # the same transaction as the insert, so it is committed before anything
        # is enqueued.
        if run.run_id == run_id:
            # Attach the input set in the SAME transaction as the insert, so a
            # run can never be dispatched referencing evidence it does not own.
            # An unowned or unfinalized handle raises here and rolls the whole
            # run back, rather than leaving a run pointing at nothing.
            if input_handle is not None and scope is not None:
                registry.require_data_plane(version, "input sets")
                session_id = input_sessions.attach_to_run(
                    conn, handle=input_handle, run_id=run_id, scope=scope
                )
                registry.set_input_set(conn, run_id, session_id)
            if dispatch_id is not None:
                registry.require_data_plane(version, "queue dispatch")
                registry.set_dispatch(conn, run_id, dispatch_id)
            run = registry.get_run(conn, run_id) or run
    return run


def _input_handle(workflow: str, normalized: dict[str, Any]) -> str | None:
    """Return the ``inputset:`` handle this run carries, if its adapter uses one.

    Driven off the contract's declared adapter rather than a hardcoded key name,
    so a second inputset-consuming workflow needs no change here.
    """
    schema = load_contract(workflow).schema
    if schema.get("x-input-adapter") != "inputset":
        return None
    value = normalized.get("inputs_md_folder")
    return value if isinstance(value, str) and value.startswith("inputset:") else None


def _mark_failed(run_id: str, error: str) -> None:
    with registry.connect() as conn:
        registry.mark_failed(conn, run_id, error)


def _mark_dispatch_failed(run_id: str, reason: str) -> None:
    with registry.connect() as conn:
        registry.mark_dispatch_failed(conn, run_id, reason)


async def start_run(
    name: str,
    config: dict[str, Any],
    client_context: dict[str, Any] | None,
    idempotency_key: str | None,
    scope: CallerScope,
    *,
    job_client: JobClient | None = None,
    dispatcher: Dispatcher | None = None,
) -> dict[str, Any]:
    context = client_context or {}
    client_slug = require_client(scope, context.get("client_slug"))
    # Checked against the RESOLVED owner, not the caller's union of roles: a caller
    # holding two client roles must not start a run owned by the unentitled one.
    # validate_config's own gate is union-scoped and cannot make this distinction.
    require_entitled(name, client_slug, scope)
    normalized = validate_config(name, config, scope)
    run_id = registry.new_run_id()
    client = job_client or AcaJobClient()
    prepared = await client.prepare(run_id)

    dispatcher = dispatcher or build_dispatcher(client)
    queued = isinstance(dispatcher, QueueDispatcher)
    dispatch_id = new_dispatch_id() if queued else None

    # A workflow whose input adapter is `inputset` receives its evidence as a
    # handle, never as a server-side path. The contract's own pattern already
    # rejects a raw path; this is what turns the accepted handle into an
    # ownership-checked attachment.
    input_handle = _input_handle(name, normalized)

    run = await asyncio.to_thread(
        _persist_start,
        run_id=run_id,
        name=name,
        normalized=normalized,
        client_slug=client_slug,
        template=prepared.template,
        image_tag=prepared.image_tag,
        idempotency_key=idempotency_key,
        dispatch_id=dispatch_id,
        input_handle=input_handle,
        scope=scope,
    )
    if run.run_id != run_id:
        return {**run.public(), "idempotent_replay": True}

    # Enqueue only AFTER the row is committed. The reverse order would let a
    # runner receive a message for a run that does not exist yet.
    try:
        await dispatcher.dispatch(
            run_id=run_id, dispatch_id=dispatch_id or "", template=prepared.template
        )
    except JobStartError as exc:
        await asyncio.to_thread(_mark_failed, run_id, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is a dispatch failure
        # The row is deliberately NOT deleted or failed-terminal: a committed
        # run that was never dispatched is recoverable by an operator retry,
        # whereas a deleted row is a run the client was told about and can
        # never be told anything more about. Re-enqueueing is safe because the
        # dispatch id makes a duplicate message a no-op at claim time.
        await asyncio.to_thread(_mark_dispatch_failed, run_id, str(exc))
        raise DispatchError(f"run {run_id} was created but not dispatched: {exc}") from exc
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
    dispatcher: Dispatcher | None = None,
) -> dict[str, Any]:
    client = job_client or AcaJobClient()
    dispatcher = dispatcher or build_dispatcher(client)
    queued = isinstance(dispatcher, QueueDispatcher)
    # A resume is a NEW dispatch of an existing run: same run id, same thread,
    # same workspace, fresh dispatch id. Minting a new one is what lets the
    # claim reject a redelivery of the *original* start message, which would
    # otherwise be indistinguishable from this resume.
    dispatch_id = new_dispatch_id() if queued else None

    def _request() -> tuple[registry.Run, dict[str, Any]]:
        with registry.connect() as conn:
            run = _require_run_scope(registry.get_run(conn, run_id), scope)
            # The stored template is only needed by the ARM lane; a queue-lane
            # resume resolves everything from the row, so an event-dispatched
            # run legitimately has no template to replay.
            if not queued and not run.job_template_json:
                raise registry.RegistryError(f"run {run_id} has no stored job template")
            resumed = registry.request_resume(conn, run_id, resume_payload)
            if dispatch_id is not None:
                registry.set_dispatch(conn, run_id, dispatch_id)
            return resumed, run.job_template_json or {}

    resumed, template = await asyncio.to_thread(_request)
    try:
        await dispatcher.dispatch(
            run_id=run_id, dispatch_id=dispatch_id or "", template=template
        )
    except JobStartError as exc:
        await asyncio.to_thread(_mark_failed, run_id, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is a dispatch failure
        # The run stays `queued`: it is genuinely awaiting a runner, and an
        # operator re-dispatch is safe because the dispatch id de-duplicates.
        await asyncio.to_thread(_mark_dispatch_failed, run_id, str(exc))
        raise DispatchError(f"run {run_id} was resumed but not dispatched: {exc}") from exc
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
