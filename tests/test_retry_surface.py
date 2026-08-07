"""The facade's retry surface (ORG-PLAN-164 WS5).

The lineage mechanics live in ``workflow-runtime-core`` and are proved there against
a real Postgres. What is genuinely the facade's — and therefore tested here — is the
authorization it applies before minting an attempt, plus the two things it must not
do: let a caller redirect a retry, or hand back a run id for an attempt that cannot
possibly succeed.

Each property below is a way a client could be harmed:

1. **Entitlement is re-resolved now**, against the original workflow and the run's
   recorded owner. A client whose entitlement was withdrawn must not be able to rerun
   the work it used to be allowed to start.
2. **Nothing about the attempt comes from the caller** — no client context, no
   config, no workflow name — so a retry cannot move a run to another client's slug,
   and a fresh job template is rendered because the template embeds the run id.
3. **A reaped workspace is refused up front.** Retention marks a run before it
   destroys anything, so the facade can say no instead of returning a run id that
   fails minutes later inside a container.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from stromy_workflows_mcp import registry, service
from stromy_workflows_mcp.aca import PreparedJob
from stromy_workflows_mcp.scoping import CallerScope

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
PARENT = "11111111-1111-1111-1111-111111111111"
WORKSPACE = "22222222-2222-2222-2222-222222222222"
ATTEMPT = "33333333-3333-3333-3333-333333333333"

OWNER_SCOPE = CallerScope(frozenset({"dukestrategies"}))
OTHER_SCOPE = CallerScope(frozenset({"someoneelse"}))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _run(**overrides: Any) -> registry.Run:
    row: dict[str, Any] = {
        "run_id": PARENT,
        "workflow": "stakeholder_analysis_workflow",
        "thread_id": PARENT,
        "status": "failed",
        "client_slug": "dukestrategies",
        "config_json": {"depth": 1},
        "image_tag": "sha-abc",
        "job_template_json": {"template": {"containers": []}},
        "created_at": NOW,
        "updated_at": NOW,
        "interrupt_payload": None,
        "error": "node blew up",
        "artifacts_json": None,
        "idempotency_key": None,
        "workspace_id": WORKSPACE,
        "attempt_no": 1,
    }
    row.update(overrides)
    # Built through from_row: the mapper the facade actually uses, so the fixture
    # cannot drift from the real row shape.
    return registry.Run.from_row(row)


class _JobClient:
    """Records which run id the template was rendered for."""

    def __init__(self) -> None:
        self.prepared_for: list[str] = []

    async def prepare(self, run_id: str) -> PreparedJob:
        self.prepared_for.append(run_id)
        return PreparedJob(
            template={"template": {"containers": [{"name": "runner"}]}},
            image_tag="sha-def",
        )

    async def start(self, template: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("the dispatcher is faked; start must not be reached")


class _Dispatcher:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raises = raises

    async def dispatch(self, *, run_id: str, dispatch_id: str, template: dict[str, Any]) -> None:
        self.calls.append({"run_id": run_id, "dispatch_id": dispatch_id, "template": template})
        if self.raises is not None:
            raise self.raises


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Serve one parent run, record the retry, and keep every gate real."""

    def _install(
        *,
        parent: registry.Run,
        events: list[dict[str, Any]] | None = None,
        entitled: bool = True,
        schema: int = 2,
    ) -> dict[str, Any]:
        recorded: dict[str, Any] = {"retries": [], "dispatch_ids": [], "failures": []}

        @contextmanager
        def _connect(*_a: Any, **_k: Any):
            yield object()

        monkeypatch.setattr(registry, "connect", _connect)
        monkeypatch.setattr(registry, "get_run", lambda _conn, _run_id: parent)
        monkeypatch.setattr(registry, "schema_version", lambda _conn: schema)
        monkeypatch.setattr(registry, "list_events", lambda _conn, _run_id: events or [])
        monkeypatch.setattr(registry, "new_run_id", lambda: ATTEMPT)

        def _create_retry(_conn: Any, **kwargs: Any) -> registry.Run:
            recorded["retries"].append(kwargs)
            return _run(
                run_id=kwargs["new_run_id"],
                thread_id=kwargs["new_run_id"],
                status="queued",
                error=None,
                attempt_no=2,
                retry_of=PARENT,
                image_tag=kwargs["image_tag"],
                job_template_json=kwargs["job_template"],
            )

        monkeypatch.setattr(registry, "create_retry", _create_retry)
        monkeypatch.setattr(
            registry,
            "set_dispatch",
            lambda _conn, run_id, dispatch_id: recorded["dispatch_ids"].append(
                (run_id, dispatch_id)
            ),
        )
        monkeypatch.setattr(
            service,
            "_mark_dispatch_failed",
            lambda run_id, reason: recorded["failures"].append((run_id, reason)),
        )

        def _require_entitled(name: str, slug: str, _scope: Any) -> None:
            recorded["entitlement_checked"] = (name, slug)
            if not entitled:
                raise PermissionError(f"{slug} is not entitled to {name}")

        monkeypatch.setattr(service, "require_entitled", _require_entitled)
        return recorded

    return _install


# --- 1. authorization ---------------------------------------------------------


