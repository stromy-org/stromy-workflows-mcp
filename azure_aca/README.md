# Azure Container Apps — Stromy Workflows MCP

Infrastructure for this MCP server is managed by the org's centralized Terraform repo (`stromy-org/terraform`).

## Adding this server to Terraform

**Automated (recommended):**

```bash
bash scripts/register-terraform.sh
```

This clones `stromy-org/terraform`, adds `mcp-servers/stromy-workflows-mcp.json`, opens a PR, and cleans up the clone. On merge, the Terraform Apply workflow provisions all resources automatically.

**Manual alternative:**

Add one generated server fragment at `terraform/mcp-servers/stromy-workflows-mcp.json`:

```json
{
  "oauth_app": true,
  "oauth_runtime": true,
  "oauth_sessions": "files"
}
```

`"oauth_sessions": "files"` provisions a persistent **Azure Files** share mounted
at FastMCP's home path so OAuth sessions survive restarts / scale-to-zero — no
Redis, no app code, no extra secrets (ORG-PLAN-073). It is **BENIGN** under the
cost policy (a per-use file share). FastMCP derives its signing/encryption keys
from the stable client secret, so nothing else is needed; see `auth.py`.


Then open a PR on `stromy-org/terraform`, review the plan comment, merge, and
dispatch the apply (Actions → Terraform → Run workflow — merges never apply by
themselves; a new scale-to-zero server is BENIGN under the cost policy).

This creates:
- Resource group `rg-stromy-workflows-mcp`
- A **dedicated** ACA environment + Log Analytics workspace for this server (`stromy-workflows-mcp-env` / `stromy-workflows-mcp-workspace`)
- Container App in that environment (scale-to-zero, port 8080)
  - KEDA cool-down defaults to **28800s (8h)** — a scale-to-zero server stays warm across a working day after first use, then scales to 0. Override per server with `"cooldown_period": <seconds>` in the Terraform fragment.
- OIDC app registration for GitHub Actions CI/CD
- GitHub Actions variables (`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`)
- OAuth app registration with `mcp.access` scope and client secret

## Deployment workflow

On push to `main`, `.github/workflows/deploy-aca.yml`:
1. Builds Docker image and pushes to GHCR
2. Authenticates to Azure via OIDC (using GitHub vars set by Terraform)
3. Updates the Container App with the new image

```bash
git push origin main
# Wait for GitHub Actions to complete, then:
curl https://<app-fqdn>/health
```

## GHCR registry access

GHCR registry credentials are managed centrally by Terraform via the `GHCR_PAT` secret in `stromy-org/terraform`. No per-repo setup is needed — every Container App gets credentials automatically on `terraform apply`.

If you need to troubleshoot image pull failures:

```bash
# Verify registry is configured
az containerapp registry list --name stromy-workflows-mcp --resource-group rg-stromy-workflows-mcp

# Manual override (only if Terraform credentials are missing)
az containerapp registry set \
  --name stromy-workflows-mcp \
  --resource-group rg-stromy-workflows-mcp \
  --server ghcr.io \
  --username stromy-org \
  --password $GHCR_PAT
```


## OAuth setup (local development)

After `terraform apply`, retrieve OAuth credentials:

```bash
cd <path-to-terraform-repo>
terraform output -json oauth_clients
```

Add to your local `.env`:

```
OAUTH_ENABLE=true
OAUTH_CLIENT_ID=<client_id from output>
OAUTH_CLIENT_SECRET=<from terraform output -raw>
OAUTH_TENANT_ID=${TENANT_ID}
OAUTH_BASE_URL=http://localhost:8000
OAUTH_REQUIRED_SCOPES=mcp.access
```

For production, set these as Container App secrets:

```bash
az containerapp secret set \
  --name stromy-workflows-mcp --resource-group rg-stromy-workflows-mcp \
  --secrets oauth-client-secret="<secret>"

az containerapp update \
  --name stromy-workflows-mcp --resource-group rg-stromy-workflows-mcp \
  --set-env-vars \
    OAUTH_ENABLE=true \
    OAUTH_CLIENT_ID="<client_id>" \
    OAUTH_TENANT_ID="<tenant_id>" \
    OAUTH_BASE_URL="https://<app-fqdn>" \
    OAUTH_REQUIRED_SCOPES=mcp.access \
    OAUTH_CLIENT_SECRET=secretref:oauth-client-secret
```


## Troubleshooting provisioning

### Capacity errors (`ManagedEnvironmentCapacityHeavyUsageError` / `AKSCapacityHeavyUsage`)

The Terraform apply can fail with a transient Azure regional capacity error while creating the ACA environment. This is an Azure-side outage, not a config problem. Azure may leave a partially-created resource behind that Terraform doesn't have in state. The apply workflow reports any matching `terraform import` commands, but state mutation stays manual. Review the failed job output in `stromy-org/terraform`, import intentionally if needed, then re-run the apply job; if the region is still constrained, wait and re-run later.

If a region is persistently capacity-constrained, set an explicit `location` for this server in `mcp-servers/stromy-workflows-mcp.json` (e.g. `northeurope`, `germanywestcentral`):

```json
{
  "location": "northeurope"
}
```

Note: changing the region of an already-provisioned server forces replacement of its environment and container app (FQDN changes).

## Cost

With `minReplicas: 0` and Consumption plan, idle cost is ~$0. Set `minReplicas: 1` for always-on (~$5/mo).
