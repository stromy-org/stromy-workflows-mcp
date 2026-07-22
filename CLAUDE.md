<!--
  GENERATED FILE — DO NOT EDIT.
  Source of truth: AGENTS.md (cross-vendor standard).
  Override file:   .agent-overrides/claude.md (optional, appended below)
  Regenerate with: scripts/render-agent-md.py
-->

# Stromy Workflows MCP

Hosted workflow discovery, validation, execution, and lifecycle facade for Stromy

> **AGENTS.md is the canonical instruction file** for this repo (cross-vendor standard).
> `CLAUDE.md` and `.github/copilot-instructions.md` are generated from this file by
> `scripts/render-agent-md.py`. Gemini CLI reads this file directly via
> `context.fileName: ["AGENTS.md"]` in `.gemini/settings.json`. **Do not hand-edit
> the generated files.**

## Commands

```bash
uv sync                        # install dependencies
uv run python -m stromy_workflows_mcp.server    # run the server (HTTP on :8000)
uv run pytest                  # run tests
uv run ruff check              # lint
uv run pyright                 # strict type check
python3 scripts/sync_contracts.py --source-root ../../Stromy --check
uv add <package>               # add a dependency
uv run python scripts/sync_skill_stubs.py --server stromy-workflows-mcp-http  # regen PRE-MANIFEST local stubs (org-owned stubs are left as-is)
uv run python scripts/sync_skill_stubs.py --server stromy-workflows-mcp-http --check  # local check: exit 1 if a pre-manifest stub is stale (NOT run in CI)
```

## Layout

This repo follows the **three-zone ownership model** (ORG-PLAN-086): CHROME
files are template-owned — `copier update` overwrites them, so never hand-edit
them; INDIVIDUALITY files are yours — Copier never clobbers them. `ownership.yml`
in the `fastmcp-template` repo is the authoritative split.

```
src/stromy_workflows_mcp/server.py          CHROME — FastMCP entrypoint (`mcp`); do not edit, use server_hooks.py
src/stromy_workflows_mcp/server_hooks.py    YOURS — register(mcp): custom routes/startup/providers (§3 seam)
src/stromy_workflows_mcp/config_base.py     CHROME — framework SettingsBase
src/stromy_workflows_mcp/config.py          YOURS — Settings(SettingsBase); add domain settings
src/stromy_workflows_mcp/auth.py            CHROME — OAuth provider builder (Azure / Entra ID)

components/tools/      @tool functions — auto-discovered, no registration (YOURS, except fs_tools.py)
components/resources/  @resource functions
components/prompts/    @prompt functions
skills/                Skill directories — served to any client via the fs_read/fs_list tools
scripts/               CHROME — framework tooling (sync_skill_stubs.py, render-*.py)
tests/test_server.py   YOURS — in-memory Client(transport=mcp) domain tests
tests/test_chrome.py   CHROME — framework-contract regression tests (fs path-jail, fs_read/fs_list)
tests/test_auth.py     CHROME — OAuth provider builder tests

```

## Workflow facade invariants

- This MCP is client-agnostic. Client identity comes only from verified Entra
  app roles (`client.<slug>` or `operator`), never from tool arguments alone.
- Stromy owns the run-registry schema. This repo issues DML only and must never
  introduce schema DDL. `/health` fails on an unsupported `schema_meta.version`.
- Caller config is persisted in Postgres and must never enter the ACA Job start
  template. The template carries server-controlled values plus `--run-id` only.
- Contracts are authored in Stromy and generated into
  `components/resources/contracts/` by `scripts/sync_contracts.py`; never edit
  the generated JSON files by hand.
- Resume replays the exact `job_template_json` stored at run creation.

## Adding a component

Drop a `.py` file into the right `components/` subdirectory — `FileSystemProvider` picks it up automatically:

```python
from fastmcp.tools import tool   # or: resources.resource / prompts.prompt

@tool
def my_tool(param: str) -> str:
    """Description shown to the MCP client."""
    return param
```

No import in `server.py` needed. Type-hint everything; docstring becomes the schema description.

