---
name: server-guide
_server_only: true
description: Overview of the Stromy Workflows MCP MCP server — capabilities, connection, and usage.
---

# Stromy Workflows MCP

Hosted workflow discovery, validation, execution, and lifecycle facade for Stromy

## Capabilities

This server exposes MCP **tools**, **resources**, and **prompts** via FastMCP 3.0.

| Type | Discovery | Access |
|------|-----------|--------|
| Tools | `list_tools()` | call by name |
| Resources | `list_resources()` | `data://`, custom schemes |
| Prompts | `list_prompts()` | get by name |
| Skills | `fs_list("skills")` tool | `fs_read("skills/<name>/SKILL.md")` tool |

Skills are served through the generic `fs_read` / `fs_list` tools rather than MCP
resources, so they reach every client (the Claude app, ChatGPT, etc.) — not only
clients that surface resources.

## Connecting

```bash
# stdio (local)
uv run python -m stromy_workflows_mcp.server

# HTTP
FASTMCP_TRANSPORT=http uv run python -m stromy_workflows_mcp.server
# → http://127.0.0.1:8000/mcp/
```

## Adding skills

Drop a new directory into `skills/` with a `SKILL.md` file:

```
skills/
└── my-skill/
    ├── SKILL.md          # Required — main instruction file
    └── references/       # Optional — supporting material
```

No registration needed — the `fs_read` / `fs_list` tools serve any file under
`skills/`. Discover skills with `fs_list("skills")`, then load one with
`fs_read("skills/<name>/SKILL.md")`.

<!-- cold-start:start -->
## If this server is slow to respond

This server scales to zero to save cost, so the first call after an idle period wakes the container — typically ~10–30s, and up to ~1–2 min for a heavier image (media / browser tier). If `fs_read` or a tool errors with unavailable/timeout:

1. Tell the user the server is starting, then retry the same call — the call itself wakes the container.
2. Retry with a short backoff up to ~3 times.
3. Only if it is still unreachable after retries, STOP and report. Never downgrade to a local or base skill just to "get something out".
<!-- cold-start:end -->
