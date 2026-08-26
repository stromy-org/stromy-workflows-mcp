"""Generic read-only filesystem tools — serve skills and other shipped files to
any MCP client (works where MCP resources are unsupported)."""

import hashlib

from fastmcp.tools import tool

from stromy_workflows_mcp.config import settings
from stromy_workflows_mcp.fs import MAX_READ_BYTES, PROJECT_ROOT, resolve_within_roots
from stromy_workflows_mcp.response_budget import DEFAULT_RESPONSE_CONTENT_CHARS, excerpt_text

# Room for the wrapper fields (path, offsets, sha256, JSON punctuation) so the
# COMPLETE result fits the budget, not just the content slice. Generous on
# purpose: a slightly short page costs one extra call, an over-budget result
# costs the whole response.
_FS_READ_OVERHEAD_CHARS = 512


@tool
def fs_read(
    path: str, offset_chars: int = 0, max_chars: int = DEFAULT_RESPONSE_CONTENT_CHARS
) -> dict:
    """Read a UTF-8 text file from the server's content roots, one page at a time.

    Skills live under `skills/<name>/SKILL.md` (+ `references/`). Paths are
    relative to the project root and must stay inside an allowed root; traversal
    outside is rejected.

    Returns `{path, content, offset_chars, next_offset_chars, truncated, sha256}`.
    **The file body is in `content`.** When `next_offset_chars` is not null the
    file continues: call again with `offset_chars` set to that value and
    concatenate. `sha256` is the digest of the WHOLE file, so a caller can tell
    that a file changed between pages rather than silently stitching two
    versions together.

    `max_chars` bounds this response; the byte guard on the file itself remains
    a separate input/resource limit.
    """
    target = resolve_within_roots(path)
    if not target.is_file():
        raise FileNotFoundError(f"no file at {path!r}")
    if target.stat().st_size > MAX_READ_BYTES:
        raise ValueError(f"{path!r} exceeds {MAX_READ_BYTES} bytes")
    raw = target.read_bytes()
    text = raw.decode("utf-8")
    chunk, next_offset = excerpt_text(
        text,
        offset_chars=offset_chars,
        max_chars=max_chars,
        overhead_chars=_FS_READ_OVERHEAD_CHARS,
    )
    return {
        "path": path,
        "content": chunk,
        "offset_chars": max(0, int(offset_chars)),
        "next_offset_chars": next_offset,
        "truncated": next_offset is not None,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@tool
def fs_list(path: str = "") -> list[dict]:
    """List entries in a content root. Empty path lists the roots themselves.
    Call fs_list("skills") to discover skills, then fs_read the SKILL.md."""
    if not path:
        return [
            {"name": r, "is_dir": True}
            for r in settings.fs_roots
            if (PROJECT_ROOT / r).is_dir()
        ]
    target = resolve_within_roots(path)
    if not target.is_dir():
        raise NotADirectoryError(f"{path!r} is not a directory")
    return sorted(
        (
            {
                "name": e.name,
                "is_dir": e.is_dir(),
                "size": e.stat().st_size if e.is_file() else None,
            }
            for e in target.iterdir()
        ),
        key=lambda e: (not e["is_dir"], e["name"]),
    )
