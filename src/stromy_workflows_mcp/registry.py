"""Facade-side adapter over the shared ``workflow-runtime-core`` registry.

This module used to carry its own copy of the Stromy-owned schema's DML —
`INSERT INTO runs`, the idempotency savepoint, the resume `FOR UPDATE`, the
whole lot. Two hand-maintained copies of one schema's statements, in two repos,
kept in step by nothing but discipline. Since ORG-PLAN-155 Phase A there is one
implementation and this file is the thin adapter onto it.

**No SQL below this line**, deliberately — `test_facade_contains_no_schema_ddl`
reads this module's source and asserts it. What remains here is only what is
genuinely facade-shaped:

* ``connect()`` takes no DSN. The facade resolves it from pydantic settings, not
  from the ambient environment the core defaults to.
* ``schema_version()`` keeps its name and its raise-on-out-of-range contract,
  because ``server_hooks`` calls it on the readiness path and the health payload
  is a public response body.
* ``cancel_run()`` keeps the facade's own error wording for the same reason.

The core install here is the BASE package: no LangGraph, no aio-pika. A contract
test in the core asserts that boundary, and the facade's dependency audit
asserts the resulting install.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from workflow_runtime_core import registry as core
from workflow_runtime_core.exceptions import RegistryError, SchemaVersionMismatch
from workflow_runtime_core.models import RunRecord as Run
from workflow_runtime_core.schema import (
    SUPPORTED_SCHEMA_MAX,
    SUPPORTED_SCHEMA_MIN,
    require_compatible_schema,
)

from .config import settings

DbConnection = core.DbConnection

__all__ = [
    "SUPPORTED_SCHEMA_MAX",
    "SUPPORTED_SCHEMA_MIN",
    "DbConnection",
    "RegistryError",
    "Run",
    "SchemaVersionMismatch",
    "cancel_run",
    "connect",
    "create_run",
    "get_run",
    "list_runs",
    "mark_failed",
    "new_run_id",
    "request_resume",
    "schema_version",
]


@contextmanager
def connect() -> Iterator[DbConnection]:
    """Open a registry connection using the facade's configured DSN.

    Deliberately no-arg: the facade's DSN comes from validated settings, and an
    unset one is a misconfiguration to report, never a cue to fall back to a
    local store.
    """
    if not settings.stromy_pg_dsn:
        raise RegistryError("STROMY_PG_DSN is unset; the facade has no local fallback")
    with core.connect(settings.stromy_pg_dsn) as conn:
        yield conn


def schema_version(conn: DbConnection) -> int:
    """Live schema version, raising if it is outside the supported range.

    Called on the readiness path: a facade compiled against a schema it cannot
    read must fail its health check rather than serve.
    """
    return require_compatible_schema(
        conn, minimum=SUPPORTED_SCHEMA_MIN, maximum=SUPPORTED_SCHEMA_MAX
    )


def new_run_id() -> str:
    return core.new_run_id()


def create_run(
    conn: DbConnection,
    *,
    run_id: str,
    workflow: str,
    config: dict[str, Any],
    client_slug: str,
    job_template: dict[str, Any],
    image_tag: str | None,
    idempotency_key: str | None,
) -> Run:
    """Register a queued run under a caller-minted id.

    The id is minted before the row exists because the rendered job template
    embeds it, and that exact template is what a resume replays.
    """
    return core.create_run(
        conn,
        run_id=run_id,
        workflow=workflow,
        config=config,
        client_slug=client_slug,
        job_template=job_template,
        image_tag=image_tag,
        idempotency_key=idempotency_key,
    )


def get_run(conn: DbConnection, run_id: str) -> Run | None:
    return core.get_run(conn, run_id)


def list_runs(
    conn: DbConnection,
    *,
    client_slugs: Sequence[str] | None,
    limit: int = 50,
) -> list[Run]:
    """List runs within the caller's resolved scope.

    ``None`` is the unscoped operator view; an EMPTY sequence is a scope that
    resolved to zero clients and must return nothing — never the operator view.
    """
    return core.list_runs(conn, client_slugs=client_slugs, limit=limit)


def request_resume(conn: DbConnection, run_id: str, payload: Any) -> Run:
    return core.request_resume(conn, run_id, payload)


def cancel_run(conn: DbConnection, run_id: str) -> Run:
    """Cancel a non-terminal run, preserving the facade's error wording."""
    try:
        return core.cancel_run(conn, run_id)
    except RegistryError as exc:
        raise RegistryError(
            f"run {run_id} cannot be cancelled from its current state"
        ) from exc


def mark_failed(conn: DbConnection, run_id: str, error: str) -> None:
    core.mark_failed(conn, run_id, error)
