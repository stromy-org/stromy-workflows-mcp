"""Inbound upload-session policy (ORG-PLAN-164 WS3).

The rules a client-uploaded file must satisfy before it can become evidence a
report is written from. Kept in one module, free of transport and database
concerns, because this is the part that has to be exhaustively testable — it is
the only thing standing between an arbitrary browser upload and a folder the
runner will read.

Applied per OWASP's file-upload guidance:

* a strict **allowlist**, never a denylist — a denylist is a list of the attacks
  someone already thought of;
* **generated storage names**, because a user-supplied filename is an injection
  surface (traversal, control characters, NTFS streams, homoglyphs);
* **detected type**, not the declared header — the ``Content-Type`` a browser
  sends is caller-controlled and therefore worthless as a check;
* explicit **per-file, per-session and count ceilings**;
* uploaded bytes are **never executed and never served inline**.

A capability token is a bearer credential. It is generated with
``secrets.token_urlsafe``, stored only as a hash, single-session, expiring, and
revoked at finalization.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

# --- Limits (server-owned; the fragment sets them as env) --------------------

MAX_FILES = int(os.environ.get("WORKFLOW_UPLOAD_MAX_FILES", "20"))
MAX_FILE_BYTES = int(os.environ.get("WORKFLOW_UPLOAD_MAX_FILE_BYTES", str(25 * 1024 * 1024)))
MAX_SESSION_BYTES = int(
    os.environ.get("WORKFLOW_UPLOAD_MAX_SESSION_BYTES", str(100 * 1024 * 1024))
)
SESSION_TTL_SECONDS = int(os.environ.get("WORKFLOW_UPLOAD_SESSION_TTL_SECONDS", "3600"))
MAX_FILENAME_CHARS = 200

#: v1 allowlist. Richer Office formats need malware/CDR controls, which are a
#: separate security decision — not an implicit extension of this tuple.
ALLOWED_EXTENSIONS: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}

#: Leading bytes that prove a PDF really is one. Text types have no magic, so
#: they are validated by decodability instead.
_PDF_MAGIC = b"%PDF-"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class UploadRejected(ValueError):
    """A declared or uploaded file violates policy.

    Carries ``code`` so the facade returns a stable error surface rather than
    forcing a caller to string-match a message.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DeclaredFile:
    """One file a client says it intends to upload."""

    name: str
    size_bytes: int
    media_type: str | None = None


@dataclass(frozen=True)
class AcceptedFile:
    """A declared file that passed policy, with its generated storage name."""

    display_name: str
    storage_name: str
    media_type: str
    size_bytes: int
    ordinal: int


# --- Capability tokens -------------------------------------------------------


def new_capability_token() -> str:
    """A high-entropy page token. Returned to the caller exactly once."""
    return secrets.token_urlsafe(32)


