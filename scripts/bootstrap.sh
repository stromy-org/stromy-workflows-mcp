#!/usr/bin/env bash
# Post-copy bootstrap: initialize the repo, push it to GitHub, and open the
# infrastructure-provisioning PR on stromy-org/terraform.
#
# Runs as a Copier post-copy task on INITIAL SCAFFOLD ONLY, and ONLY when
# BOOTSTRAP_ALLOW_PUSH=1 is exported (the explicit opt-in that makes `copier
# update`/`recopy` — including their throwaway temp renders — never push). It
# also no-ops when a .git directory already exists.
#
# After this runs, the terraform PR provisions Azure infra (resource group,
# Container App, OIDC + OAuth app registrations, and the AZURE_* GitHub Actions
# variables that the deploy workflow needs). Until that PR merges, the first
# deploy-aca run will fail for lack of those variables — expected. Once infra is
# live, re-push to main to deploy.
set -euo pipefail

GITHUB_OWNER="stromy-org"
GITHUB_REPO="stromy-workflows-mcp"
REPO_SLUG="${GITHUB_OWNER}/${GITHUB_REPO}"

# -- Guard: initial scaffold only -----------------------------
# Detect an existing git repo robustly. `.git` is a DIRECTORY in a normal repo
# but a FILE (gitlink) when this project is a submodule / worktree — so the old
# `[ -d .git ]` check missed the submodule case and let `copier update` re-init,
# auto-commit, and push (clobbering evolved src + deps). `[ -e .git ]` catches
# both; the rev-parse is a belt-and-suspenders for nested work-tree layouts.
if [ -e .git ] || git rev-parse --git-dir >/dev/null 2>&1; then
  echo "bootstrap: existing git repo detected -- skipping git init / push / register (copier update)."
  exit 0
fi

# -- Guard: explicit push opt-in (temp-dir push protection, §5) ------
# On `copier update`/`recopy`, Copier renders the OLD and NEW template into fresh
# temp dirs (no .git) to compute the diff, and runs this script there too. The
# `.git` guard above misses those temp dirs (they have no repo), and
# `_copier_operation` is "copy" inside them (they ARE copies), so neither
# distinguishes a genuine initial scaffold from an update's throwaway render.
# The only robust signal is an explicit opt-in: push + terraform-register ONLY
# when BOOTSTRAP_ALLOW_PUSH=1 is exported by the operator/scaffold skill on the
# real initial `copier copy`. No copier-internal temp render ever sets it, so
# update/recopy can never push. Set it on initial scaffold:
#     BOOTSTRAP_ALLOW_PUSH=1 uvx copier copy --trust gh:stromy-org/... <dst>
# or run `BOOTSTRAP_ALLOW_PUSH=1 bash scripts/bootstrap.sh` later.
if [ "${BOOTSTRAP_ALLOW_PUSH:-}" != "1" ]; then
  echo "bootstrap: BOOTSTRAP_ALLOW_PUSH != 1 -- skipping git init / push / register." >&2
  echo "          Initial scaffold: BOOTSTRAP_ALLOW_PUSH=1 bash scripts/bootstrap.sh" >&2
  exit 0
fi

# -- Preflight -------------------------------------------------
if ! command -v git &>/dev/null; then
  echo "bootstrap: git not found -- skipping repo init. Run scripts/bootstrap.sh manually later." >&2
  exit 0
fi
if ! command -v gh &>/dev/null; then
  echo "bootstrap: gh CLI not found -- skipping push + terraform registration." >&2
  echo "          Install https://cli.github.com, then: gh repo create ${REPO_SLUG} --private --source=. --push && bash scripts/register-terraform.sh" >&2
  exit 0
fi
if ! gh auth status &>/dev/null; then
  echo "bootstrap: gh CLI not authenticated -- skipping push + terraform registration." >&2
  echo "          Run 'gh auth login', then: gh repo create ${REPO_SLUG} --private --source=. --push && bash scripts/register-terraform.sh" >&2
  exit 0
fi

# -- Initialize git --------------------------------------------
echo "bootstrap: initializing git repository..."
git init --quiet
git add -A
git commit --quiet -m "feat: ✨ Initial scaffold from fastmcp-template"

# -- Create + push GitHub repo ---------------------------------
if gh repo view "$REPO_SLUG" &>/dev/null; then
  echo "bootstrap: ${REPO_SLUG} already exists on GitHub -- pushing to it."
  git remote get-url origin &>/dev/null || git remote add origin "https://github.com/${REPO_SLUG}.git"
  git branch -M main
  git push -u origin main --quiet
else
  echo "bootstrap: creating ${REPO_SLUG} and pushing..."
  gh repo create "$REPO_SLUG" --private --source=. --remote=origin --push
fi

# Pushing main triggers deploy-aca.yml. The first run is EXPECTED to fail until
# the terraform PR (below) merges and provisions the AZURE_* variables.
echo "bootstrap: pushed main. The first deploy-aca run will fail until infra is provisioned (expected)."

# -- Register Azure infrastructure -----------------------------
echo "bootstrap: opening infrastructure PR on stromy-org/terraform..."
REGISTER_TERRAFORM_AUTO_MERGE=true bash scripts/register-terraform.sh

echo ""
echo "bootstrap: done. Once the terraform PR merges and infra is live, re-push to main to deploy:"
echo "           git commit --allow-empty -m 'chore: trigger deploy' && git push"