async def test_entitlement_is_checked_against_the_original_workflow_and_owner(
    patched: Any,
) -> None:
    recorded = patched(parent=_run())
    await service.retry_run(PARENT, OWNER_SCOPE, job_client=_JobClient(), dispatcher=_Dispatcher())
    assert recorded["entitlement_checked"] == (
        "stakeholder_analysis_workflow",
        "dukestrategies",
    )


async def test_a_withdrawn_entitlement_blocks_the_retry(patched: Any) -> None:
    """The check is re-run at retry time, not inherited from the original start."""
    patched(parent=_run(), entitled=False)
    dispatcher = _Dispatcher()
    with pytest.raises(PermissionError):
        await service.retry_run(
            PARENT, OWNER_SCOPE, job_client=_JobClient(), dispatcher=dispatcher
        )
    assert dispatcher.calls == []


async def test_another_clients_run_cannot_be_retried(patched: Any) -> None:
    patched(parent=_run())
    with pytest.raises(PermissionError, match="outside the caller's client scope"):
        await service.retry_run(
            PARENT, OTHER_SCOPE, job_client=_JobClient(), dispatcher=_Dispatcher()
        )


async def test_a_missing_run_is_not_found_not_a_crash(patched: Any) -> None:
    patched(parent=None)  # type: ignore[arg-type]
    with pytest.raises(registry.RegistryError, match="not found"):
        await service.retry_run(
            PARENT, OWNER_SCOPE, job_client=_JobClient(), dispatcher=_Dispatcher()
        )


# --- 2. the attempt is derived, never supplied --------------------------------


async def test_the_attempt_inherits_everything_and_renders_its_own_template(
    patched: Any,
) -> None:
    recorded = patched(parent=_run())
    client = _JobClient()
    result = await service.retry_run(
        PARENT, OWNER_SCOPE, job_client=client, dispatcher=_Dispatcher()
    )

    # A fresh template for the NEW run id: the rendered template embeds it, so
    # replaying the parent's would point the container at the wrong run.
    assert client.prepared_for == [ATTEMPT]
    call = recorded["retries"][0]
    assert call["run_id"] == PARENT
    assert call["new_run_id"] == ATTEMPT
    assert call["image_tag"] == "sha-def"
    assert call["job_template"] == {"template": {"containers": [{"name": "runner"}]}}
    # No config override reaches the core from this path at all.
    assert "config" not in call

    assert result["run_id"] == ATTEMPT
    # The lineage has exactly one home in the payload, the same one every other
    # run-returning call uses.
    assert result["attempt"] == {"attempt_no": 2, "retry_of": PARENT}
    assert "retry_of" not in result
    assert result["client_slug"] == "dukestrategies"


async def test_the_retry_signature_offers_no_way_to_redirect_a_run() -> None:
    """Structural, on purpose. A ``client_context`` parameter here would be the one
    place a caller could aim a retry at another client's slug, so the guarantee is
    the absence of the parameter rather than a check on its value."""
    import inspect

    params = set(inspect.signature(service.retry_run).parameters)
    assert params == {"run_id", "scope", "job_client", "dispatcher"}


async def test_a_v1_registry_refuses_the_feature_by_name(patched: Any) -> None:
    """Lineage columns do not exist on v1. The named refusal is what keeps this from
    surfacing as a column error inside a background worker."""
    patched(parent=_run(), schema=1)
    with pytest.raises(registry.SchemaFeatureUnavailable, match="retry"):
        await service.retry_run(
            PARENT, OWNER_SCOPE, job_client=_JobClient(), dispatcher=_Dispatcher()
        )


# --- 3. a reaped workspace is refused before a run id is handed out -----------


async def test_a_run_past_retention_cannot_be_retried(patched: Any) -> None:
    recorded = patched(parent=_run(), events=[{"kind": "retention_started", "detail": None}])
    with pytest.raises(registry.RegistryError, match="passed retention"):
        await service.retry_run(
            PARENT, OWNER_SCOPE, job_client=_JobClient(), dispatcher=_Dispatcher()
        )
    assert recorded["retries"] == []


async def test_an_ordinary_event_trail_does_not_block_a_retry(patched: Any) -> None:
    patched(
        parent=_run(),
        events=[{"kind": "created", "detail": None}, {"kind": "failed", "detail": None}],
    )
    result = await service.retry_run(
        PARENT, OWNER_SCOPE, job_client=_JobClient(), dispatcher=_Dispatcher()
    )
    assert result["run_id"] == ATTEMPT


# --- dispatch failure keeps the row ------------------------------------------


async def test_a_dispatch_failure_keeps_the_attempt_row(patched: Any) -> None:
    """Deleting it would also free the workspace's single live-attempt slot, letting
    a second retry start against a workspace the first may yet be handed."""
    recorded = patched(parent=_run())
    with pytest.raises(service.DispatchError, match="created but not dispatched"):
        await service.retry_run(
            PARENT,
            OWNER_SCOPE,
            job_client=_JobClient(),
            dispatcher=_Dispatcher(raises=RuntimeError("queue unreachable")),
        )
    assert recorded["failures"] == [(ATTEMPT, "queue unreachable")]
