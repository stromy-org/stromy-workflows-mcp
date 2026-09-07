#!/usr/bin/env bash
# premerge-verify.sh — is this tree self-consistent?
#
# `stromy-org/scripts/premerge_review.py` runs this against the MATERIALIZED
# MERGE TREE — not against the PR branch — and holds the merge when it exits
# non-zero. That distinction is the entire reason the file exists.
#
# The class it catches: git merges by hunk, and hunk-level success is not
# semantic success. A branch cut a week ago that regenerated a derived artifact
# — CLAUDE.md, .github/copilot-instructions.md, the rendered MCP configs, the
# skill stubs, uv.lock — from a source `main` has since changed will merge
# CLEANLY and land a rendering that matches nothing. Nothing conflicts, nothing
# is red on the branch, and the drift is only visible once it is on `main`.
# Running the generators' own `--check` modes against the merged tree is what
# turns that silence into a refusal.
#
# Note what that means here specifically: NONE of the three `--check` gates
# below runs in this repo's CI. They are local-only conveniences that nothing
# enforces, so the merge seam is the first and only place the merged tree's
# derived artifacts get checked at all.
#
# The contract for any repo adopting this convention:
#   * executable, at exactly `scripts/premerge-verify.sh`
#   * exit 0 = the merged tree is self-consistent; non-zero = hold the merge
#   * READ-ONLY with respect to everything outside its own tree. It runs in a
#     throwaway detached worktree, so writing `.venv/` inside that tree is fine,
#     but it must never push, merge, write elsewhere, or reach the network for
#     anything but a read. A verifier with side effects would be running them
#     against a tree that may never exist.
#   * bounded — premerge_review.py kills it at 900s and records the timeout as
#     a failure.
#
# EVERY gate runs, even after one fails. A verifier that stops at its first red
# just moves the red one step to the right, costing a whole extra review cycle
# per additional problem it was holding behind the first.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

rc=0
gate() {
  local name="$1"
  shift
  local out
  if out="$("$@" 2>&1)"; then
    printf '  OK   %s\n' "$name"
  else
    rc=1
    printf '  FAIL %s\n' "$name"
    printf '%s\n' "$out" | tail -n 30 | sed 's/^/       /'
  fi
}

# A satellite scaffolded before one of these generators existed simply does not
# have it, and nothing in its CI runs it either. Skip rather than fail: a gate
# that is red on arrival for reasons the branch did not cause gets routed
# around, which is the one thing this whole mechanism cannot afford. The skip is
# printed, because a silent skip and a pass must never look the same.
gen_gate() {
  local name="$1" script="$2"
  shift 2
  if [ ! -f "$script" ]; then
    printf '  SKIP %s (%s is not present in this repo)\n' "$name" "$script"
    return
  fi
  gate "$name" python3 "$script" "$@"
}

echo "premerge-verify: stromy-workflows-mcp"

# ---- derived artifacts: regenerate-and-compare, writing nothing ------------
gen_gate "AGENTS.md -> CLAUDE.md + copilot-instructions" \
  scripts/render-agent-md.py --check
gen_gate ".agents/mcp.json -> rendered MCP configs" \
  scripts/render-mcp.py --check
gen_gate "hosted skills -> .claude/skills stubs" \
  scripts/sync_skill_stubs.py --server stromy-workflows-mcp-http --check

# ---- the toolchain half ----------------------------------------------------
# No `uv` means this tree cannot be evaluated, which is a HOLD, not a pass. An
# unprovable merge and a proven-good one must never print the same verdict.
if ! command -v uv >/dev/null 2>&1; then
  echo "  FAIL uv is not installed — this tree cannot be verified, so the merge is held"
  exit 1
fi

gate "pyproject.toml -> uv.lock" uv lock --check
gate "dependencies resolve" uv sync --frozen

# `-m` is deliberately NOT passed here: a command-line marker expression
# REPLACES pyproject's `addopts` rather than adding to it, so writing
# `-m "not live"` would silently drop every other addopt (the coverage floor
# included) while looking more careful than this line does.
gate "test suite against the merged tree" uv run pytest -q

exit $rc
