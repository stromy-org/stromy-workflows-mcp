"""Inbound upload policy (ORG-PLAN-164 WS3).

This module is the only thing standing between an arbitrary browser upload and a
folder the runner will read and write a client report from, so the coverage here
is deliberately exhaustive rather than representative.
"""

from __future__ import annotations

import pytest

from stromy_workflows_mcp import uploads
from stromy_workflows_mcp.input_sessions import InputSessionError, parse_handle
from stromy_workflows_mcp.uploads import DeclaredFile, UploadRejected

SESSION = "11111111-1111-1111-1111-111111111111"


def _declare(name: str, size: int = 10, media_type: str | None = None) -> DeclaredFile:
    return DeclaredFile(name=name, size_bytes=size, media_type=media_type)


# --- Capability tokens -------------------------------------------------------


def test_tokens_are_high_entropy_and_unique() -> None:
    tokens = {uploads.new_capability_token() for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 32 for t in tokens)


def test_only_the_hash_is_persistable() -> None:
    """A registry dump must not be a working set of upload capabilities."""
    token = uploads.new_capability_token()
    stored = uploads.hash_capability(token)
    assert token not in stored
    assert uploads.capability_matches(token, stored)
    assert not uploads.capability_matches(uploads.new_capability_token(), stored)


# --- Name policy -------------------------------------------------------------


@pytest.mark.parametrize("name", ["brief.md", "notes.txt", "report.pdf", "UPPER.PDF"])
def test_allowlisted_extensions_are_accepted(name: str) -> None:
    assert uploads.safe_extension(name) in uploads.ALLOWED_EXTENSIONS


@pytest.mark.parametrize(
    "name", ["payload.exe", "script.js", "macro.docm", "archive.zip", "noext", "x.svg"]
)
def test_everything_outside_the_allowlist_is_refused(name: str) -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.safe_extension(name)
    assert exc.value.code == "type_not_allowed"


def test_extension_is_taken_from_the_final_component_only() -> None:
    """`evil.pdf/x.exe` must not pass because it contains an allowed segment."""
    with pytest.raises(UploadRejected):
        uploads.safe_extension("evil.pdf/x.exe")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "../../etc/passwd",
        "a/b.md",
        "a\\b.md",
        "bad\x00name.md",
        "line\nbreak.md",
        "..",
        "x" * 300 + ".md",
    ],
)
def test_hostile_display_names_are_refused_not_sanitised(name: str) -> None:
    """Silent sanitisation hides an attack; an explicit rejection surfaces it."""
    with pytest.raises(UploadRejected):
        uploads.validate_display_name(name)


def test_storage_name_is_derived_from_server_values_only() -> None:
    """Nothing the caller supplied appears in the storage key."""
    key = uploads.generated_storage_name(SESSION, 3, ".md")
    assert key == f"staging/{SESSION}/003.md"
    assert ".." not in key


# --- Declaration -------------------------------------------------------------


def test_a_valid_declaration_is_accepted_and_ordered() -> None:
    accepted = uploads.accept_declaration(
        [_declare("b.md"), _declare("a.pdf")], session_id=SESSION
    )
    assert [f.ordinal for f in accepted] == [1, 2]
    assert [f.display_name for f in accepted] == ["b.md", "a.pdf"]
    assert all(f.storage_name.startswith(f"staging/{SESSION}/") for f in accepted)


def test_declared_media_type_is_discarded_in_favour_of_the_extension() -> None:
    """The caller's Content-Type is caller-controlled and therefore worthless."""
    accepted = uploads.accept_declaration(
        [_declare("brief.md", media_type="application/x-executable")], session_id=SESSION
    )
    assert accepted[0].media_type == "text/markdown"


def test_empty_session_is_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([], session_id=SESSION)
    assert exc.value.code == "empty_session"


def test_file_count_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uploads, "MAX_FILES", 2)
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_declare(f"{i}.md") for i in range(3)], session_id=SESSION)
    assert exc.value.code == "too_many_files"


def test_per_file_ceiling() -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration(
            [_declare("big.pdf", size=uploads.MAX_FILE_BYTES + 1)], session_id=SESSION
        )
    assert exc.value.code == "file_too_large"


def test_session_total_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """A set of individually-legal files must not add up to an illegal session."""
    monkeypatch.setattr(uploads, "MAX_SESSION_BYTES", 100)
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration(
            [_declare("a.md", size=60), _declare("b.md", size=60)], session_id=SESSION
        )
    assert exc.value.code == "session_too_large"


def test_zero_byte_file_is_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_declare("empty.md", size=0)], session_id=SESSION)
    assert exc.value.code == "empty_file"


# --- Content verification ----------------------------------------------------


def test_valid_pdf_verifies_and_returns_its_digest() -> None:
    raw = b"%PDF-1.7 real content"
    digest = uploads.verify_content(raw, media_type="application/pdf", declared_size=len(raw))
    assert len(digest) == 64


def test_a_renamed_executable_is_caught_by_magic_bytes() -> None:
    """The extension said .pdf; the bytes say otherwise.

    Declared size matches deliberately, so the magic-byte check is what rejects
    this rather than the size check firing first.
    """
    raw = b"MZ\x90\x00executable"
    with pytest.raises(UploadRejected) as exc:
        uploads.verify_content(raw, media_type="application/pdf", declared_size=len(raw))
    assert exc.value.code == "type_mismatch"


