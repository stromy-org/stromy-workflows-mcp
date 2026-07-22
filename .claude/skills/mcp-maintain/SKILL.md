---
name: mcp-maintain
description: "Maintain the Stromy Workflows MCP FastMCP server — add/modify tools, resources, prompts, and skills; import source from another repo; promote skills from workspace-studio; regenerate skill stubs; bump version; redeploy. Use whenever the user wants to extend, modify, or maintain this MCP server — even if they don't say 'MCP' explicitly. Triggers on phrases like 'add a tool', 'add a resource', 'add a prompt', 'add a skill', 'import skill from workspace-studio', 'import source', 'expose this skill', 'regenerate stubs', 'redeploy MCP', 'update the server', 'pull template improvements', 'sync the template', or any request to modify this MCP's behavior."
---

# MCP Maintain — Stromy Workflows MCP

Source of truth for maintaining the **Stromy Workflows MCP** MCP server. This skill is shipped by the `fastmcp-template` Copier template — the same content lives in every MCP satellite and in `stromy-org/.claude/skills/mcp-maintain/` (org mirror, propagated by `sync-maintainer-skills.sh`).

## When to use

- Adding or modifying tools, resources, prompts, or skills in this MCP.
- Importing source code or skills from another repo (workspace-studio, prototype, ad-hoc working copy).
- Promoting a skill from workspace-studio to canonical here.
- Regenerating local skill stubs after a skill change.
- Bumping version, redeploying, or refreshing CI config.

## When NOT to use

- Creating a new MCP from scratch → org-level `repo-scaffold mcp-server <slug>`.
- Pure FastMCP-API questions ("how does FastMCP do X?") → the global `fastmcp` skill.

## Repo layout (recap)

```
Stromy Workflows MCP/
├── AGENTS.md                   # Source of truth (CLAUDE.md + copilot instructions generated)
├── pyproject.toml
├── fastmcp.json                # Server config
├── src/
│   └── stromy_workflows_mcp/
│       └── server.py           # FastMCP() instance + registration
├── components/                 # FileSystemProvider auto-discovers this tree
│   ├── tools/                  # @tool functions (from fastmcp.tools import tool)
│   ├── resources/              # @resource functions
│   └── prompts/                # @prompt functions
├── skills/                     # Source-of-truth skills (full content)
│   └── <skill>/SKILL.md
├── .claude/skills/             # Auto-generated stubs of `skills/` (do not edit)
├── tests/
├── azure_aca/                  # Azure Container Apps deploy
└── scripts/
    ├── render-agent-md.py      # AGENTS.md → CLAUDE.md / .github/copilot-instructions.md
    └── sync_skill_stubs.py     # skills/ → .claude/skills/ (stub generator)
```

## Workflows

> **Discovery contract.** `server.py` wires `FileSystemProvider(components/)`,
> which scans `components/` automatically. You do **not** import or register
> components in `server.py` — dropping the file is enough. Import the decorator
> from `fastmcp` (`from fastmcp.tools import tool`), **not** from the server
> instance: `@mcp.tool` is not discovered and silently fails to register. Full
> orientation (layout, config, testing, logging, conventions): [`references/orientation.md`](references/orientation.md).

### Add a tool

1. Create `components/tools/<verb>_<noun>.py` defining one function.
2. `from fastmcp.tools import tool`, decorate with `@tool`, add a docstring (FastMCP uses it as the tool description).
3. Add a test in `tests/test_tools.py`.
4. Run `uv run pytest -k <tool>`. (No `server.py` edit — `FileSystemProvider` discovers it.)

### Add a resource

1. Create `components/resources/<noun>.py`: `from fastmcp.resources import resource`, then `@resource("scheme://path/{param}")`.
2. Verify discovery with the in-memory client test pattern (`async with Client(transport=mcp) as c: await c.list_resources()`) — see [`references/orientation.md`](references/orientation.md) §4. No `server.py` edit needed.

