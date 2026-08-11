"""The browser upload surface (ORG-PLAN-164 WS3).

Covers the parts that decide whether an arbitrary browser can put bytes in front
of the runner: what a SAS is scoped to, what the page leaks, and whether the
capability can be widened into someone else's session.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from stromy_workflows_mcp import blobs, input_sessions, upload_page
from stromy_workflows_mcp.scoping import CallerScope
from stromy_workflows_mcp.uploads import AcceptedFile


class _RecordingMinter:
    """Captures exactly what a SAS would be scoped to."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def mint(self, *, blob_key: str, media_type: str) -> tuple[str, datetime]:
        self.calls.append({"blob_key": blob_key, "media_type": media_type})
        return (
            f"https://acct.blob.core.windows.net/workflow-inputs/{blob_key}?sig=SECRET",
            datetime.now(UTC) + timedelta(minutes=15),
        )


def _accepted(n: int = 2) -> list[AcceptedFile]:
    return [
        AcceptedFile(
            display_name=f"doc{i}.pdf",
            storage_name=f"staging/sess/{i:03d}.pdf",
            media_type="application/pdf",
            size_bytes=100,
            ordinal=i,
        )
        for i in range(1, n + 1)
    ]


def test_one_url_is_minted_per_declared_file_in_order() -> None:
    minter = _RecordingMinter()
    targets = blobs.build_upload_targets(_accepted(3), minter)

    assert [t.ordinal for t in targets] == [1, 2, 3]
    assert [c["blob_key"] for c in minter.calls] == [
        "staging/sess/001.pdf",
        "staging/sess/002.pdf",
        "staging/sess/003.pdf",
    ]


def test_the_minted_content_type_comes_from_the_allowlist_not_the_caller() -> None:
    """The server pins the stored type; a browser header cannot change it."""
    minter = _RecordingMinter()
    blobs.build_upload_targets(_accepted(1), minter)

    assert minter.calls[0]["media_type"] == "application/pdf"


def test_a_session_scope_is_exactly_the_sessions_own_client() -> None:
    """The capability proves 'this session'; the row decides which client."""
    session = input_sessions.InputSession(
        session_id="11111111-1111-1111-1111-111111111111",
        client_slug="dukestrategies",
        status="created",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        finalized_at=None,
        files=[],
    )

    scope = upload_page._session_scope(session)

    assert scope == CallerScope(frozenset({"dukestrategies"}))
    assert scope.unrestricted is False


def test_a_page_scope_can_never_be_unrestricted() -> None:
    """An operator-wide scope must not be reachable from a browser capability."""
    session = input_sessions.InputSession(
        session_id="22222222-2222-2222-2222-222222222222",
        client_slug="amaris",
        status="created",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        finalized_at=None,
        files=[],
    )

    assert upload_page._session_scope(session).unrestricted is False


def test_the_public_view_never_carries_a_token_or_a_storage_key() -> None:
    session = input_sessions.InputSession(
        session_id="33333333-3333-3333-3333-333333333333",
        client_slug="amaris",
        status="uploading",
        created_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        finalized_at=None,
        files=[
            {
                "ordinal": 1,
                "display_name": "brief.pdf",
                "status": "declared",
                "size_bytes": 10,
                "media_type": "application/pdf",
                "staging_blob_key": "staging/33333333/001.pdf",
                "capability_token_hash": "deadbeef",
            }
        ],
    )

    blob = json.dumps(session.public())

    assert "staging/" not in blob
    assert "deadbeef" not in blob
    assert "brief.pdf" in blob


def test_the_page_escapes_a_hostile_display_name() -> None:
    """A filename reaches the page as text; it must never reach it as markup."""
    assert upload_page._escape('<img src=x onerror="alert(1)">') == (
        "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;"
    )


def test_the_page_forbids_framing_and_external_origins() -> None:
    csp = upload_page._SECURITY_HEADERS["Content-Security-Policy"]

    assert "frame-ancestors 'none'" in csp
    assert "default-src 'none'" in csp
    # The direct-to-Blob PUT is the one cross-origin call the page may make.
    assert "connect-src 'self' https://*.blob.core.windows.net" in csp
    assert upload_page._SECURITY_HEADERS["Referrer-Policy"] == "no-referrer"


def test_the_page_is_never_cached() -> None:
    """A cached page is a cached capability."""
    assert upload_page._SECURITY_HEADERS["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "handle",
    [
        "inputset:../../etc/passwd",
        "inputset:'; DROP TABLE runs;--",
        "notasession:11111111-1111-1111-1111-111111111111",
        "inputset:",
    ],
)
def test_a_hostile_handle_never_reaches_sql(handle: str) -> None:
    with pytest.raises(input_sessions.InputSessionError):
        input_sessions.parse_handle(handle)
