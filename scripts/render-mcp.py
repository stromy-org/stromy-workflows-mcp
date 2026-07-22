#!/usr/bin/env python3
"""Render per-agent MCP configuration files from `.agents/mcp.json`.

Canonical source of truth: ``.agents/mcp.json`` (agent-neutral, rich schema).
Targets (rendered, do not hand-edit):

    .mcp.json                       — Claude Code
    .gemini/settings.json mcpServers — Gemini CLI (preserves other keys)
    .codex/config.toml [mcp_servers.*] — Codex CLI (preserves non-MCP content)
    .vscode/mcp.json                — VS Code MCP extension (same shape as Claude)

Schema honoured (per-server fields beyond standard MCP):

    "_consumed_by": ["claude", "codex", "gemini"]
        Gate the server per CLI. VS Code inherits Claude. Omit a CLI to hide.
    "_codex_via": "mcp-remote"
        For HTTP/SSE servers, render Codex as a stdio wrapper around
        `npx mcp-remote <url>` so OAuth flow goes through mcp-remote's
        token cache (~/.mcp-auth/) instead of Codex's rmcp client.
    "_gemini_via": "mcp-remote"
        Same idea for Gemini — useful when Gemini's strict OAuth resource-URI
        validation rejects a FastMCP server's discovery response.
    "_notes": "..."
        Free-form prose, ignored by renderer.

Codex-only fields preserved in TOML output:
    "startup_timeout_sec": int

Secret discipline: every value in env/headers/url that looks like a secret
(matches password|token|secret|api[_-]?key|auth|bearer) must be a `${VAR}`
reference. Literal values exit 2.

Codex nuance: `[mcp_servers.*].env` does not interpolate `${VAR}` placeholders.
For Codex only, renderer drops env entries whose values are `${VAR}` refs so the
server inherits the already-exported parent-shell environment instead of
receiving the literal placeholder string.

Usage:
    render-mcp.py             # write all four files (skip files that don't apply)
    render-mcp.py --check     # exit 1 if any output differs from canonical
    render-mcp.py --dry-run   # show what would be written
    render-mcp.py --repo PATH # operate against PATH instead of cwd

Exit codes:
    0 — success or up-to-date
    1 — drift detected (--check mode)
    2 — literal secret in source, or invalid schema
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GENERATED_BANNER = (
    "Rendered from .agents/mcp.json by scripts/render-mcp.py "
    "— DO NOT EDIT BY HAND. Edit .agents/mcp.json and re-run."
)

TOML_BEGIN_MARKER = "# BEGIN:RENDERED-MCP — managed by scripts/render-mcp.py"
TOML_END_MARKER = "# END:RENDERED-MCP"

SECRET_KEY_RE = re.compile(r"(password|token|secret|api[_-]?key|auth|bearer)", re.IGNORECASE)
ENV_REF_RE = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*(?::-[^}]*)?\}$")

# Codex-specific fields preserved in the TOML output.
CODEX_EXTRA_FIELDS = ("startup_timeout_sec",)


# ── Schema validation ────────────────────────────────────────────────────────


def validate_secrets(servers: dict[str, dict]) -> list[str]:
    """Return a list of error messages for any literal secrets found."""
    errors: list[str] = []
    for name, spec in servers.items():
        for section in ("env", "headers"):
            for key, value in (spec.get(section) or {}).items():
                if not isinstance(value, str):
                    continue
                if SECRET_KEY_RE.search(key) and not ENV_REF_RE.match(value):
                    errors.append(
                        f"{name}.{section}.{key}: literal value "
                        f"'{value[:8]}…' — must be ${{ENV_VAR}}"
                    )
        url = spec.get("url")
        if isinstance(url, str) and "://" in url:
            # URLs themselves are not secrets, but inline tokens are. Quick heuristic:
            if any(part in url.lower() for part in ("token=", "key=", "secret=")):
                if "${" not in url:
                    errors.append(f"{name}.url: literal credential in URL")
    return errors


def filter_for(cli: str, servers: dict[str, dict]) -> dict[str, dict]:
    """Return only servers whose _consumed_by includes `cli`."""
    out = {}
    for name, spec in servers.items():
        consumed = spec.get("_consumed_by", ["claude", "codex", "gemini"])
        if cli in consumed:
            out[name] = spec
    return out


# ── Per-CLI shape transforms ─────────────────────────────────────────────────


def _stdio_payload(spec: dict, *, include_codex_extras: bool = False) -> dict:
    """Common stdio fields shared across CLI shapes."""
    out: dict = {"command": spec["command"]}
    if "args" in spec:
        out["args"] = list(spec["args"])
    if "env" in spec and spec["env"]:
        out["env"] = dict(spec["env"])
    if include_codex_extras:
        for field in CODEX_EXTRA_FIELDS:
            if field in spec:
                out[field] = spec[field]
    return out


def _http_payload_claude(spec: dict) -> dict:
    """Claude Code HTTP server shape."""
    out: dict = {"type": "http", "url": spec["url"]}
    if "headers" in spec:
        out["headers"] = dict(spec["headers"])
    return out


def _http_payload_gemini(spec: dict) -> dict:
    """Gemini CLI HTTP server shape (uses `httpUrl`)."""
    # Gemini uses `httpUrl` for streamable HTTP and `url` for SSE.
    key = "url" if spec.get("transport") == "sse" else "httpUrl"
    out: dict = {key: spec["url"]}
    if "headers" in spec:
        out["headers"] = dict(spec["headers"])
    return out


def _mcp_remote_wrapper(spec: dict) -> dict:
    """Build a stdio shape that wraps an HTTP server via `npx mcp-remote`."""
    return {"command": "npx", "args": ["mcp-remote", spec["url"]]}


def _codex_env_payload(spec: dict) -> dict | None:
    """Return only Codex-safe env entries.

    Codex currently passes `${VAR}` strings through literally in `[mcp_servers.*].env`
    instead of interpolating them. Rendering those placeholders would override a
    correctly exported shell environment with unusable literal values such as
    `${ODOO_TIMEOUT_SECONDS}`. Keep literal env values like PATH overrides, but
    let `${VAR}` refs inherit from the parent process.
    """
    env = spec.get("env") or {}
    if not env:
        return None

    rendered = {
        key: value
        for key, value in env.items()
        if not (isinstance(value, str) and ENV_REF_RE.match(value))
    }
    return rendered or None


def render_for_claude(servers: dict[str, dict]) -> dict:
    out: dict[str, dict] = {}
    for name, spec in filter_for("claude", servers).items():
        if spec.get("transport") in (None, "stdio"):
            out[name] = _stdio_payload(spec)
        else:
            out[name] = _http_payload_claude(spec)
    return out


def render_for_gemini(servers: dict[str, dict]) -> dict:
    out: dict[str, dict] = {}
    for name, spec in filter_for("gemini", servers).items():
        if spec.get("_gemini_via") == "mcp-remote":
            out[name] = _mcp_remote_wrapper(spec)
        elif spec.get("transport") in (None, "stdio"):
            out[name] = _stdio_payload(spec)
        else:
            out[name] = _http_payload_gemini(spec)
    return out


def render_for_codex(servers: dict[str, dict]) -> dict:
    out: dict[str, dict] = {}
    for name, spec in filter_for("codex", servers).items():
        if spec.get("_codex_via") == "mcp-remote":
            payload = _mcp_remote_wrapper(spec)
            for field in CODEX_EXTRA_FIELDS:
                if field in spec:
                    payload[field] = spec[field]
            out[name] = payload
        elif spec.get("transport") in (None, "stdio"):
            payload = _stdio_payload(spec, include_codex_extras=True)
            env = _codex_env_payload(spec)
            if env:
                payload["env"] = env
            else:
                payload.pop("env", None)
            out[name] = payload
        else:
            # Codex 0.125+ supports native HTTP via [mcp_servers.<name>] http_url
            out[name] = {"url": spec["url"]}
            for field in CODEX_EXTRA_FIELDS:
                if field in spec:
                    out[name][field] = spec[field]
    return out


def render_for_vscode(servers: dict[str, dict]) -> dict:
    """VS Code inherits Claude's set and uses the same shape."""
    return render_for_claude(servers)


