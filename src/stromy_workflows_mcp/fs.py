"""Sandboxed filesystem access for the fs_read / fs_list tools."""

from pathlib import Path

from .config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
MAX_READ_BYTES = 1_000_000  # guard against huge / binary reads


def _allowed_roots() -> list[Path]:
    return [(PROJECT_ROOT / r).resolve() for r in settings.fs_roots]


def resolve_within_roots(path: str) -> Path:
    """Resolve `path` (relative to the project root) and confirm it stays inside
    an allowed root. .resolve() collapses '..' and follows symlinks, so escapes
    via traversal, absolute paths, or symlinks are all rejected."""
    candidate = (PROJECT_ROOT / path).resolve()
    for root in _allowed_roots():
        if candidate == root or root in candidate.parents:
            return candidate
    raise ValueError(f"path {path!r} is outside the allowed roots {settings.fs_roots}")
