"""DML-only client for the Stromy-owned workflow registry schema."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

from .config import settings

SUPPORTED_SCHEMA_MIN = 1
SUPPORTED_SCHEMA_MAX = 1
DbConnection = psycopg.Connection[dict[str, Any]]


class RegistryError(RuntimeError):
    pass


class SchemaVersionMismatch(RegistryError):
    pass


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

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Run:
        return cls(**{**row, "run_id": str(row["run_id"]), "thread_id": str(row["thread_id"])})

    def public(self) -> dict[str, Any]:
        return {
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