# ── File writers ─────────────────────────────────────────────────────────────


def write_mcp_json(path: Path, servers: dict[str, dict]) -> str:
    """Render the Claude `.mcp.json` file."""
    body = {
        "_comment": GENERATED_BANNER,
        "mcpServers": render_for_claude(servers),
    }
    return json.dumps(body, indent=2) + "\n"


def write_vscode_mcp_json(path: Path, servers: dict[str, dict]) -> str:
    """Render the VS Code `.vscode/mcp.json` file."""
    body = {
        "_comment": GENERATED_BANNER,
        "servers": render_for_vscode(servers),
    }
    return json.dumps(body, indent=2) + "\n"


def write_gemini_settings(path: Path, servers: dict[str, dict]) -> str:
    """Render `.gemini/settings.json`, preserving non-mcpServers keys."""
    rendered = render_for_gemini(servers)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}: invalid JSON ({exc})") from exc
    else:
        existing = {}
    # Drop the stale comment key (any variation) and rewrite a fresh one.
    for stale in ("_generated_mcpServers", "_comment_mcpServers"):
        existing.pop(stale, None)
    # Insert our keys in a stable order: keep existing top-level keys, then
    # our managed block. Use dict-key reorder via reconstruction.
    out = {k: v for k, v in existing.items() if k != "mcpServers"}
    out["_comment_mcpServers"] = GENERATED_BANNER
    out["mcpServers"] = rendered
    return json.dumps(out, indent=2) + "\n"


