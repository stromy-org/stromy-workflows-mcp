"""DML-only client for the Stromy-owned workflow registry schema."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from .config import settings

# The expand/migrate/cutover/contract bridge (ORG-PLAN-164 WS0). Widened to
# accept BOTH schemas *before* Stromy applies the v2 migration, because the
# deploy order is migrate-then-deploy: a facade pinned to [1, 1] would start
# rejecting the database the instant the migration lands, taking the whole
# hosted surface down until its own deploy caught up. v1 support is contracted
# away only after queue cutover and legacy-run disposition.
SUPPORTED_SCHEMA_MIN = 1
SUPPORTED_SCHEMA_MAX = 2

#: Schema version at which the workflow data plane's columns exist. Feature
#: paths that need them refuse v1 with a named compatibility error rather than
#: reading NULLs and pretending the feature works.
DATA_PLANE_SCHEMA = 2

DbConnection = psycopg.Connection[dict[str, Any]]


class RegistryError(RuntimeError):
    pass


class SchemaVersionMismatch(RegistryError):
    pass


class SchemaFeatureUnavailable(RegistryError):
    """A data-plane feature was requested against a pre-v2 registry.

    Distinct from ``SchemaVersionMismatch``: the registry is *servable*, the
    caller simply asked for something the live schema cannot yet record. Named
    so the facade returns a stable compatibility error instead of silently
    writing a half-populated row.
    """


def require_data_plane(version: int, feature: str) -> None:
    if version < DATA_PLANE_SCHEMA:
        raise SchemaFeatureUnavailable(
            f"{feature} requires registry schema v{DATA_PLANE_SCHEMA}; the live "
            f"schema is v{version}. The Stromy migration lands before this "
            "feature is usable."
        )


@dataclass(frozen=True)
class Run:
    run_id: str
    workflow: str
    thread_id: str
    status: str
    client_slug: str | None
    config_json: dict[str, Any]
    image_tag: str | None
    job_template_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
    interrupt_payload: dict[str, Any] | None
    error: str | None
    artifacts_json: dict[str, Any] | None
    idempotency_key: str | None
    # --- schema v2 (workflow data plane, ORG-PLAN-164) -----------------------
    # Every one of these defaults to None so a v1 row maps cleanly. The dataclass
    # is the *union* of both schemas during expansion, never a v2-only shape.
    workspace_id: str | None = None
    retry_of: str | None = None
    attempt_no: int | None = None
    dispatch_id: str | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    progress_json: dict[str, Any] | None = None
    error_json: dict[str, Any] | None = None
    heartbeat_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Run:
        """Build a Run from a ``SELECT *`` row of EITHER schema version.

        Field-filtered rather than splatted. A bare ``cls(**row)`` breaks in both
        directions across the v1/v2 bridge: against v2 it raises on the new
        columns it has never heard of, and against v1 it raises on the ones it
        expects and does not get. Filtering to declared fields makes the same
        code correct on both, which is what lets the facade keep serving while
        the migration lands underneath it.
        """
        declared = {f.name for f in fields(cls)}
        values = {name: value for name, value in row.items() if name in declared}
        for key in ("run_id", "thread_id", "workspace_id", "retry_of", "dispatch_id"):
            if values.get(key) is not None:
                values[key] = str(values[key])
        return cls(**values)

    def public(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "status": self.status,
            "client_slug": self.client_slug,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "interrupt_payload": self.interrupt_payload,
            "error": self.error,
            "artifacts": self.artifacts_json,
        }
        # Omitted entirely on v1 rather than reported as null: an absent key is
        # honest about "this registry cannot tell you", where `"progress": null`
        # reads as "no progress yet".
        if self.attempt_no is not None:
            payload["attempt"] = {"attempt_no": self.attempt_no, "retry_of": self.retry_of}
        if self.progress_json is not None:
            payload["progress"] = self.progress_json
        if self.heartbeat_at is not None:
            payload["heartbeat_at"] = self.heartbeat_at.isoformat()
        if self.error_json is not None:
            payload["failure"] = self.error_json
        return payload


@contextmanager
def connect() -> Iterator[DbConnection]:
    if not settings.stromy_pg_dsn:
        raise RegistryError("STROMY_PG_DSN is unset; the facade has no local fallback")
    try:
        conn = cast(
            DbConnection,
            psycopg.connect(
                settings.stromy_pg_dsn,
                row_factory=dict_row,  # pyright: ignore[reportArgumentType]
            ),
        )
    except psycopg.Error as exc:
        raise RegistryError(f"cannot reach workflow registry: {exc}") from exc
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def schema_version(conn: DbConnection) -> int:
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version FROM schema_meta LIMIT 1")
            row = cur.fetchone()
    except psycopg.Error as exc:
        raise SchemaVersionMismatch(f"registry schema metadata unavailable: {exc}") from exc
    if not row:
        raise SchemaVersionMismatch("registry schema_meta has no version row")
    version = int(row["version"])
    if not (SUPPORTED_SCHEMA_MIN <= version <= SUPPORTED_SCHEMA_MAX):
        raise SchemaVersionMismatch(
            f"registry schema v{version} is outside supported range "
            f"[{SUPPORTED_SCHEMA_MIN}, {SUPPORTED_SCHEMA_MAX}]"
        )
    return version


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
    if idempotency_key:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE idempotency_key = %s", (idempotency_key,))
            existing = cur.fetchone()
        if existing:
            return Run.from_row(existing)
    try:
        # Nested transaction = savepoint. A concurrent insert may win the
        # partial unique index race; rolling back only this savepoint keeps the
        # outer request transaction usable for the winner re-fetch.
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO runs (run_id, workflow, thread_id, status, client_slug,
                                  config_json, image_tag, job_template_json,
                                  idempotency_key)
                VALUES (%s, %s, %s, 'queued', %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    run_id,
                    workflow,
                    run_id,
                    client_slug,
                    json.dumps(config),
                    image_tag,
                    json.dumps(job_template),
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RegistryError("run insert returned no row")
            cur.execute(
                "INSERT INTO run_events (run_id, kind, detail) VALUES (%s, 'created', %s)",
                (run_id, json.dumps({"workflow": workflow, "client_slug": client_slug})),
            )
        return Run.from_row(row)
    except psycopg.errors.UniqueViolation:
        if not idempotency_key:
            raise
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM runs WHERE idempotency_key = %s", (idempotency_key,))
            winner = cur.fetchone()
        if winner:
            return Run.from_row(winner)
        raise


def new_run_id() -> str:
    return str(uuid.uuid4())


def set_dispatch(conn: DbConnection, run_id: str, dispatch_id: str) -> None:
    """Record which dispatch a run belongs to, before the message is enqueued.

    The runner's claim re-checks this value, so a stale message from an earlier
    dispatch of the same run cannot start a second writer.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET dispatch_id = %s, updated_at = now() WHERE run_id = %s",
            (dispatch_id, run_id),
        )