**Use resources for any static data the server needs at runtime** — JSON schemas, lookup tables, prompt fragments, section templates, fixtures. Do NOT invent a top-level data directory; the Dockerfile and CI only know about `src/`, `components/`, `skills/`. If the data is purely internal (the LLM should never read it), still place the files under `components/resources/<subdir>/` (no decorator) so the standard `COPY components/` ships them. See [`fastmcp-template/references/component-shapes.md`](https://github.com/stromy-org/stromy-org/blob/main/scaffolds/fastmcp-template/references/component-shapes.md) for the decision table and a worked incident.

### Add a prompt

1. Create `components/prompts/<noun>.py`: `from fastmcp.prompts import prompt`, then `@prompt`. No `server.py` edit — `FileSystemProvider` discovers it.

### Add a skill (authored here)

1. Create `skills/<skill-slug>/SKILL.md` with required frontmatter (`name`, `description`).
2. Add `references/` and `scripts/` as needed.
3. Regenerate local stubs: `python3 scripts/sync_skill_stubs.py`.
4. **Mandatory** — add an entry to `stromy-org/sync-manifest.json` `mcp-skill-mirrors` for this skill. Every MCP-hosted skill must appear in the manifest so it auto-mirrors as a stub into workspace-studio (the integration testbed). The `plugins` array can be empty if no plugin consumes the skill yet — the manifest entry still ensures the workspace-studio mirror is generated. Skip the workspace-studio mirror only for genuinely client-specific variants by setting `"mirror_to_cowork": false` (document the exception in `AGENTS.md` per the three-layer-tooling rule).
5. From `stromy-org/`, run `python3 scripts/sync-mcp-skill-stubs.py` to materialise the workspace-studio stub + any plugin stubs.

### Import a skill from workspace-studio (promotion)

> **Rendering decision gate (do this FIRST for artifact-producing skills).** If
> the skill renders a binary artifact (pptx/docx/pdf/image/video) via a local
> build step needing a heavy toolchain (Playwright/Chromium, LibreOffice, Sharp,
> system fonts), do NOT promote the build scripts as plugin-local scripts the
> agent runs. The rendering MUST become an MCP `render_*` **tool** (bytes + sha256
> out, `brand_context` dict + asset bytes in, any brand/correctness contract
> enforced inside the tool + returned as an audit). A local build path silently
> downgrades when the client sandbox (Cowork) lacks the toolchain — the
> stromy-format Calibri-masquerade failure. See `infra-docs/ai/three-layer-tooling.md`
> § "Skill→MCP conversion: when rendering must move server-side" and the
> `ORG-PLAN-010` worked example. The SKILL.md you promote should author HTML and
> call the render tool, not run a build script.

1. Identify the source: `workspace-studio/.claude/skills/<name>/`.
2. Copy the full skill into `skills/<name>/` (full content, not a stub).
3. Add the manifest entry in `stromy-org/sync-manifest.json` (see step 4 above) — this immediately wipes the workspace-studio full-copy and replaces it with a stub on the next sync, so there's no need to add a "promoted to" banner anymore.
4. Regenerate the MCP's local stubs: `python3 scripts/sync_skill_stubs.py`.
5. From `stromy-org/`, run `python3 scripts/sync-mcp-skill-stubs.py` — overwrites the old workspace-studio full-copy with a stub.

### Import source from another repo or working copy

1. Confirm with the user which files to import and from where.
2. Copy into the right locations (`src/stromy_workflows_mcp/`, `components/`, `tests/`).
3. Adjust imports — every module under `src/stromy_workflows_mcp/` and `components/` uses absolute imports rooted at the `stromy_workflows_mcp` package.
4. Run `uv sync && uv run ruff check && uv run pyright && uv run pytest`.
5. Iterate until the test suite passes; do NOT skip failures.

### Regenerate local skill stubs

```bash
python3 scripts/sync_skill_stubs.py
```

Generated stubs live at `.claude/skills/<skill>/SKILL.md`. They are short wrappers that tell the agent to fetch the live SKILL.md via the `fs_read` tool on the `stromy-workflows-mcp-http` MCP with `path="skills/<name>/SKILL.md"`. Never hand-edit them.

> **Ownership boundary — this script only touches PRE-MANIFEST stubs.** Once a
> skill has a `sync-manifest.json` entry in `stromy-org`, its stub is **owned by
> the org-level pipeline** (`scripts/sync-mcp-skill-stubs.py`, run by
> `sync-on-mcp-skill-change.yml`), which pushes a richer stub — ownership banner,
> anti-fallback and cold-start sections — straight to this repo's `main`. This
> local script **detects those org-owned stubs (by their `GENERATED FILE — DO NOT
> EDIT.` banner) and skips them**, so running it is safe and never downgrades
> them. Do not try to "fix" an org-owned stub here or force it into the local
> format — regenerate it via the org pipeline (`python3
> scripts/sync-mcp-skill-stubs.py` from `stromy-org`, or `gh workflow run
> sync-on-mcp-skill-change.yml`). A local `--check` that reports an org-owned
> stub as up to date / skipped is correct, not drift.

### Bump version & redeploy

1. Update `version` in `pyproject.toml`.
2. Update `CHANGELOG.md` (if present).
3. Commit via `/conventional-commit`.
4. Push — CI (`.github/workflows/deploy-aca.yml`) builds the container, pushes to GHCR, deploys to Azure Container Apps, and runs a `/health` check (see `azure_aca/`). Infra itself is provisioned via `scripts/register-terraform.sh` (a PR on `stromy-org/terraform`), not from this repo.

### Live-state changes (env vars, secrets, Entra permissions, rollback)

For changes that do NOT require a new image (env var flips, OAuth scope updates, secret rotation, revision rollback, Entra App Registration permission grants), follow the named operations in [`references/aca-runbook.md`](references/aca-runbook.md). Each operation has explicit input requirements, step ordering, verification commands, and rollback procedure — designed so an agent run can execute them without back-and-forth once the matching `az` commands are pre-authorized in `.claude/settings.json`.

### Pull template improvements

Bring this satellite current with `fastmcp-template` HEAD — the mode that keeps the fleet from drifting behind template fixes (new CI invariants, workflow hardening, orientation updates).

The template separates **CHROME** (template-owned, safe to overwrite) from **INDIVIDUALITY** (project-owned, never touched) per the ownership manifest `ownership.yml` (three-zone model, ORG-PLAN-086 — see `infra-docs/ai/mcp-fleet-maintenance.md`). A clean `copier update` only ever rewrites chrome; your `config.py`, `server_hooks.py`, `components/`, deps, and `version` are protected by `_skip_if_exists` / the stable-anchor pyproject region. So the conflict rule below applies to the residual customized-chrome only.

1. From the satellite root: `uvx copier update --trust --skip-answered`. Your recorded answers are kept; only files the template changed since your `.copier-answers.yml` `_commit` are 3-way merged.
2. **Review the diff hunk-by-hunk.** The conflict rule: **evolved server source wins, template chrome wins.** Never let a template merge regress this MCP's `src/` logic, tools, or tests; on `.github/workflows/`, lint/type config, agent-surface files, and `.claude/skills/` maintainer chrome, take the template side. Drop any hunk that touches a generated overlay you don't own.
3. Resolve every `*.rej` by hand, then delete all `.rej` files (`find . -name '*.rej'` must come back empty).
4. Run the quality stack before committing — `uv run pytest`, plus `ruff` and `pyright` per this repo's CI (`.github/workflows/`).
5. Commit the update through the repository commit workflow (`/conventional-commit`), on `main`, never on a detached HEAD.
6. **Deploy awareness.** Pushing this satellite auto-triggers CI → GHCR → ACA redeploy (`.github/workflows/deploy-aca.yml`). A **chrome-only** diff (workflows, config, skills, docs) is a low-risk push. Any diff touching `src/`, the `Dockerfile`, transport/auth, or secret wiring is a **risky change**: surface it and confirm before pushing the submodule.

**Far-diverged? Re-baseline with `recopy` as a reviewed PR.** When `copier update` produces a near-total rewrite — the recorded `_commit` is unreachable (rebased/gone) or the layout era is misaligned (a flat-`src` / FastMCP-2 repo) — do NOT fight the 3-way. Run a one-time `uvx copier recopy --vcs-ref <tag>` in an **isolated worktree/branch as a PR** (take a checkpoint first — recopy overwrites the tree before you reconcile), hand-restore every individuality file, and gate the PR on *no dep dropped / no version downgrade / tests green* before merge. Afterwards the repo is a normal Tier-1 `update` consumer. Full decision rule + runbook: `infra-docs/ai/mcp-fleet-maintenance.md`.

## Critical rules

- **Never edit `.claude/skills/<skill>/SKILL.md` directly.** It's generated. Edit `skills/<skill>/SKILL.md` and regenerate.
- **Never edit generated agent-instruction files directly.** They are generated from `AGENTS.md`. Edit AGENTS.md and run `python3 scripts/render-agent-md.py`.
- **Never bypass `pyproject.toml` for dependencies** — use `uv add <pkg>` not bare `pip install`.
- **Tests are non-negotiable.** No commit that breaks `uv run pytest` lands on main.
- **Promotion is one-way.** Once a skill lives canonically in this MCP, workspace-studio's copy is read-only.
- **Stick to scaffold-defined directories.** Runtime assets go inside `src/`, `components/`, or `skills/`. A bespoke top-level dir (e.g. `templates/`, `data/`, `schemas/`) silently ships empty in production because the scaffold's Dockerfile doesn't copy it. Reshape to a `@resource` under `components/resources/` instead — see [component-shapes.md](https://github.com/stromy-org/stromy-org/blob/main/scaffolds/fastmcp-template/references/component-shapes.md).
- **Design tools to be forgiving toward LLM clients.** Default to upsert over `NotFound`, return valid enum options in error strings, make finalize/delete idempotent. The LLM cannot recover from strict 4xx the way a human programmer can. Pattern + rationale in the same reference doc.

## Reference

- Repo orientation (layout, discovery contract, config, testing, logging, conventions, post-generation bootstrap): [`references/orientation.md`](references/orientation.md)
- Generic FastMCP API: `/fastmcp` skill
- Sync manifest schema: `stromy-org/sync-manifest.json` (top-level)
- Template source-of-truth: `stromy-org/scaffolds/fastmcp-template/template/skills/mcp-maintain/SKILL.md.jinja`