def _toml_escape(value: str) -> str:
    """Minimal TOML basic-string escape."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(value) -> str:
    """Render a Python value as TOML."""
    if isinstance(value, str):
        return f'"{_toml_escape(value)}"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        # Inline table
        body = ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items())
        return "{ " + body + " }"
    raise TypeError(f"unsupported TOML type: {type(value).__name__}")


def _toml_render_servers(servers: dict[str, dict]) -> str:
    """Render servers as TOML [mcp_servers.<name>] tables."""
    blocks: list[str] = []
    for name, payload in servers.items():
        lines = [f"[mcp_servers.{name}]"]
        for key, value in payload.items():
            lines.append(f"{key} = {_toml_value(value)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def write_codex_config(path: Path, servers: dict[str, dict]) -> str:
    """Render Codex MCP block inside `.codex/config.toml`, preserving the rest."""
    rendered_block = _toml_render_servers(render_for_codex(servers))
    managed = (
        f"{TOML_BEGIN_MARKER}\n"
        f"# {GENERATED_BANNER}\n"
        f"{rendered_block}\n"
        f"{TOML_END_MARKER}\n"
    )

    if not path.exists():
        return (
            "#:schema https://developers.openai.com/codex/config-schema.json\n\n"
            "# Codex project configuration\n\n"
            f"{managed}"
        )

    text = path.read_text()
    # Strip any prior managed block (with either old or new markers).
    text = re.sub(
        r"^# BEGIN:(?:RENDERED|GENERATED)-MCP.*?^# END:(?:RENDERED|GENERATED)-MCP\n?",
        "",
        text,
        flags=re.DOTALL | re.MULTILINE,
    )
    text = text.rstrip() + "\n\n" + managed
    return text


# ── Drift / apply driver ─────────────────────────────────────────────────────


TARGETS = [
    (".mcp.json", write_mcp_json),
    (".vscode/mcp.json", write_vscode_mcp_json),
    (".gemini/settings.json", write_gemini_settings),
    (".codex/config.toml", write_codex_config),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Render per-agent MCP config files from .agents/mcp.json",
    )
    ap.add_argument("--repo", type=Path, default=Path.cwd(), help="repo root (default: cwd)")
    ap.add_argument("--check", action="store_true", help="exit 1 on drift, write nothing")
    ap.add_argument("--dry-run", action="store_true", help="print what would change")
    args = ap.parse_args(argv)

    repo = args.repo.resolve()
    source = repo / ".agents" / "mcp.json"
    if not source.exists():
        print(f"{source.relative_to(repo)}: not found — nothing to render")
        return 0

    try:
        data = json.loads(source.read_text())
    except json.JSONDecodeError as exc:
        print(f"{source}: invalid JSON ({exc})", file=sys.stderr)
        return 2

    servers = data.get("servers") or {}
    if not servers:
        print(f"{source}: no servers declared")
        return 0

    errors = validate_secrets(servers)
    if errors:
        print(f"{source}: literal secrets detected", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 2

    drift = 0
    for rel, writer in TARGETS:
        path = repo / rel
        # Skip a target if its parent dir is absent AND the file doesn't exist —
        # e.g., a repo without VS Code shouldn't get .vscode/mcp.json forced on
        # it. But render unconditionally if the file already exists (matches
        # existing convention).
        if not path.exists() and not path.parent.exists():
            continue
        rendered = writer(path, servers)
        current = path.read_text() if path.exists() else ""
        if rendered == current:
            continue
        drift += 1
        if args.check:
            print(f"DRIFT  {rel}")
            continue
        if args.dry_run:
            print(f"would write {rel} ({len(rendered)} bytes)")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered)
        print(f"wrote  {rel}")

    if args.check:
        if drift:
            print(f"\n{drift} file(s) stale — run `python scripts/render-mcp.py`")
            return 1
        print("all MCP outputs up-to-date")
        return 0

    if drift == 0:
        print("all MCP outputs up-to-date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
