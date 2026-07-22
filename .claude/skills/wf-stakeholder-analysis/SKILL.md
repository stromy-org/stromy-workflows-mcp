---
name: wf-stakeholder-analysis
description: "Run a hosted stakeholder-acceptance analysis from a client plugin: gather the decision and evidence settings, validate the safe workflow configuration, start the asynchronous run, handle questionnaire review, and return its report links. Use whenever a client asks for stakeholder mapping, acceptance analysis, resistance analysis, coalition analysis, or a stakeholder report, even if they do not mention workflows."
---
<!--
  GENERATED FILE — DO NOT EDIT.
  Owner:       scripts/sync-mcp-skill-stubs.py (via sync-on-mcp-skill-change.yml)
  Source:      MCPs/stromy-workflows-mcp/skills/wf-stakeholder-analysis/SKILL.md
  This workflow pushes DIRECT to this repo's main — a local edit here will be
  overwritten or rejected non-fast-forward. Edit the source, push, then:
    gh workflow run sync-on-mcp-skill-change.yml -R stromy-org/stromy-org
  Hand-authored skill? Set `_local: true` in frontmatter instead.
-->

# Hosted stakeholder analysis (MCP-hosted skill)

This skill's full instructions are hosted on the `stromy-workflows` MCP server. Do not hardcode workflow logic locally — always fetch the live version from the MCP.

## Loading instructions

1. Read the main skill instructions:
   → call the `fs_read` tool on the `stromy-workflows` MCP with `path="skills/wf-stakeholder-analysis/SKILL.md"`.

2. Discover reference files (and any other skill assets), then read on demand:
   → call `fs_list` with `path="skills/wf-stakeholder-analysis"` (and `path="skills/wf-stakeholder-analysis/references"`),
   → call `fs_read` with `path="skills/wf-stakeholder-analysis/references/<file>"`.

Follow the instructions returned by the MCP exactly.

## This MCP is the only correct path

Produce this skill's output **only** by following the live SKILL.md fetched above and calling the `stromy-workflows` MCP's own tools. Do **not** substitute a local or identically-named base skill from elsewhere, and do **not** invent your own output path. A locally-produced or unbranded artifact is **wrong output, not a fallback** — it bypasses the server-side brand and quality gates.

## If the `stromy-workflows` MCP is slow to respond

This server scales to zero to save cost, so the first call after an idle period wakes the container — typically ~10–30s, and up to ~1–2 min for a heavier image (media / browser tier). If `fs_read` or a tool errors with unavailable/timeout:

1. Tell the user the server is starting, then retry the same call — the call itself wakes the container.
2. Retry with a short backoff up to ~3 times.
3. Only if it is still unreachable after retries, STOP and report. Never downgrade to a local or base skill just to "get something out".
