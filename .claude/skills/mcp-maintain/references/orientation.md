# Orientation — Stromy Workflows MCP repo conventions

Repo-specific conventions for this FastMCP project: layout, the component
auto-discovery contract, config/env wiring, the test pattern, logging, and a
post-generation bootstrap checklist. For FastMCP **API** questions (auth,
advanced decorators, transports, Inspector), defer to the `fastmcp` skill — or
`WebFetch https://gofastmcp.com/llms.txt` for the docs index.

---

## 1. The discovery contract (most important)

`src/stromy_workflows_mcp/server.py` wires `FileSystemProvider(COMPONENTS_DIR)` into
`FastMCP` — it **scans `components/` automatically**. Skills under `skills/` are
served separately through the generic `fs_read` / `fs_list` **tools** (in
`components/tools/fs_tools.py`), so they reach every client — not only clients
that surface MCP resources.

This means:

- **No registry, no import in `server.py`.** The provider scans `components/` on
  startup. You do **not** add anything to `server.py` when you add a component.
- **Adding a component = dropping a `.py` file** into the right `components/`
  subdirectory with the right standalone decorator (see §2). Nothing else.
- **Adding a skill = creating a directory** in `skills/` with a `SKILL.md`. The
  `fs_read` / `fs_list` tools serve it immediately — no registration.
- **Filename is free** — `stripe_payments.py`, `fetch_orders.py` — any valid
  module name works.
- Hot-reload during development: set `MCP_DEV_MODE=true` in `.env` and the
  component provider detects changes without a restart.

---

## 2. Adding components — standalone decorators

Components are discovered as standalone module-level functions. Import the
decorator from `fastmcp`, **not** from the server instance — `@mcp.tool` will
**not** be discovered by `FileSystemProvider` and silently fails to register.

### Tool — `components/tools/<verb>_<noun>.py`
```python
from fastmcp.tools import tool

@tool
def my_tool(param: str, count: int = 1) -> str:
    """One-line description — becomes the tool description in the MCP schema."""
    return param * count
```

### Resource — `components/resources/<noun>.py`
```python
from fastmcp.resources import resource

@resource("data://my-resource")
def my_resource() -> str:
    """Description of what this resource provides."""
    return "resource content"
```

### Prompt — `components/prompts/<noun>.py`
```python
from fastmcp.prompts import prompt

@prompt
def my_prompt(context: str, style: str = "concise") -> str:
    """Description of what this prompt template generates."""
    return f"You are a {style} assistant. Context: {context}"
```

**Rules:**
- Type-hint every parameter — FastMCP derives the JSON schema from hints.
- The docstring becomes the description visible to the MCP client.
- One component per file is preferred; multiple are allowed if tightly related.
- For async tools, use `async def` — FastMCP supports both.
- Use resources (not a bespoke top-level dir) for any static runtime data — see
  the SKILL.md "Add a resource" workflow and `component-shapes.md`.

---

## 3. Configuration & environment

`src/stromy_workflows_mcp/config.py` uses `pydantic-settings`:

```python
class Settings(BaseSettings):
    fastmcp_transport: str = "http"   # "http" or "stdio"
    fastmcp_port: int = 8000
    mcp_dev_mode: bool = False
    log_level: str = "INFO"
    # add your own settings here
```

Rules:
- **Add new settings as fields on `Settings`**, not as bare `os.environ` reads.
- **Document new vars in `.env.example`** in the same change.
- Settings are read once at import time via `settings = Settings()` — no dynamic
  reload.
- Env vars map to field names (uppercase, underscores): `MCP_DEV_MODE=true` →
  `settings.mcp_dev_mode`.

---

## 4. Testing pattern

`pytest-asyncio` is pre-configured with `asyncio_mode = "auto"`. Use the
in-memory transport — no network or process required:

```python
import pytest
from fastmcp.client import Client
from stromy_workflows_mcp.server import mcp

@pytest.fixture
async def client():
    async with Client(transport=mcp) as c:
        yield c

async def test_my_tool(client):
    result = await client.call_tool(name="my_tool", arguments={"param": "hello"})
    assert result.data == "hello"
```

Run with: `uv run pytest`

**For any tool that returns a list, table, or long text, this in-memory client
call is also where you assert the size contract** — see §4a. A test that stops at
`len(json.dumps(envelope))` measures a string the client never receives.

---

## 4a. Response budgeting (ORG-PLAN-221)

**The budget is the complete MCP-visible text, and the server owns it.** A row
cap says how many rows the caller wanted; it says nothing about how wide they
are, nor about the enclosing list, envelope, warnings, metadata, or FastMCP's own
serialization. On 2026-08-25 a tool returned exactly its documented
`max_results=200` and a claude.ai surface rejected the 70,681-character result;
the truncation warning said "or raise max_results", the model did, and the retry
was 179 KB.

`src/stromy_workflows_mcp/response_budget.py` ships with this scaffold:

