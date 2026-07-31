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
