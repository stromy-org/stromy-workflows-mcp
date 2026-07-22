"""Generic read-only filesystem tools — serve skills and other shipped files to
any MCP client (works where MCP resources are unsupported)."""

from fastmcp.tools import tool

from stromy_workflows_mcp.config import settings
from stromy_workflows_mcp.fs import MAX_READ_BYTES, PROJECT_ROOT, resolve_within_roots


@tool
def fs_read(path: str) -> str:
    """Read a UTF-8 text file from the server's content roots. Skills live under
    `skills/<name>/SKILL.md` (+ `references/`). Paths are relative to the project
    root and must stay inside an allowed root; traversal outside is rejected."""
    target = resolve_within_roots(path)
    if not target.is_file():
        raise FileNotFoundError(f"no file at {path!r}")
    if target.stat().st_size > MAX_READ_BYTES:
        raise ValueError(f"{path!r} exceeds {MAX_READ_BYTES} bytes")
    return target.read_text(encoding="utf-8")


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