```python
from stromy_workflows_mcp.response_budget import fit_json_result

def build_payload(prefix, budget_meta, warning):
    """Called repeatedly by the fitter — keep it pure, and put EVERYTHING the
    client pays for inside it (fixed metadata, pre-existing warnings, coverage)."""
    return {
        "results": list(prefix),
        "returned_count": len(prefix),
        "warnings": [warning] if warning else [],
        "metadata": {"coverage": {...}, "response_budget": budget_meta},
    }

fitted = fit_json_result(
    matched,
    build_payload=build_payload,
    narrowing_hint="Filter by state or period, or page with metadata.coverage.next_offset.",
)
return fitted.payload
```

Defaults: **40,000 content characters**, hard ceiling 60,000 for a named
`response_format="complete"` mode. Never expose the budget as a caller parameter.

Four things that are easy to get wrong:

- **Wide cross-tabs need an encoding change, not just a cap.** Repeating a
  `{code, label}` pair per dimension per row cost 49,847 tokens for a cube that
  encodes to 7,657 as rows-as-tuples + a legend + hoisted constants. Hoisting
  constants alone only reaches ~54% — measured, and not sufficient.
- **Default to the aggregate the question needs.** A totals margin answers the
  real question in ~490 tokens; full detail stays behind an explicit parameter.
- **Truncation names the NARROWING move.** `assert_narrowing_grammar` rejects
  "raise the cap"/"raise max_results" phrasing at authoring time.
- **Binary and bulk output goes by reference** (sha256 handle, confined local
  path, or expiring URL), never inline because the decoded bytes looked small —
  base64 inflates ~4/3 before the envelope is added.

Full standard, calibration matrix and the `table/v1` schema:
`infra-docs/ai/mcp-response-budgeting.md` in stromy-org.

---

## 5. Logging & observability

All logs are structured JSON lines on stdout. Azure Container Apps forwards them
to the Log Analytics workspace automatically.

- `src/stromy_workflows_mcp/logging.py` configures a `JSONFormatter` on the root
  logger at import time (called from `server.py`).
- `src/stromy_workflows_mcp/middleware.py` contains `ToolCallLoggingMiddleware` — a
  FastMCP `Middleware` subclass logging every tool call.
- Each entry includes: `timestamp`, `tool` name, `input` arguments, `user_email`
  (from the OAuth token when enabled, else `"anonymous"`), `client_slug`
  (the canonical client key, read defensively from `brand_context.client_slug`;
  `"unknown"` for a tool that takes no `brand_context` — see ORG-PLAN-046),
  `duration_ms`, and `status` (`ok` / `error`).
- Log level is controlled by `LOG_LEVEL` (default `INFO`).

### Querying in Azure
```kql
ContainerAppConsoleLogs_CL
| where Log_s contains "tool_call"
| extend parsed = parse_json(Log_s)
| project TimeGenerated, tool=parsed.tool, client=parsed.client_slug, user=parsed.user_email, duration=parsed.duration_ms, status=parsed.status
```

Per-client tool mix (the usage-intel pattern — `stromy-org/analytics/kql/`):
```kql
ContainerAppConsoleLogs_CL
| where Log_s has "tool_call"
| extend p = parse_json(Log_s)
| summarize calls = count() by client = tostring(p.client_slug), tool = tostring(p.tool)
```

### Adding custom log fields
```python
logger.info("custom event", extra={"json_fields": {"key": "value"}})
```
The `JSONFormatter` merges `json_fields` into the output.

---

## 6. Post-generation bootstrap (just ran `copier copy`)

1. **Read the user's intent** — what business logic should the server expose.
2. **Delete the example files** in `components/tools/` (keep `fs_tools.py`),
   `components/resources/`, `components/prompts/`, and `skills/server-guide/` —
   they are placeholders only.
3. **Drop in new components** following §2.
4. **Generate skill stubs** for any skills you added (see the SKILL.md "Add a
   skill" workflow): `python3 scripts/sync_skill_stubs.py`.
5. **Verify:**
   ```bash
   uv run pytest                                   # smoke tests
   uv run python -m stromy_workflows_mcp.server       # starts the server
   ```
   Replace the echo smoke test in `tests/test_server.py` with tests for the new
   components.

---

## 7. Repo conventions cheatsheet

| Topic | Convention |
|-------|-----------|
| Python version | 3.13 (see `.python-version`) |
| Package manager | `uv` — use `uv run`, `uv sync`, `uv add` |
| Linter | ruff, line-length 100 |
| Test runner | pytest, in-memory `Client(transport=mcp)` |
| Default transport | HTTP on `http://127.0.0.1:8000/mcp/` |
| Stdio transport | Set `FASTMCP_TRANSPORT=stdio` in `.env` or override in `.mcp.json` |
| Claude Code registration | `.mcp.json` registers both stdio and http variants |
| Dev hot-reload | `MCP_DEV_MODE=true` in `.env` |
| Production | `streamable-http` on `:8080` (Dockerfile); `/health` is the ACA liveness probe |