def test_encrypted_pdf_is_refused() -> None:
    """It cannot be read, so accepting it yields an analysis silently missing it."""
    raw = b"%PDF-1.7" + b"x" * 100 + b"/Encrypt 1 0 R"
    with pytest.raises(UploadRejected) as exc:
        uploads.verify_content(raw, media_type="application/pdf", declared_size=len(raw))
    assert exc.value.code == "encrypted_pdf"


def test_non_utf8_text_is_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.verify_content(b"\xff\xfe\x00binary", media_type="text/markdown", declared_size=9)
    assert exc.value.code == "type_mismatch"


def test_size_mismatch_between_declaration_and_upload_is_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.verify_content(b"# short", media_type="text/markdown", declared_size=9999)
    assert exc.value.code == "size_mismatch"


def test_empty_upload_is_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.verify_content(b"", media_type="text/markdown", declared_size=0)
    assert exc.value.code == "empty_file"


# --- Inline content ----------------------------------------------------------
#
# The agent-side channel: text drafted and reviewed in the conversation enters
# the session directly, with the SAME content gates the browser path gets at
# finalization — applied at declaration, the only moment the author can still
# fix the input.


def _inline(name: str = "briefing.md", content: str = "# Briefing\n", **kw) -> DeclaredFile:
    return DeclaredFile(name=name, content=content, **kw)


def test_inline_text_is_accepted_and_its_size_is_derived() -> None:
    accepted = uploads.accept_declaration([_inline()], session_id=SESSION)
    assert accepted[0].content_bytes == b"# Briefing\n"
    assert accepted[0].size_bytes == len(b"# Briefing\n")
    assert accepted[0].media_type == "text/markdown"


def test_inline_size_is_derived_from_bytes_not_characters() -> None:
    """A é is one character and two UTF-8 bytes; the byte count must win."""
    accepted = uploads.accept_declaration([_inline(content="café\n")], session_id=SESSION)
    assert accepted[0].size_bytes == len("café\n".encode())


def test_inline_pdf_is_refused() -> None:
    """Binary formats stay on the browser path; inline is text-only."""
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_inline(name="report.pdf")], session_id=SESSION)
    assert exc.value.code == "inline_not_text"


def test_inline_above_the_inline_ceiling_is_refused() -> None:
    big = "x" * (uploads.MAX_INLINE_BYTES + 1)
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_inline(content=big)], session_id=SESSION)
    assert exc.value.code == "inline_too_large"


def test_inline_with_a_contradicting_declared_size_is_refused() -> None:
    """One source of truth: the bytes. A stale declared size must not ride along."""
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration(
            [_inline(content="# Hello\n", size_bytes=9999)], session_id=SESSION
        )
    assert exc.value.code == "size_mismatch"


def test_inline_with_a_matching_declared_size_is_accepted() -> None:
    content = "# Hello\n"
    accepted = uploads.accept_declaration(
        [_inline(content=content, size_bytes=len(content.encode()))], session_id=SESSION
    )
    assert accepted[0].size_bytes == len(content.encode())


def test_empty_inline_content_is_refused() -> None:
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_inline(content="")], session_id=SESSION)
    assert exc.value.code == "empty_file"


def test_inline_content_with_nul_bytes_is_refused() -> None:
    """The finalization content gate runs at declaration for inline files."""
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_inline(content="a\x00b")], session_id=SESSION)
    assert exc.value.code == "type_mismatch"


def test_a_declared_file_still_requires_an_exact_size() -> None:
    """The browser path's contract is unchanged — and the refusal now teaches it."""
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_declare("brief.pdf", size=0)], session_id=SESSION)
    assert exc.value.code == "empty_file"
    assert "EXACT" in str(exc.value)


def test_inline_files_count_toward_the_session_total() -> None:
    """The session ceiling is one budget, whichever channel fills it."""
    chunk = "x" * uploads.MAX_INLINE_BYTES
    # Enough max-size declared files to land exactly on the session ceiling, so
    # the inline bytes are what tip it over.
    fills = uploads.MAX_SESSION_BYTES // uploads.MAX_FILE_BYTES
    declared = [_declare(f"big{i}.pdf", size=uploads.MAX_FILE_BYTES) for i in range(fills)]
    with pytest.raises(UploadRejected) as exc:
        uploads.accept_declaration([_inline(content=chunk), *declared], session_id=SESSION)
    assert exc.value.code == "session_too_large"


# --- Redaction ---------------------------------------------------------------


def test_sas_signature_is_redacted() -> None:
    """A SAS in a log outlives the TTL that was supposed to bound it."""
    url = "https://acct.blob.core.windows.net/c/k?sv=2024-11-04&se=2026-08-01&sig=AbC%2Fdef"
    redacted = uploads.redact(url)
    assert "AbC" not in redacted
    assert "sig=REDACTED" in redacted
    assert "acct.blob.core.windows.net" in redacted  # still diagnosable


# --- Handles -----------------------------------------------------------------


def test_handle_round_trip() -> None:
    assert parse_handle(f"inputset:{SESSION}") == SESSION


@pytest.mark.parametrize(
    "handle",
    [
        "/mnt/runs/other-client/evidence",
        "inputset:../../etc",
        SESSION,
        "inputset:",
        "inputset:not-a-uuid",
        "",
    ],
)
def test_bad_handles_are_refused_before_reaching_sql(handle: str) -> None:
    """Run config is the most caller-controlled surface the facade has."""
    with pytest.raises(InputSessionError):
        parse_handle(handle)
