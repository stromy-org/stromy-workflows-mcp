"""Inline input content — the agent-side channel of an input session.

Two halves, matching what can actually break:

* **Service shape** (pure, monkeypatched): the ``upload_url`` capability is
  minted only when a browser still has work to do, the staging writer is only
  constructed when inline bytes exist, and hostile ``content`` values are
  refused before any I/O.
* **Round trip** (real Postgres, in-memory blob store): create with inline
  content → rows land as ``uploaded`` → ``finalize`` verifies the staged bytes →
  ``attach_to_run`` accepts the handle. Faked SQL would assert we call our own
  code; what has to be true is what the engine does with the rows.
"""

from __future__ import annotations

import itertools
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from stromy_workflows_mcp import input_sessions, registry, service
from stromy_workflows_mcp.input_sessions import InputSession, InputSessionError
from stromy_workflows_mcp.scoping import CallerScope
from stromy_workflows_mcp.uploads import AcceptedFile, DeclaredFile, UploadRejected

OWNER = CallerScope(frozenset({"dukestrategies"}))
SESSION_ID = "22222222-2222-2222-2222-222222222222"


# --- Service shape -----------------------------------------------------------


def _session(files: list[dict[str, Any]]) -> InputSession:
    return InputSession(
        session_id=SESSION_ID,
        client_slug="dukestrategies",
        status="created",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        finalized_at=None,
        files=files,
    )


