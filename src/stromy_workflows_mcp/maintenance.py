"""Retention for the tables this facade owns: upload sessions (ORG-PLAN-164 WS5).

The runner's retention pass deletes run workspaces, checkpoint threads and registry
rows. It deliberately does not touch ``input_sessions`` / ``input_files``, because
those belong to *this* deployable's migration chain (the 2026-08-06 ownership rule)
— a runner reaching into another service's tables is exactly the coupling that
produced the ORG-PLAN-155/164 schema fork. So this is the other half, and it runs
here.

**Reference-aware, and the safety half uses only our own data.** An expired session
nobody claimed is garbage. An *attached* one is a run's provenance — which documents
produced a client's report — and because the core cannot declare a foreign key to a
table it does not own, nothing in the database would stop this pass from deleting it.

So the predicate is in two parts, deliberately unequal:

* **``status <> 'attached'`` is the safety gate**, and it reads a column this
  application owns. ``attach_to_run`` sets that status in the same transaction that
  binds the session to a run, so a non-attached session cannot be referenced.
* **The ``runs`` lookup is reclamation, not safety.** Once run retention deletes the
  run, its attached session is orphaned and would otherwise be immortal. Reading
  ``runs.input_set_id`` collects it. Delete that clause and nothing becomes unsafe —
  orphaned session rows simply accumulate. Keeping the cross-boundary read on the
  *optimisation* side rather than the safety side is what stops this from becoming
  the kind of coupling the ownership rule exists to prevent.

**Rows, not bytes.** Staged upload objects are content-addressed and can be
referenced by more than one session, so no single session may delete them. Azure
lifecycle policy owns them: ``expire-unfinalized-staging`` in
``terraform/workflow-data-plane.tf`` deletes staging blobs two days after creation,
which is well past the one-hour session TTL.

Operator-run, like the migration chain, and for the same reason: a server that
prunes on its own schedule prunes during a deploy.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict, dataclass

from workflow_runtime_core.registry import DbConnection

from .config import settings
from .registry import RegistryError, connect

logger = logging.getLogger(__name__)

#: Grace period beyond ``expires_at`` before an unclaimed session is collected.
#: Not zero: a browser can finalize a session moments after it lapses, and deleting
#: the row out from under that request turns a recoverable "your link expired" into
#: an unexplainable 500.
DEFAULT_GRACE_HOURS = 24


@dataclass
class SessionSweep:
    """Counts only. A client's filenames are their content and never logged."""

    sessions_deleted: int = 0
    files_deleted: int = 0
    retained_attached: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def expire_sessions(conn: DbConnection, *, grace_hours: int = DEFAULT_GRACE_HOURS) -> SessionSweep:
    """Delete lapsed, unreferenced upload sessions. ``input_files`` cascades."""
    if grace_hours < 0:
        raise RegistryError("grace_hours cannot be negative")

    with conn.cursor() as cur:
        # Counted before the delete, because the cascade makes them uncountable
        # after and "sessions_deleted: 12, files_deleted: 0" reads as a bug.
        cur.execute(
            """
            SELECT count(*) AS files
              FROM input_files f
             WHERE f.session_id IN (
                   SELECT s.session_id FROM input_sessions s
                    WHERE s.expires_at < now() - make_interval(hours => %(grace)s)
                      AND (
                            s.status <> 'attached'
                            OR NOT EXISTS (
                                 SELECT 1 FROM runs r
                                  WHERE r.input_set_id = s.session_id
                               )
                          )
                 )
            """,
            {"grace": grace_hours},
        )
        files = int((cur.fetchone() or {}).get("files") or 0)

        cur.execute(
            """
            DELETE FROM input_sessions s
             WHERE s.expires_at < now() - make_interval(hours => %(grace)s)
               AND (
                     s.status <> 'attached'
                     OR NOT EXISTS (
                          SELECT 1 FROM runs r WHERE r.input_set_id = s.session_id
                        )
                   )
            """,
            {"grace": grace_hours},
        )
        deleted = cur.rowcount

        # Reported rather than merely skipped: a growing count here is the signal
        # that run retention is not keeping up, not that this pass is broken.
        cur.execute(
            """
            SELECT count(*) AS held FROM input_sessions s
             WHERE s.expires_at < now() - make_interval(hours => %(grace)s)
               AND s.status = 'attached'
               AND EXISTS (SELECT 1 FROM runs r WHERE r.input_set_id = s.session_id)
            """,
            {"grace": grace_hours},
        )
        held = int((cur.fetchone() or {}).get("held") or 0)

    return SessionSweep(
        sessions_deleted=deleted, files_deleted=files, retained_attached=held
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stromy_workflows_mcp.maintenance",
        description="Delete lapsed, unreferenced upload sessions.",
    )
    parser.add_argument("--grace-hours", type=int, default=DEFAULT_GRACE_HOURS)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s"
    )
    if not settings.stromy_pg_dsn:
        logger.error("STROMY_PG_DSN is unset; nothing to sweep")
        return 2
    try:
        with connect() as conn:
            sweep = expire_sessions(conn, grace_hours=args.grace_hours)
    except RegistryError as exc:
        logger.error("%s", exc)
        return 1
    print(json.dumps(sweep.as_dict(), indent=2))  # noqa: T201 - this IS the output
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
