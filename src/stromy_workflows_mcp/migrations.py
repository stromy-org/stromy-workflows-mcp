"""The facade's own migration chain — the upload tables it owns.

Two chains share one database and one ledger, and the split is the 2026-08-06
ownership rule: anything the shared lifecycle reads or writes changes only via
``workflow-runtime-core``'s chain; anything ONE application owns lives in that
application's namespaced chain. ``input_sessions``/``input_files`` are ours —
nothing in the core reads them, and their shape is a property of how this facade
accepts uploads.

Getting here mattered. Before this module the DDL lived in *Stromy's* migration,
which meant the runner created tables it only reads while the facade that owns
them shipped no schema at all. Two consequences, both bad: a facade deployed
against a fresh registry had no upload tables until an unrelated service migrated,
and a change to OUR table shape had to be made in someone else's repo — the exact
arrangement that produced the ORG-PLAN-155/164 schema fork.

**Applied by an operator, never by the running server.** Same rule as the core
chain, for the same reason: a server that migrates on boot can move the schema out
from under its sibling mid-deploy. ``python -m stromy_workflows_mcp.migrations``
applies it; the readiness path *verifies* and refuses to serve if it is absent.

The ledger records a checksum per applied version and verifies it fail-closed
(ORG-191), so a build whose migration 1 differs from the one the database actually
ran refuses to start rather than reading a shape it does not have.
"""

from __future__ import annotations

import argparse
import logging
import sys

from workflow_runtime_core.auth import resolve_application_role, resolve_owner_role
from workflow_runtime_core.grants import GrantManifest, reconcile
from workflow_runtime_core.migrations import Migration, apply_app_migrations, read_app_version
from workflow_runtime_core.registry import DbConnection

from .config import settings
from .registry import RegistryError, connect

logger = logging.getLogger(__name__)

#: Ledger namespace for this application's chain. Must not collide with ``core``
#: (``apply_app_migrations`` refuses that name outright) and must never change
#: once applied anywhere — the ledger keys on it, so a rename reads as "this
#: chain was never applied" and re-runs migration 1 against existing tables.
NAMESPACE = "stromy_workflows_mcp"

#: Durable inbound upload sessions (ORG-PLAN-164 WS3).
#:
#: DURABLE, not in-memory. The facade scales to zero, so the in-memory token
#: ledger the asset-broker upload page uses is simply not a valid session store
#: here: a client would create a session on one replica, upload against another,
#: and finalize against a third.
#:
#: The page capability is stored as a HASH. It is a bearer credential handed to a
#: browser; storing it verbatim would make a registry dump a working set of upload
#: capabilities for every open session.
_UPLOAD_SESSIONS = """
CREATE TABLE IF NOT EXISTS input_sessions (
    session_id            UUID        PRIMARY KEY,
    client_slug           TEXT        NOT NULL,
    capability_token_hash TEXT        NOT NULL,
    status                TEXT        NOT NULL CHECK (status IN (
                              'created','uploading','finalized','attached','expired')),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at            TIMESTAMPTZ NOT NULL,
    finalized_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS input_sessions_client_idx  ON input_sessions (client_slug);
CREATE INDEX IF NOT EXISTS input_sessions_expires_idx ON input_sessions (expires_at);

CREATE TABLE IF NOT EXISTS input_files (
    file_id          UUID        PRIMARY KEY,
    session_id       UUID        NOT NULL REFERENCES input_sessions (session_id)
                                 ON DELETE CASCADE,
    display_name     TEXT        NOT NULL,
    staging_blob_key TEXT        NOT NULL,
    sha256           TEXT,
    media_type       TEXT,
    size_bytes       BIGINT,
    ordinal          INTEGER     NOT NULL,
    status           TEXT        NOT NULL CHECK (status IN (
                         'declared','uploaded','verified','rejected')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One file per position in a session, so a retried declaration replaces rather
-- than duplicates: two rows at ordinal 3 would rehydrate into one workspace slot
-- and the loser would be invisible.
CREATE UNIQUE INDEX IF NOT EXISTS input_files_session_ordinal_uniq
    ON input_files (session_id, ordinal);
"""

MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="upload_sessions", sql=_UPLOAD_SESSIONS),
)

LATEST_VERSION = MIGRATIONS[-1].version

#: What the application role may do to THIS chain's objects (ORG-PLAN-226).
#:
#: Only this chain can classify its own tables, which is why the manifest lives
#: here rather than in the estate's SQL: `input_sessions` and `input_files` hold
#: operational rows the facade creates and mutates, and nothing here is a
#: migration ledger — this chain's version is recorded in the shared governed
#: `schema_migrations`, which the core chain already protects.
#:
#: No blanket grant, deliberately. A table a future migration adds is absent
#: from this tuple and therefore INACCESSIBLE to the application until someone
#: classifies it — loud, and in a test, rather than silently swept in.
GRANTS = GrantManifest(
    name=NAMESPACE,
    operational_tables=("input_sessions", "input_files"),
)


def apply(conn: DbConnection, *, owner_role: str | None = None) -> int:
    """Apply this application's chain. Idempotent; returns the version reached.

    ``owner_role`` elevates before anything is read, so the tables end up owned
    by that role rather than by whichever login ran the migration — and so a
    principal holding only DML fails loudly instead of finding the chain already
    current and exiting 0.
    """
    return apply_app_migrations(conn, NAMESPACE, MIGRATIONS, owner_role=owner_role)


def live_version(conn: DbConnection) -> int | None:
    """Applied version of this chain, or ``None`` if it was never applied."""
    return read_app_version(conn, NAMESPACE)


def require_applied(conn: DbConnection) -> int:
    """Assert the upload chain is applied, or raise.

    Called from the readiness path. A facade serving with no upload tables accepts
    ``create_input_session`` calls and fails every one of them at the first write,
    which reads to a client as "uploads are broken" rather than "this deployment
    was never migrated".
    """
    version = live_version(conn)
    if version is None or version < LATEST_VERSION:
        raise RegistryError(
            f"the {NAMESPACE} migration chain is at "
            f"{'(none)' if version is None else f'v{version}'}, but this build "
            f"needs v{LATEST_VERSION}. Run "
            "`python -m stromy_workflows_mcp.migrations` against the registry "
            "before starting the server; applications never migrate themselves."
        )
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m stromy_workflows_mcp.migrations",
        description="Apply this service's upload-session migration chain.",
    )
    # The same generic contract `wrc migrate` carries, and generic for the same
    # reason: this repo must not encode the estate's role names any more than
    # the shared library does. Both default to their environment variables, and
    # both being unset means "this deployment does not separate ownership" —
    # which is what every deployment did before ORG-PLAN-226.
    parser.add_argument(
        "--owner-role",
        default=None,
        help="Role to SET ROLE before migrating (defaults to $WRC_PG_OWNER_ROLE).",
    )
    parser.add_argument(
        "--application-role",
        default=None,
        help=(
            "Role to reconcile this chain's grants for after migrating "
            "(defaults to $WRC_PG_APPLICATION_ROLE)."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    if not settings.stromy_pg_dsn:
        logger.error("STROMY_PG_DSN is unset; nothing to migrate")
        return 2

    owner = resolve_owner_role(args.owner_role)
    application = resolve_application_role(args.application_role)

    with connect() as conn:
        reached = apply(conn, owner_role=owner)
        granted: list[str] = []
        if application:
            # Same connection and same elevation as the migration: the grants
            # must be made by the owner, and a second connection would have lost
            # the SET LOCAL ROLE.
            granted = reconcile(conn, GRANTS, application_role=application)

    logger.info("%s chain applied through v%d", NAMESPACE, reached)
    if granted:
        logger.info("reconciled %d grant(s) for %s", len(granted), application)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
