# Stromy Workflows MCP

Hosted workflow discovery, validation, execution, and lifecycle facade for Stromy

Built with [FastMCP 3.0](https://gofastmcp.com) and managed with [uv](https://docs.astral.sh/uv/).

## Setup

```bash
uv sync
cp .env.example .env
```

## Run

```bash
# stdio (default)
uv run python -m stromy_workflows_mcp.server

# Or via the FastMCP CLI (reads fastmcp.json):
uv run fastmcp run
uv run fastmcp dev      # with the Inspector UI
```

HTTP transport is enabled by default — the server listens on `http://127.0.0.1:8000/mcp/`.

## Project layout

```
src/stromy_workflows_mcp/server.py        FastMCP server entrypoint (instance: `mcp`)
src/stromy_workflows_mcp/config.py        Settings via pydantic-settings (reads .env)
components/
├── tools/                 @tool functions, auto-discovered
├── resources/             @resource functions, auto-discovered
└── prompts/               @prompt functions, auto-discovered
skills/
└── server-guide/          Example skill, served via the fs_read/fs_list tools
tests/                     pytest + in-memory FastMCP Client
```

Components are loaded by `FileSystemProvider`. Drop a new `.py` file into any
subdirectory of `components/` with a standalone `@tool` / `@resource` /
`@prompt` decorator — no registration required. Set `MCP_DEV_MODE=true`
in `.env` to enable hot-reload during development.

### Skills

The `skills/` directory is served through the generic `fs_read` / `fs_list`
**tools** rather than MCP resources, so skills reach every client (the Claude
app, ChatGPT, etc.) — not only clients that surface resources. Discover skills
with `fs_list("skills")` and load one with `fs_read("skills/<name>/SKILL.md")`.
The tools are jailed to the configured `fs_roots` (default `["skills"]`); paths
that escape via `..`, absolute paths, or symlinks are rejected.

Drop a new folder into `skills/` with a `SKILL.md` — no registration needed.

## Tests

```bash
uv run pytest
```

## Use with Claude Code

The included `.mcp.json` registers this server as `stromy-workflows-mcp` for any
Claude Code session opened in this directory.


## OAuth (Microsoft Entra ID)

This server supports optional OAuth authentication via Microsoft Entra ID. When enabled, HTTP/SSE clients must authenticate via browser-based Azure login before accessing MCP tools. Stdio transport is unaffected.

### Azure App Registration

1. Go to **Azure Portal → App registrations → New registration**
2. Name: `stromy-workflows-mcp-oauth`
3. Supported account types: **Single tenant** (Accounts in this organizational directory only)
4. Redirect URI: **Web** → `http://localhost:8000/auth/callback` (update for production)
5. After creation, go to **Authentication** and ensure **Access tokens** and **ID tokens** are checked under "Implicit grant and hybrid flows"

#### Token version

Go to **Manifest** and set `"accessTokenAcceptedVersion": 2` (required by FastMCP's AzureProvider).

#### Expose an API

1. Go to **Expose an API** → Set Application ID URI (accept default `api://<client-id>`)
2. **Add a scope**: `mcp.access` — "Access MCP server" — Admins and users

#### Client secret

1. Go to **Certificates & secrets** → **New client secret**
2. Copy the **Value** (not the Secret ID) — this is `OAUTH_CLIENT_SECRET`

### Configuration

Fill in `.env`:

```bash
OAUTH_ENABLE=true
OAUTH_CLIENT_ID=<Application (client) ID from Overview>
OAUTH_CLIENT_SECRET=<Client secret Value>
OAUTH_TENANT_ID=<Directory (tenant) ID from Overview>
OAUTH_BASE_URL=http://localhost:8000
OAUTH_REQUIRED_SCOPES=mcp.access
```

### Production deployment on Azure Container Apps

Store secrets separately from plain env vars:

```bash
APP=stromy-workflows-mcp
RG=rg-stromy-workflows-mcp

az containerapp secret set --name $APP --resource-group $RG \
  --secrets oauth-client-secret="<your-secret>"

az containerapp update --name $APP --resource-group $RG \
  --set-env-vars \
    OAUTH_ENABLE=true \
    OAUTH_CLIENT_ID=<client-id> \
    OAUTH_TENANT_ID=<tenant-id> \
    OAUTH_BASE_URL=https://<your-app-fqdn> \
    OAUTH_REQUIRED_SCOPES=mcp.access \
    OAUTH_CLIENT_SECRET=secretref:oauth-client-secret
```

### Durable sessions

FastMCP keeps OAuth state — the JWT signing key, dynamically-registered clients, and
upstream tokens. The keys it needs (signing key, storage encryption key, storage path)
are **derived deterministically from the stable OAuth client secret**, so they already
survive restarts with no configuration. The **only** ephemeral piece is the store
*directory contents* on the container's local disk, which Azure Container Apps wipes on
every cold start / scale-to-zero — invalidating the tokens clients hold and forcing them
to re-authenticate.

The fix is **infrastructure, not app code**: a persistent **Azure Files** share mounted at
FastMCP's home path (`/home/appuser/.local/share/fastmcp`) makes the store dir survive
restarts. No Redis, no extra dependencies, no extra env vars, no extra secrets
(ORG-PLAN-073).

**On Azure this is provisioned automatically** when the server is registered with
`enable_oauth = true` — the generated terraform fragment carries `"oauth_sessions": "files"`,
which mounts the share. **BENIGN** under the cost policy (a per-use file share). Nothing to
configure by hand.

**Locally**, the default on-disk / in-memory store needs no configuration, so
`uv run pytest` and `fastmcp run` work out of the box.


## Deploy to Azure Container Apps

Production runs on [Azure Container Apps](https://learn.microsoft.com/en-us/azure/container-apps/) with scale-to-zero. CI/CD is handled by `.github/workflows/deploy-aca.yml` on every push to `main`.

See [`azure_aca/README.md`](azure_aca/README.md) for the full setup guide (automated script or manual commands).
