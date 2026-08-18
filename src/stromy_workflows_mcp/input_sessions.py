"""Durable inbound input sessions (ORG-PLAN-164 WS3).

DML over the Stromy-owned ``input_sessions`` / ``input_files`` tables, plus the
scope checks that make a handle an *authorization*, not merely an identifier.

Why a session record rather than a bare content hash
----------------------------------------------------
A SHA identifies bytes; it does not say who may use them. If the caller-visible
handle were a hash, anyone who learned it could attach another client's evidence
to their own run — and hashes are exactly the sort of value that leaks into
logs, error messages and support threads. So the handle is an opaque
``inputset:<uuid>`` whose row records the owning client, and every lookup is
scoped to the caller.

The shared ``asset-transport`` store stays domain-agnostic and
content-addressed. Tenancy, session lifecycle, and file ordering live here, in
the authorization layer that actually knows about clients.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .registry import DbConnection, RegistryError
from .scoping import CallerScope
from .uploads import (
    SESSION_TTL_SECONDS,
    AcceptedFile,
    DeclaredFile,
    UploadRejected,
    accept_declaration,
    capability_matches,
    hash_capability,
    new_capability_token,
    verify_content,
)

HANDLE_PREFIX = "inputset:"
HANDLE_RE = re.compile(r"^inputset:[0-9a-f-]+$")


class InputSessionError(RegistryError):
    """Session lookup or transition failed."""


@dataclass(frozen=True)
class InputSession:
    session_id: str
    client_slug: str
    status: str
    created_at: datetime
    expires_at: datetime
    finalized_at: datetime | None
    files: list[dict[str, Any]]

    @property
    def handle(self) -> str:
        return f"{HANDLE_PREFIX}{self.session_id}"

    def public(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "session_id": self.session_id,
            "status": self.status,
            "expires_at": self.expires_at.isoformat(),
            "finalized_at": self.finalized_at.isoformat() if self.finalized_at else None,
            # Never the capability token, never a storage key, never a SAS.
            "files": [
                {
                    "ordinal": f["ordinal"],
                    "name": f["display_name"],
                    "status": f["status"],
                    "size_bytes": f["size_bytes"],
                    "media_type": f["media_type"],
                }
                for f in self.files
            ],
        }


def parse_handle(handle: str) -> str:
    """Extract the session id from a caller-supplied handle.

    Grammar-checked before it reaches SQL. A handle arrives in run config, which
    is the most caller-controlled surface the facade has.
    """
    if not isinstance(handle, str) or not HANDLE_RE.fullmatch(handle):
        raise InputSessionError(f"not a valid input-set handle: {handle!r}")
    raw = handle[len(HANDLE_PREFIX) :]
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise InputSessionError("input-set handle is not a UUID") from exc


def create_session(
    conn: DbConnection,
    *,
    client_slug: str,
    files: list[DeclaredFile],
    ttl_seconds: int = SESSION_TTL_SECONDS,
    stage_bytes: Any = None,
) -> tuple[InputSession, str, list[AcceptedFile]]:
    """Create a session. Returns (session, RAW capability token, accepted files).

    The raw token is returned exactly once, to be embedded in the page URL. Only
    its hash is stored, so a registry dump is not a working set of upload
    capabilities.

    Files supplied inline are staged server-side here via
    ``stage_bytes(storage_key, data, media_type)`` and recorded as ``uploaded``;
    declared-only files stay ``declared`` and await the browser page. Both kinds
    pass through the same ``finalize`` verification before the handle is usable.
    """
    session_id = str(uuid.uuid4())
    accepted = accept_declaration(files, session_id=session_id)
    inline = [item for item in accepted if item.content_bytes is not None]
    if inline and stage_bytes is None:
        raise InputSessionError("inline content was supplied but no staging writer is configured")
    token = new_capability_token()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    # Stage BEFORE any row exists: if a blob write fails, no session was
    # created, so there is never a half-staged session to reason about.
    for item in inline:
        stage_bytes(item.storage_name, item.content_bytes, item.media_type)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO input_sessions
                (session_id, client_slug, capability_token_hash, status, expires_at)
            VALUES (%s, %s, %s, 'created', %s)
            """,
            (session_id, client_slug, hash_capability(token), expires_at),
        )
        for item in accepted:
            cur.execute(
                """
                INSERT INTO input_files
                    (file_id, session_id, display_name, staging_blob_key,
                     media_type, size_bytes, ordinal, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    session_id,
                    item.display_name,
                    item.storage_name,
                    item.media_type,
                    item.size_bytes,
                    item.ordinal,
                    "uploaded" if item.content_bytes is not None else "declared",
                ),
            )
    return _load(conn, session_id), token, accepted


def _load(conn: DbConnection, session_id: str) -> InputSession:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM input_sessions WHERE session_id = %s", (session_id,))
        row = cur.fetchone()
        if row is None:
            raise InputSessionError("input session not found")
        cur.execute(
            "SELECT * FROM input_files WHERE session_id = %s ORDER BY ordinal",
            (session_id,),
        )
        files = [dict(item) for item in cur.fetchall()]
    return InputSession(
        session_id=str(row["session_id"]),
        client_slug=row["client_slug"],
        status=row["status"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        finalized_at=row["finalized_at"],
        files=files,
    )


def require_scope(session: InputSession, scope: CallerScope) -> InputSession:
    """A session belongs to exactly one client.

    Deliberately the SAME message whether the session is missing or simply
    someone else's: distinguishing them would let a caller enumerate other
    clients' sessions by diffing errors.
    """
    if not scope.unrestricted and session.client_slug not in scope.client_slugs:
        raise InputSessionError("input session not found")
    return session


def get_session(conn: DbConnection, session_id: str, scope: CallerScope) -> InputSession:
    return require_scope(_load(conn, session_id), scope)


def authorize_capability(
    conn: DbConnection, session_id: str, token: str
) -> InputSession:
    """Authorize an upload-page request by its capability token.

    The page has no Entra identity — it is a browser holding a one-time URL — so
    the token IS the authorization, checked in constant time against the stored
    hash and refused once the session has expired or been finalized.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT capability_token_hash, status, expires_at FROM input_sessions "
            "WHERE session_id = %s",
            (session_id,),
        )
        row = cur.fetchone()
    if row is None or not capability_matches(token, row["capability_token_hash"]):
        raise InputSessionError("upload link is not valid")
    if row["expires_at"] <= datetime.now(UTC):
        raise InputSessionError("upload link has expired")
    if row["status"] in {"finalized", "attached", "expired"}:
        # Revoked at finalization: a link that already produced a handle must
        # not keep granting write access to the staging prefix.
        raise InputSessionError("upload link has already been used")
    return _load(conn, session_id)