**Stick to these three buckets.** Anything the running server reads at runtime — JSON schemas, prompt fragments, lookup tables, fixtures — belongs **inside** `components/` (usually under `components/resources/`), not in a new top-level dir. The Dockerfile copies `src/`, `components/`, `skills/` and nothing else; a bespoke top-level `templates/` or `data/` dir will silently ship empty in production. The same data, exposed as an `@resource("scheme://path")`, becomes both deployable-by-default *and* discoverable by the LLM via `ReadMcpResourceTool`. See [`references/component-shapes.md`](references/component-shapes.md) for the decision table, the worked deliverable-canvas incident, and the related "forgiving tool design for LLM clients" pattern.

## Adding a skill

Create a subdirectory under `skills/` with a `SKILL.md` file:

```
skills/
└── my-skill/
    ├── SKILL.md          # Required — main instruction file (frontmatter: name, description)
    └── references/       # Optional — supporting files, read via fs_read("skills/<name>/references/<file>")
```

Skills are served through the generic `fs_read` / `fs_list` **tools**, not MCP resources, so they reach every client (most hosts ignore resources). Clients discover skills via `fs_list("skills")` and load them via `fs_read("skills/<name>/SKILL.md")`. No registration needed — the tools serve any file under the configured `fs_roots` (default `["skills"]`).

## Config

Add settings to `src/stromy_workflows_mcp/config.py` as `Settings` fields — it subclasses the chrome `SettingsBase` (framework transport/host/log/oauth fields), so your fields sit alongside those. Document new vars in `.env.example` **below the `# END framework env` marker** (the block above it is template-managed). Never read `os.environ` directly inside components.

## Project wiring (server_hooks.py)

Anything that must touch the `mcp` object directly — custom Starlette routes, startup/shutdown handlers, extra providers — goes in `src/stromy_workflows_mcp/server_hooks.py`'s `register(mcp)` function, **not** in the chrome `server.py`. Ordinary tools/resources/prompts need no wiring (FileSystemProvider auto-discovers them).

## Conventions

- Python 3.13, `uv` for all operations
- ruff: line-length 100, rules `E,F,I,UP,ASYNC,B,PERF,S`
- `MCP_DEV_MODE=true` enables hot-reload during development

## Agent-md & MCP rendering

This repo treats `AGENTS.md` and (optionally) `.agents/mcp.json` as the only authored sources. Run:

```bash
python scripts/render-agent-md.py            # CLAUDE.md + .github/copilot-instructions.md
python scripts/render-agent-md.py --check    # exit 1 if stale
python scripts/render-mcp.py                 # .mcp.json + .gemini/settings.json mcpServers + .codex/config.toml + .vscode/mcp.json
python scripts/render-mcp.py --check         # exit 1 if stale
```

**Never hand-edit** `CLAUDE.md`, `.github/copilot-instructions.md`, or any of the four per-agent MCP files — they all carry a "GENERATED FILE" banner; edits are wiped on next render.


## OAuth (Microsoft Entra ID)

`src/stromy_workflows_mcp/auth.py` builds an `AzureProvider` when `OAUTH_ENABLE=true`. Required env vars: `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, `OAUTH_TENANT_ID`, `OAUTH_BASE_URL`, `OAUTH_REQUIRED_SCOPES`. Auth only applies to HTTP/SSE transports — stdio bypasses it. See README "OAuth (Microsoft Entra ID)" for setup.


## Deployment

Production runs on Azure Container Apps. The deploy path is:
- `Dockerfile` (multi-stage; runtime is `python:3.13-slim` + `uv sync --frozen`)
- `.github/workflows/deploy-aca.yml` builds on push to `main`, pushes to `ghcr.io/<owner>/<repo>`, then runs `az containerapp update`
- ACA pulls the new image and rolls the revision
- Readiness probe hits `GET /health`; `server_hooks.py` replaces the template's
  generic route with a registry schema/version check
- Production transport is `streamable-http` on port 8080; `min-replicas=0` (scale-to-zero)

Infrastructure provisioning: run `bash scripts/register-terraform.sh` to open a PR in `stromy-org/terraform` that adds `mcp-servers/stromy-workflows-mcp.json`. See `azure_aca/README.md` for details. Don't invent a different deploy path; extend this one.
