"""Workflow facade use cases, independent of the FastMCP transport wrappers."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

from . import input_sessions, registry
from .aca import AcaJobClient, JobStartError, PreparedJob
from .blobs import AzureStagedReader, output_container, storage_account
from .config import settings
from .contracts import CallerRole, Contract, load_contract
from .dispatch import (
    Dispatcher,
    DispatchError,
    QueueDispatcher,
    build_dispatcher,
    new_dispatch_id,
)
from .entitlements import require_entitled, require_visible, visible_workflows
from .scoping import CallerScope, require_client
from .uploads import SESSION_TTL_SECONDS, DeclaredFile


class JobClient(Protocol):
    async def prepare(self, run_id: str) -> PreparedJob: ...

    async def start(self, template: dict[str, Any]) -> dict[str, Any]: ...


logger = logging.getLogger(__name__)

#: Download URLs are minted per authorized read and kept short — a client fetches
#: results interactively, and a longer-lived URL is an unauthenticated capability
#: sitting in whatever transcript it was returned in.
DOWNLOAD_URL_TTL_SECONDS = int(os.environ.get("WORKFLOW_DOWNLOAD_URL_TTL_SECONDS", "900"))


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


def validate_config(name: str, config: dict[str, Any], scope: CallerScope) -> dict[str, Any]:
    """Caller-facing normalized config: provider-locked keys withheld."""
    contract, effective = _validated(name, config, scope)
    return contract.project(effective, _role(scope))


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


HANDLE_PATTERN = "^inputset:[0-9a-f-]+$"


def _input_handle(workflow: str, normalized: dict[str, Any]) -> str | None:
    """Return the ``inputset:`` handle this run carries, if its adapter uses one.

    The key NAME is discovered from the schema — the property whose pattern is
    the handle grammar — rather than hardcoded. A hardcoded name is what made the
    first version of this brittle: the key was renamed (``inputs_md_folder`` ->
    ``input_set``, since the old name described document loading's *derived*
    output rather than a client's raw upload) and a name-matching lookup would
    have gone silently None, dropping the attachment with no error anywhere.
    """
    schema = load_contract(workflow).schema
    if schema.get("x-input-adapter") != "inputset":
        return None
    properties = schema.get("properties") or {}
    keys = [
        name
        for name, spec in properties.items()
        if isinstance(spec, dict) and spec.get("pattern") == HANDLE_PATTERN
    ]
    if len(keys) != 1:
        # Zero means the contract declares the adapter but offers no way to pass
        # a handle; more than one means the run is ambiguous. Both are contract
        # bugs, and both must be loud rather than silently unattached.
        raise registry.RegistryError(
            f"{workflow} declares the inputset adapter but has {len(keys)} "
            "handle-shaped config keys; expected exactly one"
        )
    value = normalized.get(keys[0])
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
    # The INTERNAL shape: the runner needs the provider pins, so this must not go
    # through the caller-facing projection.
    _, normalized = _validated(name, config, scope)
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


def _public_base_url() -> str:
    """Origin the browser upload page is reachable at.

    Falls back to the OAuth base URL because that is already the facade's own
    externally-resolvable origin — the one value guaranteed correct wherever
    this is deployed.
    """
    explicit = os.environ.get("WORKFLOW_PUBLIC_BASE_URL", "").strip()
    return (explicit or settings.oauth_base_url).rstrip("/")


def create_input_session(
    files: list[dict[str, Any]],
    client_context: dict[str, Any] | None,
    scope: CallerScope,
) -> dict[str, Any]:
    """Open an upload session and return a one-time browser upload link.

    The link — not a set of SAS URLs — is what comes back to the caller. An
    agent has no filesystem the client's documents live on; a human does. So the
    capability travels to a browser the person controls, and the bytes go
    straight from that browser to storage without transiting this facade.
    """
    context = client_context or {}
    client_slug = require_client(scope, context.get("client_slug"))
    declared = [
        DeclaredFile(
            name=str(item.get("name", "")),
            size_bytes=int(item.get("size_bytes") or 0),
            media_type=item.get("media_type"),
        )
        for item in files
    ]
    with registry.connect() as conn:
        registry.require_data_plane(registry.schema_version(conn), "input sessions")
        session, token, _accepted = input_sessions.create_session(
            conn, client_slug=client_slug, files=declared
        )
    payload = session.public()
    # The raw token exists only in this response. It is not stored, not logged,
    # and cannot be re-derived from the row.
    payload["upload_url"] = (
        f"{_public_base_url()}/uploads/{session.session_id}?t={token}"
    )
    payload["expires_in_seconds"] = SESSION_TTL_SECONDS
    return payload


def get_input_session(handle: str, scope: CallerScope) -> dict[str, Any]:
    """Report one caller-owned session's progress. Never returns the token."""
    session_id = input_sessions.parse_handle(handle)
    with registry.connect() as conn:
        session = input_sessions.get_session(conn, session_id, scope)
    return session.public()


def finalize_input_session(
    handle: str, scope: CallerScope, *, fetch_bytes: Any = None
) -> dict[str, Any]:
    """Verify every uploaded object and close the session.

    Idempotent, so the browser page and the agent can both call it without
    racing to a wrong answer.
    """
    session_id = input_sessions.parse_handle(handle)
    reader = fetch_bytes or AzureStagedReader()
    with registry.connect() as conn:
        session = input_sessions.finalize(conn, session_id, scope, fetch_bytes=reader)
    return session.public()


def get_results(run_id: str, scope: CallerScope) -> dict[str, Any]:
    """Return a run's outcome, minting FRESH download URLs for its artifacts.

    The registry stores URL-free descriptors, so every download capability is
    created here — after ``_require_run_scope`` has confirmed this caller owns the
    run — and expires shortly after. That is the point of the split: a URL stored
    at publication time would either expire (making a completed run unusable) or
    be long-lived enough to be a standing unauthenticated capability sitting in
    whatever transcript it was returned in.
    """
    with registry.connect() as conn:
        run = _require_run_scope(registry.get_run(conn, run_id), scope)
    artifacts = dict(run.artifacts_json or {})
    published = artifacts.get("published")
    if isinstance(published, list):
        artifacts["published"] = [_with_download_url(item) for item in published]
    return {
        "run_id": run.run_id,
        "status": run.status,
        "artifacts": artifacts,
        "error": run.error,
    }


def _with_download_url(descriptor: Any) -> Any:
    """Attach a fresh, short-lived read URL to one stored descriptor.

    A descriptor that cannot be minted is returned WITHOUT a URL rather than
    raising: one unreachable artifact must not make the whole result unreadable,
    and the client can still see the artifact exists, its digest, and its size.
    """
    if not isinstance(descriptor, dict):
        return descriptor
    blob_key = descriptor.get("blob_key")
    if not isinstance(blob_key, str) or not blob_key:
        return descriptor
    enriched = dict(descriptor)
    try:
        from stromy_asset_transport.publication import mint_download_url

        enriched["download_url"] = mint_download_url(
            blob_key=blob_key,
            account=storage_account(),
            container=descriptor.get("container") or output_container(),
            ttl_seconds=DOWNLOAD_URL_TTL_SECONDS,
        )
        enriched["download_url_ttl_seconds"] = DOWNLOAD_URL_TTL_SECONDS
    except Exception as exc:  # noqa: BLE001 - one bad artifact must not sink the read
        logger.error("could not mint a download URL for %s: %s", blob_key, exc)
    return enriched