def mark_uploading(conn: DbConnection, session_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE input_sessions SET status = 'uploading' "
            "WHERE session_id = %s AND status = 'created'",
            (session_id,),
        )


def finalize(
    conn: DbConnection,
    session_id: str,
    scope: CallerScope,
    *,
    fetch_bytes: Any,
) -> InputSession:
    """Verify every uploaded object, then close the session.

    ``fetch_bytes(storage_key) -> bytes`` is injected so this stays testable
    without Blob. Verification is against the REAL bytes: the declaration was a
    plan, and a session that finalized on the declaration alone would let a
    browser upload anything it liked under an allowlisted name.
    """
    session = require_scope(_load(conn, session_id), scope)
    if session.status in {"finalized", "attached"}:
        return session  # idempotent
    if session.expires_at <= datetime.now(UTC):
        raise InputSessionError("input session has expired")

    for item in session.files:
        try:
            raw = fetch_bytes(item["staging_blob_key"])
        except Exception as exc:  # noqa: BLE001 - a missing object is a rejection
            _reject(conn, item["file_id"])
            raise UploadRejected(
                f"file {item['ordinal']} was never uploaded", code="missing_upload"
            ) from exc
        digest = verify_content(
            raw,
            media_type=item["media_type"],
            declared_size=item["size_bytes"] or 0,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE input_files SET sha256 = %s, size_bytes = %s, status = 'verified' "
                "WHERE file_id = %s",
                (digest, len(raw), item["file_id"]),
            )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE input_sessions SET status = 'finalized', finalized_at = now(), "
            # Rotate the stored hash to a value nothing can produce: the page
            # capability is revoked the instant the handle exists.
            "capability_token_hash = %s WHERE session_id = %s",
            (hash_capability(str(uuid.uuid4())), session_id),
        )
    return _load(conn, session_id)


def _reject(conn: DbConnection, file_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE input_files SET status = 'rejected' WHERE file_id = %s", (file_id,))


def attach_to_run(
    conn: DbConnection, *, handle: str, run_id: str, scope: CallerScope
) -> str:
    """Bind a finalized, caller-owned input set to a run. Returns the session id.

    Called inside ``start_run``'s transaction so a run can never be dispatched
    referencing an input set it does not actually own.
    """
    session_id = parse_handle(handle)
    session = require_scope(_load(conn, session_id), scope)
    if session.status != "finalized":
        raise InputSessionError(
            f"input set {handle} is {session.status}, not finalized; call "
            "finalize_input_session first"
        )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE input_sessions SET status = 'attached' WHERE session_id = %s",
            (session_id,),
        )
        cur.execute(
            "INSERT INTO run_events (run_id, kind, detail) VALUES (%s, 'input_attached', %s)",
            (run_id, json.dumps({"session_id": session_id, "files": len(session.files)})),
        )
    return session_id