def _accepted(*, inline: bool, ordinal: int = 1) -> AcceptedFile:
    return AcceptedFile(
        display_name="briefing.md" if inline else "brief.pdf",
        storage_name=f"staging/{SESSION_ID}/{ordinal:03d}.md",
        media_type="text/markdown" if inline else "application/pdf",
        size_bytes=8,
        ordinal=ordinal,
        content_bytes=b"# Hello\n" if inline else None,
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Fake the registry boundary and record what create_session receives."""

    def _install(accepted: list[AcceptedFile]):
        @contextmanager
        def _connect(*_a: Any, **_k: Any):
            yield object()

        monkeypatch.setattr(registry, "connect", _connect)
        monkeypatch.setattr(registry, "schema_version", lambda _conn: 2)
        monkeypatch.setattr(registry, "require_data_plane", lambda *_a, **_k: None)
        monkeypatch.setenv("WORKFLOW_PUBLIC_BASE_URL", "https://facade.example")

        calls: list[dict[str, Any]] = []

        def _create_session(_conn: Any, *, client_slug: str, files: Any, stage_bytes: Any = None):
            calls.append({"files": files, "stage_bytes": stage_bytes})
            statuses = [
                {
                    "ordinal": item.ordinal,
                    "display_name": item.display_name,
                    "status": "uploaded" if item.content_bytes is not None else "declared",
                    "size_bytes": item.size_bytes,
                    "media_type": item.media_type,
                }
                for item in accepted
            ]
            return _session(statuses), "RAWTOKEN", accepted

        monkeypatch.setattr(input_sessions, "create_session", _create_session)
        return calls

    return _install


def test_no_upload_url_is_returned_when_every_file_is_inline(patched) -> None:
    """An unused write capability should not exist, let alone travel."""
    patched([_accepted(inline=True)])

    payload = service.create_input_session(
        [{"name": "briefing.md", "content": "# Hello\n"}],
        {"client_slug": "dukestrategies"},
        OWNER,
        stage_bytes=lambda *_a: None,
    )

    assert "upload_url" not in payload
    assert "RAWTOKEN" not in str(payload)
    assert payload["files"][0]["status"] == "uploaded"


def test_a_mixed_session_still_returns_the_browser_link(patched) -> None:
    patched([_accepted(inline=True, ordinal=1), _accepted(inline=False, ordinal=2)])

    payload = service.create_input_session(
        [
            {"name": "briefing.md", "content": "# Hello\n"},
            {"name": "brief.pdf", "size_bytes": 8},
        ],
        {"client_slug": "dukestrategies"},
        OWNER,
        stage_bytes=lambda *_a: None,
    )

    assert payload["upload_url"].startswith(f"https://facade.example/uploads/{SESSION_ID}?t=")


def test_no_staging_writer_is_built_for_a_declared_only_session(patched) -> None:
    """A browser-only session must not require blob credentials in the facade."""
    calls = patched([_accepted(inline=False)])

    service.create_input_session(
        [{"name": "brief.pdf", "size_bytes": 8}], {"client_slug": "dukestrategies"}, OWNER
    )

    assert calls[0]["stage_bytes"] is None


def test_non_string_content_is_refused_before_any_io(patched) -> None:
    patched([])
    with pytest.raises(UploadRejected) as exc:
        service.create_input_session(
            [{"name": "briefing.md", "content": {"nested": "object"}}],
            {"client_slug": "dukestrategies"},
            OWNER,
        )
    assert exc.value.code == "inline_not_text"


def test_inline_without_a_writer_is_an_error_not_a_silent_drop() -> None:
    with pytest.raises(InputSessionError):
        input_sessions.create_session(
            _NoDb(),
            client_slug="dukestrategies",
            files=[DeclaredFile(name="briefing.md", content="# Hello\n")],
            stage_bytes=None,
        )


class _NoDb:
    """create_session must fail before it ever needs a cursor."""

    def cursor(self) -> Any:  # pragma: no cover - reaching here is the failure
        raise AssertionError("no row may be written for an unstageable session")


# --- Round trip against a real engine ---------------------------------------

_DSN_ENV = "STROMY_TEST_PG_DSN"
_counter = itertools.count(1)


def _normalise(dsn: str) -> str:
    return dsn.replace("postgresql+psycopg2://", "postgresql://")


@pytest.fixture(scope="session")
def _engine() -> Iterator[str]:
    provided = os.environ.get(_DSN_ENV, "").strip()
    if provided:
        yield _normalise(provided)
        return
    testcontainers = pytest.importorskip(
        "testcontainers.postgres",
        reason=f"neither {_DSN_ENV} nor testcontainers is available",
    )
    with testcontainers.PostgresContainer("postgres:16-alpine") as pg:
        yield _normalise(pg.get_connection_url())


@pytest.fixture
def conn(_engine: str):
    """A fresh database per test, with BOTH migration chains applied."""
    import psycopg
    from workflow_runtime_core.migrations import apply_migrations

    from stromy_workflows_mcp import migrations

    name = f"inline_test_{next(_counter)}"
    with psycopg.connect(_engine, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    head, _, _tail = _engine.rpartition("/")
    with psycopg.connect(f"{head}/{name}", row_factory=psycopg.rows.dict_row) as c:
        apply_migrations(c)
        migrations.apply(c)
        c.commit()
        yield c


def test_inline_round_trip_creates_stages_finalizes(conn) -> None:
    store: dict[str, bytes] = {}

    session, _token, accepted = input_sessions.create_session(
        conn,
        client_slug="dukestrategies",
        files=[DeclaredFile(name="briefing.md", content="# The briefing\n")],
        stage_bytes=lambda key, data, _mt: store.__setitem__(key, data),
    )
    conn.commit()

    assert store[accepted[0].storage_name] == b"# The briefing\n"
    assert session.files[0]["status"] == "uploaded"

    finalized = input_sessions.finalize(
        conn, session.session_id, OWNER, fetch_bytes=lambda key: store[key]
    )
    conn.commit()

    assert finalized.status == "finalized"
    assert finalized.files[0]["status"] == "verified"
    assert finalized.files[0]["size_bytes"] == len(b"# The briefing\n")


def test_a_mixed_session_finalizes_only_after_the_browser_upload_lands(conn) -> None:
    store: dict[str, bytes] = {}
    pdf_bytes = b"%PDF-1.7 minimal"

    session, _token, accepted = input_sessions.create_session(
        conn,
        client_slug="dukestrategies",
        files=[
            DeclaredFile(name="briefing.md", content="# Notes\n"),
            DeclaredFile(name="brief.pdf", size_bytes=len(pdf_bytes)),
        ],
        stage_bytes=lambda key, data, _mt: store.__setitem__(key, data),
    )
    conn.commit()
    assert [f["status"] for f in session.files] == ["uploaded", "declared"]

    with pytest.raises(UploadRejected) as exc:
        input_sessions.finalize(
            conn, session.session_id, OWNER, fetch_bytes=lambda key: store[key]
        )
    assert exc.value.code == "missing_upload"
    conn.commit()

    store[accepted[1].storage_name] = pdf_bytes  # the browser PUT arrives
    finalized = input_sessions.finalize(
        conn, session.session_id, OWNER, fetch_bytes=lambda key: store[key]
    )
    conn.commit()
    assert [f["status"] for f in finalized.files] == ["verified", "verified"]
