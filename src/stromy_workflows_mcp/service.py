"""Workflow facade use cases, independent of the FastMCP transport wrappers."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

from stromy_byok import (
    DEFAULT_TTL_SECONDS,
    AuditAction,
    AuditEvent,
    GrantAction,
    NullCredentialStore,
    Subject,
    SubjectKind,
    UnknownCredentialError,
    mint_grant,
)

from . import credentials, input_sessions, registry
from .aca import AcaJobClient, JobStartError, PreparedJob
from .blobs import AzureStagedReader, AzureStagedWriter, output_container, storage_account
from .config import settings
from .contracts import CallerRole, Contract, ContractError, load_contract
from .dispatch import (
    Dispatcher,
    DispatchError,
    QueueDispatcher,
    build_dispatcher,
    new_dispatch_id,
)
from .entitlements import (
    credential_policy,
    require_entitled,
    require_visible,
    visible_workflows,
)
from .scoping import CallerScope, require_client
from .uploads import SESSION_TTL_SECONDS, DeclaredFile, UploadRejected


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


#: Event kind the runner's retention pass writes before it destroys anything. Its
#: presence on a run means the workspace a retry would inherit is gone.
_RETENTION_MARK = "retention_started"


def _persist_retry(
    *,
    parent_id: str,
    new_run_id: str,
    template: dict[str, Any],
    image_tag: str | None,
    dispatch_id: str | None,
) -> registry.Run:
    with registry.connect() as conn:
        registry.require_data_plane(registry.schema_version(conn), "retry")
        attempt = registry.create_retry(
            conn,
            run_id=parent_id,
            new_run_id=new_run_id,
            job_template=template,
            image_tag=image_tag,
        )
        if dispatch_id is not None:
            registry.set_dispatch(conn, attempt.run_id, dispatch_id)
            attempt = registry.get_run(conn, attempt.run_id) or attempt
    return attempt


def _refuse_if_workspace_was_reaped(conn: registry.DbConnection, run_id: str) -> None:
    """Refuse a retry whose workspace retention already deleted.

    The facade has no mount — the durable share is attached to the runner job, and
    giving this service a share credential to duplicate a check the runner must make
    anyway would widen its blast radius for nothing. But it can see the one *common*
    reason a workspace disappears, because retention marks the run before it deletes
    anything. Catching that here means the client is told "no" instead of getting a
    new run id that fails minutes later.

    Every other cause of a missing folder is caught by the runner, which refuses to
    ``mkdir`` a retry's workspace rather than silently starting a full recompute.
    """
    if any(event["kind"] == _RETENTION_MARK for event in registry.list_events(conn, run_id)):
        raise registry.RegistryError(
            f"run {run_id} has passed retention: the workspace a retry would reuse "
            "has been deleted. Start a new run instead."
        )


async def retry_run(
    run_id: str,
    scope: CallerScope,
    *,
    job_client: JobClient | None = None,
    dispatcher: Dispatcher | None = None,
) -> dict[str, Any]:
    """Start the next attempt of a failed run: new run, same workspace.

    Takes no ``client_context``, deliberately. Ownership, workflow and configuration
    all come from the parent row, so a retry cannot move a run to another client or
    switch it to a workflow the caller happens to be entitled to. What IS re-checked
    is the caller: entitlement is resolved now, against the original workflow and the
    run's recorded owner, so a client whose entitlement was withdrawn cannot rerun
    the work it used to be allowed to start.

    A fresh job template is rendered rather than the parent's replayed: the template
    embeds the run id, and this attempt has a new one.
    """
    def _authorize() -> registry.Run:
        with registry.connect() as conn:
            parent = _require_run_scope(registry.get_run(conn, run_id), scope)
            require_entitled(parent.workflow, parent.client_slug or "", scope)
            _refuse_if_workspace_was_reaped(conn, run_id)
            return parent

    parent = await asyncio.to_thread(_authorize)

    client = job_client or AcaJobClient()
    attempt_id = registry.new_run_id()
    prepared = await client.prepare(attempt_id)

    dispatcher = dispatcher or build_dispatcher(client)
    dispatch_id = new_dispatch_id() if isinstance(dispatcher, QueueDispatcher) else None

    attempt = await asyncio.to_thread(
        _persist_retry,
        parent_id=parent.run_id,
        new_run_id=attempt_id,
        template=prepared.template,
        image_tag=prepared.image_tag,
        dispatch_id=dispatch_id,
    )

    try:
        await dispatcher.dispatch(
            run_id=attempt.run_id, dispatch_id=dispatch_id or "", template=prepared.template
        )
    except JobStartError as exc:
        await asyncio.to_thread(_mark_failed, attempt.run_id, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - any transport failure is a dispatch failure
        # Same reasoning as ``start_run``: the row stays. An attempt that exists but
        # was never dispatched is recoverable by re-enqueueing, and the dispatch id
        # makes the duplicate a no-op at claim time. Deleting it would also free the
        # workspace's single live-attempt slot, letting a second retry start against
        # a workspace the first one may yet be handed.
        await asyncio.to_thread(_mark_dispatch_failed, attempt.run_id, str(exc))
        raise DispatchError(
            f"retry {attempt.run_id} was created but not dispatched: {exc}"
        ) from exc
    # Exactly the same projection every other run-returning call uses. The lineage
    # is already in it, under ``attempt`` — repeating ``retry_of`` at the top level
    # would give one fact two homes, which is how the two diverge later.
    return attempt.public()


def _reusable_config(run: registry.Run, role: CallerRole) -> dict[str, Any] | None:
    """The run's stored config, filtered to what this caller may SEE.

    ``config_json`` is the FULL effective config — tier-3 pins included, plus
    internal markers like ``_resume`` written by ``request_resume`` — so it can
    never be returned raw. ``Contract.reusable`` drops undeclared keys first and
    then applies the same role filter ``validate_config`` uses for its echo, so
    the result is resubmittable as-is — the "same as last time" seed for a
    repeat run.

    ``None`` (key omitted from the response) when the workflow's contract no
    longer loads: a run outlives contract renames, and status must keep working
    for it.
    """
    try:
        contract = load_contract(run.workflow)
    except ContractError:
        return None
    return contract.reusable(run.config_json, role)


def run_status(run_id: str, scope: CallerScope) -> dict[str, Any]:
    with registry.connect() as conn:
        run = _require_run_scope(registry.get_run(conn, run_id), scope)
    payload = run.public()
    config = _reusable_config(run, _role(scope))
    if config is not None:
        payload["config"] = config
    return payload


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


def _declared_file(item: dict[str, Any]) -> DeclaredFile:
    content = item.get("content")
    if content is not None and not isinstance(content, str):
        raise UploadRejected(
            "file 'content' must be a UTF-8 string when present", code="inline_not_text"
        )
    return DeclaredFile(
        name=str(item.get("name", "")),
        size_bytes=int(item.get("size_bytes") or 0),
        media_type=item.get("media_type"),
        content=content,
    )


def create_input_session(
    files: list[dict[str, Any]],
    client_context: dict[str, Any] | None,
    scope: CallerScope,
    *,
    stage_bytes: Any = None,
) -> dict[str, Any]:
    """Open an input session; supply files by browser upload, inline text, or both.

    A *declared* file returns inside a one-time browser upload link — not a set
    of SAS URLs — because those documents live on a person's filesystem, not the
    agent's. The capability travels to a browser the person controls, and the
    bytes go straight from that browser to storage without transiting this
    facade.

    An *inline* file carries its text in the call itself, for the opposite case:
    content the agent already holds because it was drafted and reviewed in the
    conversation. Those bytes are staged server-side immediately, and if every
    file is inline no upload link is returned at all — nothing is left for a
    browser to do, and an unused write capability should not exist.
    """
    context = client_context or {}
    client_slug = require_client(scope, context.get("client_slug"))
    declared = [_declared_file(item) for item in files]
    writer = stage_bytes
    if writer is None and any(item.content is not None for item in declared):
        writer = AzureStagedWriter()
    with registry.connect() as conn:
        registry.require_data_plane(registry.schema_version(conn), "input sessions")
        session, token, accepted = input_sessions.create_session(
            conn, client_slug=client_slug, files=declared, stage_bytes=writer
        )
    payload = session.public()
    if any(item.content_bytes is None for item in accepted):
        # The raw token exists only in this response. It is not stored, not
        # logged, and cannot be re-derived from the row.
        payload["upload_url"] = (
            f"{_public_base_url()}/uploads/{session.session_id}?t={token}"
        )
        payload["expires_in_seconds"] = SESSION_TTL_SECONDS
    return payload


def _audit(event: AuditEvent) -> None:
    """Structured, already-redacted lifecycle record. Never carries a value."""
    logger.info("byok %s", event.action.value, extra={"byok": event.to_dict()})


def create_credential_registration_link(
    workflow: str,
    credential_id: str,
    client_context: dict[str, Any] | None,
    scope: CallerScope,
    action: str = GrantAction.REGISTER_OR_ROTATE.value,
) -> dict[str, Any]:
    """Mint a single-use link for a client to register their own provider key.

    Four checks, in this order, and the order is the point:

    1. **visibility** — an unentitled caller gets "unknown workflow", identical
       to a nonexistent one, so link-minting cannot be used to enumerate the
       catalog by diffing errors (the same contract ``entitlements`` keeps);
    2. **acting-for** — the run owner is resolved from verified roles.
       An operator must name a client explicitly; they never imply one;
    3. **entitlement** — the *resolved owner* must be entitled, not merely some
       role the caller holds, so a caller entitled via ``a`` cannot register a
       key that will be spent on runs owned by ``b``;
    4. **requirement** — the credential must be one this workflow actually
       declares. Without this, a caller could register a key against any
       catalogue id under cover of a workflow that never uses it.

    Everything that scopes the resulting capability is baked into the grant and
    re-read from nothing: subject, service, credential id and action all come
    from here, never from the page's form.
    """
    context = client_context or {}
    require_visible(workflow, scope)
    client_slug = require_client(scope, context.get("client_slug"))
    require_entitled(workflow, client_slug, scope)

    try:
        grant_action = GrantAction(action)
    except ValueError:
        raise ValueError(
            f"unknown action {action!r} (expected one of "
            f"{', '.join(sorted(item.value for item in GrantAction))})"
        ) from None

    contract = load_contract(workflow)
    requirements = contract.requirements
    if not requirements.declared:
        # Fail closed. An undeclared block means nobody has stated what this
        # workflow spends, so "is this credential one of them?" has no answer —
        # and minting anyway would let a client register a key against a
        # workflow that will never use it, which reads as connected forever.
        raise ValueError(
            f"workflow {workflow!r} declares no credential requirements yet, so no "
            "credential can be registered against it"
        )
    declared = set(requirements.credentials) | set(requirements.resolved_credentials)
    if credential_id not in declared:
        raise ValueError(
            f"workflow {workflow!r} does not use credential {credential_id!r} "
            f"(it declares: {', '.join(sorted(declared)) or 'none'})"
        )
    # Catalogue membership is checked separately and AFTER the contract, so a
    # caller can never use the difference between the two errors to enumerate
    # the catalogue: they must already have named a credential this workflow
    # declares to reach here.
    spec = credentials.CATALOGUE.get(credential_id)

    subject = Subject(SubjectKind.CLIENT_SLUG, client_slug)
    if scope.unrestricted:
        _audit(
            AuditEvent(
                action=AuditAction.OPERATOR_ACTING_FOR,
                service=credentials.SERVICE,
                outcome="ok",
                subject_hash=subject.hashed,
                subject_kind=subject.kind,
                credential_id=spec.credential_id,
                workflow=workflow,
            )
        )
    grant = mint_grant(
        credentials.GRANTS,
        subject=subject,
        service=credentials.SERVICE,
        credential_id=spec.credential_id,
        action=grant_action,
        issuer=credentials.SERVICE,
        workflow=workflow,
    )
    _audit(
        AuditEvent(
            action=AuditAction.GRANT_MINTED,
            service=credentials.SERVICE,
            outcome="ok",
            subject_hash=subject.hashed,
            subject_kind=subject.kind,
            credential_id=spec.credential_id,
            workflow=workflow,
        )
    )
    return {
        "workflow": workflow,
        "client_slug": client_slug,
        "credential_id": str(spec.credential_id),
        "provider": spec.provider,
        "action": grant_action.value,
        # The token exists only in this response and in the minting process's
        # memory. It is not stored, not logged, and cannot be re-derived.
        "registration_url": f"{_public_base_url()}/keys?token={grant.token}",
        "expires_in_seconds": DEFAULT_TTL_SECONDS,
    }


def credential_status(
    workflow: str,
    client_context: dict[str, Any] | None,
    scope: CallerScope,
) -> dict[str, Any]:
    """Report which of a workflow's credentials this client has registered.

    **Existence and metadata only — never a value.** `CredentialReader.exists`
    answers without reading the secret, and the metadata surfaced here is the
    non-secret tags this service wrote itself.

    The same four gates as minting, for the same reasons: an unentitled caller
    must not learn a workflow exists, and the subject is derived from verified
    roles rather than from anything the caller sends.

    UNPROVISIONED IS NOT UNREGISTERED. With no vault, `NullCredentialStore`
    answers `False` for every subject — which is indistinguishable from a
    truthful "not registered" and would render as *"you have not connected a
    key"* to a client who connected one. The store's provisioning state is
    therefore reported separately, and each credential reads `unavailable`
    rather than `not_registered` when it cannot be known.
    """
    context = client_context or {}
    require_visible(workflow, scope)
    client_slug = require_client(scope, context.get("client_slug"))
    require_entitled(workflow, client_slug, scope)

    contract = load_contract(workflow)
    requirements = contract.requirements
    policy = credential_policy(workflow, client_slug)
    payload: dict[str, Any] = {
        "workflow": workflow,
        "client_slug": client_slug,
        # Whose keys this client's runs spend. On `operator` the client owes
        # nothing — reporting an unregistered credential without this would read
        # as an outstanding action when there is none.
        "credential_policy": policy,
        "declared": requirements.declared,
        "credentials": [],
    }
    if not requirements.declared:
        payload["note"] = (
            "This workflow has not declared its credential requirements yet, so "
            "there is nothing to register against it."
        )
        return payload

    store = credentials.credential_store()
    provisioned = not isinstance(store, NullCredentialStore)
    payload["store_provisioned"] = provisioned
    subject = Subject(SubjectKind.CLIENT_SLUG, client_slug)
    drift: list[str] = []
    entries: list[dict[str, Any]] = []
    for credential_id in sorted(
        set(requirements.credentials) | set(requirements.resolved_credentials)
    ):
        try:
            spec = credentials.CATALOGUE.get(credential_id)
        except UnknownCredentialError:
            # Reported, not raised: the contract and this catalogue drifting apart
            # is exactly what C5's manifest check exists to catch, and a status
            # call that dies on it hides every other credential's real state.
            drift.append(credential_id)
            continue
        entry: dict[str, Any] = {
            "credential_id": str(spec.credential_id),
            "provider": spec.provider,
            "display_name": spec.display_name,
            "signup_url": spec.signup_url,
        }
        if not provisioned:
            entry["status"] = "unavailable"
        else:
            entry["status"] = (
                "registered" if store.exists(spec.credential_id, subject) else "not_registered"
            )
            # `metadata` is on the WRITER protocol, and this facade holds Secrets
            # Officer so it has one — but the reader protocol is what the store
            # is typed as, and a read-only backend legitimately lacks it.
            metadata = getattr(store, "metadata", None)
            raw_tags = metadata(spec.credential_id, subject) if callable(metadata) else None
            tags = raw_tags if isinstance(raw_tags, dict) else None
            if tags:
                # Non-secret tags this service wrote. Whitelisted rather than
                # passed through, so a future tag cannot become an accidental
                # disclosure channel on a client-facing call.
                entry["last_rotated_at"] = tags.get("rotated_at")
                entry["validation"] = tags.get("validation")
        entries.append(entry)
    payload["credentials"] = entries
    if drift:
        payload["catalogue_drift"] = drift
    if not provisioned:
        payload["note"] = (
            "The credential store is not provisioned on this server, so "
            "registration state cannot be read. This is not the same as having "
            "no key registered."
        )
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
