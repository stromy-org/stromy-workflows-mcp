#!/usr/bin/env bash
# Opens a PR in stromy-org/terraform to provision Azure infrastructure for this MCP server.
# Clones the terraform repo into a temp dir, adds one mcp-servers/<server>.json fragment, creates a PR, cleans up.
set -euo pipefail

PACKAGE_SLUG="stromy-workflows-mcp"
GITHUB_REPO="stromy-workflows-mcp"
ENABLE_OAUTH="true"
AZURE_REGION="northeurope"
# Must match default_location in stromy-org/terraform's terraform.tfvars. When AZURE_REGION
# equals this, location is omitted from the fragment so the server inherits the terraform default.
DEFAULT_REGION="westeurope"
TERRAFORM_REPO="stromy-org/terraform"
BRANCH="add-mcp/${PACKAGE_SLUG}"
FRAGMENT_DIR="mcp-servers"
FRAGMENT_FILE="${PACKAGE_SLUG}.json"

# -- Preflight -------------------------------------------------
if ! command -v gh &>/dev/null; then
  echo "ERROR: gh CLI not found. Install it: https://cli.github.com" >&2
  exit 1
fi

if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found. Install Python 3 to write ${FRAGMENT_DIR}/${FRAGMENT_FILE}." >&2
  exit 1
fi

if ! gh auth status &>/dev/null; then
  echo "ERROR: gh CLI not authenticated. Run: gh auth login" >&2
  exit 1
fi

# -- Check for existing PR ------------------------------------
EXISTING_PR=$(gh pr list --repo "$TERRAFORM_REPO" --head "$BRANCH" --state open --json url --jq '.[0].url // empty' 2>/dev/null || true)
if [ -n "$EXISTING_PR" ]; then
  echo "PR already exists: $EXISTING_PR"
  exit 0
fi

# -- Clone into temp dir --------------------------------------
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "Cloning $TERRAFORM_REPO..."
gh repo clone "$TERRAFORM_REPO" "$TMPDIR/terraform" -- --depth 1 --quiet

FRAGMENT_PATH="$TMPDIR/terraform/$FRAGMENT_DIR/$FRAGMENT_FILE"
TFVARS_PATH="$TMPDIR/terraform/terraform.tfvars"

# -- Check if entry already exists -----------------------------
if [ -f "$FRAGMENT_PATH" ]; then
  echo "${PACKAGE_SLUG} already has ${FRAGMENT_DIR}/${FRAGMENT_FILE} -- nothing to do."
  exit 0
fi

if [ -f "$TFVARS_PATH" ] && grep -q "\"${PACKAGE_SLUG}\"[[:space:]]*=" "$TFVARS_PATH"; then
  echo "${PACKAGE_SLUG} is already defined manually in terraform.tfvars -- nothing to do."
  exit 0
fi

# -- Write mcp-servers/<server>.json ---------------------------
mkdir -p "$TMPDIR/terraform/$FRAGMENT_DIR"

python3 - "$FRAGMENT_PATH" "$PACKAGE_SLUG" "$GITHUB_REPO" "$ENABLE_OAUTH" "$AZURE_REGION" "$DEFAULT_REGION" <<'PY_WRITE_FRAGMENT'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
slug, github_repo, enable_oauth, azure_region, default_region = sys.argv[2:]

# Current stromy-org/terraform fragment schema (post ORG-PLAN-064 P1):
# oauth_app manages the Entra app registration, oauth_runtime wires
# OAUTH_ENABLE + the client secret into the container. The old
# enable_oauth/dedicated_infra keys are REJECTED at plan time. Every server
# gets a dedicated ACA environment by default (env_host opts into sharing).
entry = {}
if enable_oauth == "true":
    entry["oauth_app"] = True
    entry["oauth_runtime"] = True
    # Durable OAuth sessions via a persistent Azure Files home mount — no Redis,
    # no app code, no extra secrets (ORG-PLAN-073). BENIGN under the cost policy.
    entry["oauth_sessions"] = "files"
if azure_region != default_region:
    entry["location"] = azure_region
if github_repo != slug:
    entry["github_repo"] = github_repo

with path.open("w", encoding="utf-8") as handle:
    json.dump(entry, handle, indent=2)
    handle.write("\n")
PY_WRITE_FRAGMENT

# -- Commit, push, create PR ----------------------------------
git -C "$TMPDIR/terraform" checkout -b "$BRANCH"
git -C "$TMPDIR/terraform" add "$FRAGMENT_DIR/$FRAGMENT_FILE"
git -C "$TMPDIR/terraform" commit -m "feat: provision infrastructure for ${PACKAGE_SLUG}"

echo "Pushing branch $BRANCH..."
git -C "$TMPDIR/terraform" push -u origin "$BRANCH" --quiet

PR_URL=$(gh pr create --repo "$TERRAFORM_REPO" \
  --head "$BRANCH" \
  --title "feat: provision infrastructure for ${PACKAGE_SLUG}" \
  --body "$(cat <<EOF
## Summary

Adds \`${PACKAGE_SLUG}\` as \`${FRAGMENT_DIR}/${FRAGMENT_FILE}\`.

**Config:**
- \`github_repo\`: \`${GITHUB_REPO}\`
- \`oauth_app\` / \`oauth_runtime\`: \`${ENABLE_OAUTH}\`
$([ "$AZURE_REGION" != "$DEFAULT_REGION" ] && echo "- \`location\`: \`${AZURE_REGION}\`" || echo "- \`location\`: default (\`${DEFAULT_REGION}\`)")

Provisioned by \`scripts/register-terraform.sh\` from the generated MCP project.

After merge, a maintainer dispatches the apply (Actions → Terraform → Run
workflow — merges never apply by themselves). A new scale-to-zero server is
BENIGN under the cost policy, so a plain dispatch suffices. It provisions:
- Resource group \`rg-${PACKAGE_SLUG}\`
- Dedicated ACA environment + Log Analytics workspace (\`${PACKAGE_SLUG}-env\` / \`${PACKAGE_SLUG}-workspace\`)
- Container App (scale-to-zero) + OIDC deploy identity for GitHub Actions
- GitHub Actions variables on \`stromy-org/${GITHUB_REPO}\`
$([ "$ENABLE_OAUTH" = "true" ] && echo "- OAuth app registration with \`mcp.access\` scope (+ runtime wiring)")
EOF
)")

echo ""
echo "PR created: $PR_URL"

if [ "${REGISTER_TERRAFORM_AUTO_MERGE:-false}" = "true" ]; then
  gh pr merge --auto --squash "$PR_URL" --repo "$TERRAFORM_REPO" 2>/dev/null && \
    echo "Auto-merge enabled; will merge after checks pass." || \
    echo "Note: auto-merge not available (enable branch protection with required reviews)."
else
  echo "Auto-merge skipped. Set REGISTER_TERRAFORM_AUTO_MERGE=true to enable it."
fi