def hash_capability(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def capability_matches(token: str, stored_hash: str) -> bool:
    """Constant-time comparison — a token check must not leak via timing."""
    return secrets.compare_digest(hash_capability(token), stored_hash)


# --- Name policy -------------------------------------------------------------


def safe_extension(name: str) -> str:
    """Return the allowlisted extension, or raise.

    Taken from the FINAL component only, so ``evil.pdf/x.md`` cannot smuggle a
    directory past the check, and lowercased so ``.PDF`` is not a bypass.
    """
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise UploadRejected(
            f"file type {suffix or '(none)'} is not accepted; allowed: "
            f"{', '.join(sorted(ALLOWED_EXTENSIONS))}",
            code="type_not_allowed",
        )
    return suffix


def validate_display_name(name: str) -> str:
    """Reject a filename outright rather than silently sanitising it.

    Silent sanitisation hides an attack; an explicit rejection surfaces it. The
    *stored* name is generated regardless (see ``generated_storage_name``), so
    this check exists to refuse obviously hostile input, not to make it safe.
    """
    if not isinstance(name, str) or not name.strip():
        raise UploadRejected("file name is empty", code="name_invalid")
    if len(name) > MAX_FILENAME_CHARS:
        raise UploadRejected("file name is too long", code="name_invalid")
    if _CONTROL_RE.search(name):
        raise UploadRejected("file name contains control characters", code="name_invalid")
    if "/" in name or "\\" in name:
        raise UploadRejected("file name contains a path separator", code="name_invalid")
    normalized = unicodedata.normalize("NFKC", name)
    if normalized in {".", ".."} or normalized.startswith(".."):
        raise UploadRejected("file name is a path traversal", code="name_invalid")
    return name


def generated_storage_name(session_id: str, ordinal: int, suffix: str) -> str:
    """Storage key component — derived from server values only.

    Nothing the caller supplied appears here. That is the point: a generated
    name cannot collide, cannot traverse, and cannot be crafted.
    """
    return f"staging/{session_id}/{ordinal:03d}{suffix}"


# --- Declaration policy ------------------------------------------------------


def accept_declaration(files: list[DeclaredFile], *, session_id: str) -> list[AcceptedFile]:
    """Validate a whole declared file set, or raise on the first violation."""
    if not files:
        raise UploadRejected("an input session needs at least one file", code="empty_session")
    if len(files) > MAX_FILES:
        raise UploadRejected(
            f"{len(files)} files exceed the {MAX_FILES}-file limit", code="too_many_files"
        )

    accepted: list[AcceptedFile] = []
    total = 0
    for ordinal, declared in enumerate(files, start=1):
        validate_display_name(declared.name)
        suffix = safe_extension(declared.name)
        if declared.size_bytes <= 0:
            raise UploadRejected(
                f"file {ordinal} is empty; an empty file carries no evidence",
                code="empty_file",
            )
        if declared.size_bytes > MAX_FILE_BYTES:
            raise UploadRejected(
                f"file {ordinal} is {declared.size_bytes} bytes, above the "
                f"{MAX_FILE_BYTES}-byte per-file limit",
                code="file_too_large",
            )
        total += declared.size_bytes
        if total > MAX_SESSION_BYTES:
            raise UploadRejected(
                f"session exceeds the {MAX_SESSION_BYTES}-byte total limit",
                code="session_too_large",
            )
        accepted.append(
            AcceptedFile(
                display_name=declared.name,
                storage_name=generated_storage_name(session_id, ordinal, suffix),
                # The DECLARED media type is deliberately discarded: it is
                # caller-controlled. The extension is allowlisted and the real
                # bytes are checked at finalization.
                media_type=ALLOWED_EXTENSIONS[suffix],
                size_bytes=declared.size_bytes,
                ordinal=ordinal,
            )
        )
    return accepted


# --- Content policy (finalization) -------------------------------------------


def verify_content(raw: bytes, *, media_type: str, declared_size: int) -> str:
    """Check the actual bytes and return their digest.

    Runs at finalization, against what was really uploaded — the declaration is
    a plan, this is the fact. A mismatch here means the browser uploaded
    something other than what the session authorised.
    """
    if not raw:
        raise UploadRejected("uploaded file is empty", code="empty_file")
    if len(raw) > MAX_FILE_BYTES:
        raise UploadRejected("uploaded file exceeds the per-file limit", code="file_too_large")
    if declared_size and len(raw) != declared_size:
        raise UploadRejected(
            "uploaded size does not match the declared size", code="size_mismatch"
        )

    if media_type == "application/pdf":
        if not raw.startswith(_PDF_MAGIC):
            raise UploadRejected(
                "file does not have PDF magic bytes despite a .pdf extension",
                code="type_mismatch",
            )
        if b"/Encrypt" in raw[:4096] or b"/Encrypt" in raw[-4096:]:
            # An encrypted PDF cannot be read by the pipeline, so accepting it
            # would produce an analysis silently missing that evidence.
            raise UploadRejected("encrypted PDFs are not accepted", code="encrypted_pdf")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadRejected(
                "text file is not valid UTF-8", code="type_mismatch"
            ) from exc
        if "\x00" in text:
            raise UploadRejected("text file contains NUL bytes", code="type_mismatch")

    return hashlib.sha256(raw).hexdigest()


# --- Redaction ---------------------------------------------------------------

_SAS_QUERY_RE = re.compile(r"[?&](sig|se|sp|sv|st|skoid|sktid|se)=[^&\s]*", re.IGNORECASE)


def redact(value: str) -> str:
    """Strip SAS query parameters from anything about to be logged.

    A SAS is a bearer credential in a URL. Logging one at INFO puts a working
    write capability into the log store, where it long outlives the 15-minute
    TTL that was supposed to bound it.
    """
    return _SAS_QUERY_RE.sub(lambda m: m.group(0).split("=")[0] + "=REDACTED", value)
