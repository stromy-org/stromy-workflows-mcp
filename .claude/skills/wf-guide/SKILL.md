---
name: wf-guide
description: "Explain and route the Stromy hosted workflow service: what runs remotely, which workflow skill to use, how configuration review works, why the workspace connector is required, and how paused or long-running jobs return results. Use whenever a client asks how hosted analyses work, what the Stromy Workflows connector does, or which wf-* skill to choose."
---
<!--
  GENERATED FILE — DO NOT EDIT.
  Owner:       scripts/sync-mcp-skill-stubs.py (via sync-on-mcp-skill-change.yml)
  Source:      MCPs/stromy-workflows-mcp/skills/wf-guide/SKILL.md
  This workflow pushes DIRECT to this repo's main — a local edit here will be
  overwritten or rejected non-fast-forward. Edit the source, push, then:
    gh workflow run sync-on-mcp-skill-change.yml -R stromy-org/stromy-org
  Hand-authored skill? Set `_local: true` in frontmatter instead.
-->

# Stromy Workflows guide (MCP-hosted skill)

This skill's full instructions are hosted on the `stromy-workflows` MCP server. Do not hardcode workflow logic locally — always fetch the live version from the MCP.

## Before you start — this skill needs the `stromy-workflows` connector

This skill's instructions live on the `stromy-workflows` MCP, which reaches you as an **authorized connector** rather than as part of the plugin. Before step 1 below, check whether this conversation actually has the `stromy-workflows` MCP's tools (`fs_read` / `fs_list`) available to call.

**If those tools are not present at all, STOP — and do not retry.** A missing tool is not a slow server: it means the connector is either not added to this workspace or not switched on for this conversation. Retrying cannot fix it. Tell the user plainly what to do, naming the connector:

> This needs the **Stromy Workflows** connector, which isn't switched on for this chat. Open your connector settings, check it's connected and enabled for this conversation, then ask me again.

Then stop and wait. Never fall back to a local or identically-named base skill, and never answer from your own knowledge instead — an unsourced answer is **wrong output, not a fallback**.

## Loading instructions

1. Read the main skill instructions:
   → call the `fs_read` tool on the `stromy-workflows` MCP with `path="skills/wf-guide/SKILL.md"`.

2. Discover reference files (and any other skill assets), then read on demand:
   → call `fs_list` with `path="skills/wf-guide"` (and `path="skills/wf-guide/references"`),
   → call `fs_read` with `path="skills/wf-guide/references/<file>"`.

Follow the instructions returned by the MCP exactly.

## This MCP is the only correct path

Produce this skill's output **only** by following the live SKILL.md fetched above and calling the `stromy-workflows` MCP's own tools. Do **not** substitute a local or identically-named base skill from elsewhere, and do **not** invent your own output path. A locally-produced or unbranded artifact is **wrong output, not a fallback** — it bypasses the server-side brand and quality gates.