def mark_dispatch_failed(conn: DbConnection, run_id: str, reason: str) -> None:
    """Enqueue failed after the run row was committed.

    The row survives on purpose. A run that exists but was never dispatched is
    recoverable by an operator retry; deleting it would leave a client holding a
    run id that will never mean anything again.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET error_json = %s, updated_at = now() WHERE run_id = %s",
            (
                json.dumps(
                    {
                        "stage": "dispatch",
                        "error_type": "DispatchEnqueueFailed",
                        "message": reason[:2000],
                        "retryable": True,
                    }
                ),
                run_id,
            ),
        )
        cur.execute(
            "INSERT INTO run_events (run_id, kind, detail) VALUES (%s, 'dispatch_failed', %s)",
            (run_id, json.dumps({"reason": reason[:2000]})),
        )


def get_run(conn: DbConnection, run_id: str) -> Run | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return Run.from_row(row) if row else None


def list_runs(
    conn: DbConnection,
    *,
    client_slugs: Sequence[str] | None,
    limit: int = 50,
) -> list[Run]:
    with conn.cursor() as cur:
        if client_slugs is None:
            cur.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT %s", (limit,))
        elif not client_slugs:
            return []
        else:
            cur.execute(
                "SELECT * FROM runs WHERE client_slug = ANY(%s) ORDER BY created_at DESC LIMIT %s",
                (list(client_slugs), limit),
            )
        return [Run.from_row(row) for row in cur.fetchall()]


def request_resume(conn: DbConnection, run_id: str, payload: Any) -> Run:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE run_id = %s FOR UPDATE", (run_id,))
        row = cur.fetchone()
        if not row:
            raise RegistryError(f"run {run_id} not found")
        if row["status"] != "paused":
            raise RegistryError(f"run {run_id} is {row['status']}, not paused")
        config = dict(row["config_json"] or {})
        config["_resume"] = payload
        cur.execute(
            "UPDATE runs SET status='queued', config_json=%s, interrupt_payload=NULL, "
            "updated_at=now() WHERE run_id=%s RETURNING *",
            (json.dumps(config), run_id),
        )
        updated = cur.fetchone()
        cur.execute(
            "INSERT INTO run_events (run_id, kind) VALUES (%s, 'resume_requested')",
            (run_id,),
        )
    if updated is None:
        raise RegistryError("resume update returned no row")
    return Run.from_row(updated)


def cancel_run(conn: DbConnection, run_id: str) -> Run:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET status='cancelled', updated_at=now() "
            "WHERE run_id=%s AND status IN ('queued','paused') RETURNING *",
            (run_id,),
        )
        row = cur.fetchone()
        if not row:
            raise RegistryError(f"run {run_id} cannot be cancelled from its current state")
        cur.execute("INSERT INTO run_events (run_id, kind) VALUES (%s, 'cancelled')", (run_id,))
    return Run.from_row(row)


def mark_failed(conn: DbConnection, run_id: str, error: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE runs SET status='failed', error=%s, updated_at=now() WHERE run_id=%s",
            (error[:8000], run_id),
        )
        cur.execute(
            "INSERT INTO run_events (run_id, kind, detail) VALUES (%s, 'failed', %s)",
            (run_id, json.dumps({"error": error[:2000]})),
        )
