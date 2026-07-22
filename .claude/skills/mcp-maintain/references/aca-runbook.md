# ACA + Entra runbook — post-deploy operations

Named operations for runtime config changes to an already-deployed Azure Container Apps MCP server and its companion Entra ID App Registration. Each operation is a strict step list so a future agent run can execute it without back-and-forth.

These operations assume the infrastructure itself (resource group, Container App, OAuth app registration) was provisioned by the org's centralized Terraform repo (`stromy-org/terraform`) and the image build + deploy was handled by the GitHub Actions deploy workflow (`.github/workflows/deploy-aca.yml`). Use this runbook **only** for live state changes after that. Wherever a setting also lives in Terraform, treat the Terraform fragment (`stromy-org/terraform/mcp-servers/<name>.json` or `terraform.tfvars`) as the source of truth — a live `az` change is reverted on the next apply unless you also update Terraform.

## Inputs every operation needs

Resolve these once at the start of the session and reuse:

- **`ACA_NAME`** — Container App name (e.g. `deliverable-canvas-mcp`).
- **`ACA_RG`** — Resource group. **Verify with `az containerapp list`** — Stromy convention drifted between `rg-<name>` and `rg-<name>-ne` historically.
- **`ENTRA_DISPLAY_NAME`** — Entra App Registration display name for the OAuth client (often `mcp-<name>`, NOT the `-gha` deploy SP).
- **`ENTRA_APP_ID`** — resolve via `az ad app list --display-name "$ENTRA_DISPLAY_NAME" --query '[].appId' -o tsv`.

If `az containerapp show --name "$ACA_NAME" --resource-group "$ACA_RG"` returns `does not exist`, do NOT guess — run `az containerapp list --query "[?name=='$ACA_NAME'].{name:name, rg:resourceGroup}"` and report what you find before proceeding.

## Operation: set-scale-cooldown

Set the KEDA **cool-down period** — how long a scale-to-zero app stays warm after its last request before scaling back to 0. ACA's default is **300 s (5 min)**, which makes `min_replicas: 0` MCPs disappear almost immediately between calls and pay a cold start on the next one.

**Org standard:** every scale-to-zero MCP (`min_replicas: 0`) sets `cooldown_period = 28800` (8 h), so the server stays warm across a working day after first use, then scales to zero overnight. Always-on apps (`min_replicas: 1`) don't need it — cooldown only governs the final replica → 0 transition.

**Terraform owns this.** Cooldown is a first-class field on the `aca-mcp-server` Terraform module (`cooldown_period_in_seconds` on `azurerm_container_app`), defaulting to `28800`. There is **no live `az` operation** for it — changing it via the CLI is reverted on the next `terraform apply`. To change it, edit the server's Terraform config and let Apply roll it out.

**Inputs:** `COOLDOWN_SECONDS` (org standard `28800`; only set when overriding the default).

**Steps:**

1. **Confirm the app is actually scale-to-zero** (cooldown is a no-op otherwise):
   ```bash
   az containerapp show -n "$ACA_NAME" -g "$ACA_RG" \
     --query "properties.template.scale.minReplicas" -o tsv
   # 0 → cooldown applies. >0 → no-op; stop.
   ```
2. **Set `cooldown_period` in `stromy-org/terraform`.** The default is already `28800` for every server, so you only act here to use a *non-default* window. Add the field to the server's fragment:
   ```json
   // stromy-org/terraform/mcp-servers/<ACA_NAME>.json
   {
     "cooldown_period": 28800
   }
   ```
   Open a PR (or use `scripts/register-terraform.sh` from the MCP repo, which writes the fragment for you). After merge, dispatch the apply (Actions → Terraform → Run workflow) to roll the change out in place.
3. **Verify cooldown landed after the apply:**
   ```bash
   az containerapp show -n "$ACA_NAME" -g "$ACA_RG" \
     --query "{cooldown:properties.template.scale.cooldownPeriod, min:properties.template.scale.minReplicas, containers:length(properties.template.containers)}" -o json
   ```
   Expect your configured `cooldown`, `min: 0`, `containers: 1`.

**Rollback:** revert the `cooldown_period` change in the Terraform fragment and re-apply (omit the field to fall back to the `28800` default).

## Operation: update-env-var

Update one or more environment variables on the live Container App. Triggers a new revision; downtime is ~0 with `min-replicas` ≥ 0.

**Inputs:** one or more `KEY=VALUE` pairs.

**Steps:**

1. **Snapshot current value** (for rollback):
   ```bash
   az containerapp show --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --query "properties.template.containers[0].env[?name=='KEY']" -o json
   ```
2. **Apply update:**
   ```bash
   az containerapp update --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --set-env-vars KEY="VALUE"
   ```
3. **Verify revision provisioned:**
   ```bash
   az containerapp show --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --query '{rev:properties.latestRevisionName, state:properties.provisioningState}' -o json
   ```
   Expect `state: "Succeeded"` and a bumped `rev` (`--0000NN` suffix increments).
4. **Verify env value landed:**
   ```bash
   az containerapp show --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --query "properties.template.containers[0].env[?name=='KEY']" -o json
   ```
5. **Report:** print `KEY: <old> → <new>; revision <name>` so the audit log shows the change.

**Rollback:** rerun step 2 with the snapshot value from step 1.

**Common pitfalls:**

- Values containing spaces or `$` must be quoted at the shell level. `OAUTH_REQUIRED_SCOPES="mcp.access offline_access"` is one quoted string, not two args.
- `--set-env-vars` is upsert; existing unrelated vars are preserved. `--remove-env-vars` deletes by name.
- The Container App's secret bindings (`KEY=secretref:foo`) are NOT touched by `--set-env-vars KEY=…`; if you need to flip from secretref to plain value, do it explicitly and double-check `secrets:` block.

## Operation: add-entra-delegated-permission

Add a delegated permission (OIDC or Graph scope) to an OAuth App Registration. Useful when the consent flow needs a new scope (e.g. `offline_access` for refresh tokens).

**Inputs:** `ENTRA_APP_ID`, `RESOURCE_APP_ID` (e.g. `00000003-0000-0000-c000-000000000046` for Microsoft Graph), `PERMISSION_ID` (the scope's GUID), `PERMISSION_NAME` (the human-readable scope, e.g. `offline_access`).

**Steps:**

1. **Verify the App Registration exists and capture its current `requiredResourceAccess`:**
   ```bash
   az ad app show --id "$ENTRA_APP_ID" --query '{name:displayName, rra:requiredResourceAccess}' -o json
   ```
2. **Verify the App's service principal exists in this tenant.** Many App Registrations get created without an SP (the SP is auto-created on first user sign-in). Without an SP, `permission grant` fails:
   ```bash
   az ad sp list --filter "appId eq '$ENTRA_APP_ID'" --query '[].id' -o tsv
   ```
   If empty:
   ```bash
   az ad sp create --id "$ENTRA_APP_ID"
   ```
3. **Verify the resource API's service principal exists in this tenant.** For first-party Microsoft APIs (Graph, Exchange, etc.) the SP is usually pre-provisioned. If missing:
   ```bash
   az ad sp show --id "$RESOURCE_APP_ID" || az ad sp create --id "$RESOURCE_APP_ID"
   ```
   If the create call fails with `Property displayName is invalid` or `not subscribed to`, **stop**. The tenant lacks the necessary subscription or your account lacks privileges; surface this as a portal-only action and hand off to the user. Do not attempt workarounds.
4. **Register the permission on the app:**
   ```bash
   az ad app permission add --id "$ENTRA_APP_ID" \
     --api "$RESOURCE_APP_ID" \
     --api-permissions "$PERMISSION_ID=Scope"
   ```
   The `=Scope` suffix marks it delegated. Use `=Role` for app-only permissions.
5. **Grant (consent) the permission.** Two paths — pick the first that succeeds:
   - **User-consent grant** (works for delegated scopes the user can self-consent to):
     ```bash
     az ad app permission grant --id "$ENTRA_APP_ID" \
       --api "$RESOURCE_APP_ID" --scope "$PERMISSION_NAME"
     ```
   - **Admin consent** (required for scopes flagged "admin consent required"):
     ```bash
     az ad app permission admin-consent --id "$ENTRA_APP_ID"
     ```
6. **Verify the grant:**
   ```bash
   az ad app permission list-grants --id "$ENTRA_APP_ID" --show-resource-name
   ```

**Special case — OIDC scopes (`offline_access`, `openid`, `profile`, `email`):**

These are Microsoft identity platform OIDC scopes. AAD's `/authorize` endpoint honors them from the `scope` parameter at request time **even without** an explicit grant on the App Registration. If steps 3–5 fail because Microsoft Graph SP cannot be provisioned in the tenant, you can still proceed:

- The client (MCP server / connector) requests `offline_access` in its scope list (set via `OAUTH_REQUIRED_SCOPES` env var) → refresh tokens issue regardless.
- The `permission add` from step 4 still records intent in `requiredResourceAccess` for documentation and shows up in the portal consent UI if/when Graph SP is later provisioned.

If you take this path, **document it explicitly** in the change log: "OIDC scope added to requiredResourceAccess only; tenant cannot grant — works via OIDC built-in handling."

## Operation: fix-oauth-token-version

**Symptom this fixes.** The MCP connector completes the browser login and the
server logs `POST /token … 200 OK` + `Issued new FastMCP tokens`, but every
subsequent `/mcp` call returns `401 Unauthorized` with `Bearer token rejected for
client` (`jwt.py`), and the client shows *"Authorization with the MCP server
failed."* The token is **issued, then rejected** by the server's own JWT
validation — so it looks like a login failure but is a token-format mismatch.

**Root cause.** FastMCP's `AzureProvider` validates access tokens against the
**v2.0** Entra issuer/audience. If the App Registration issues **v1** tokens (the
default when `requestedAccessTokenVersion` is unset), the `iss`/`aud` claims don't
match and every token is rejected. The `stromy-org/terraform` OAuth module sets
`requestedAccessTokenVersion: 2` automatically when it provisions the App
Registration — a **hand-created or reused** App Registration (one not managed by
Terraform) is the usual way it ends up unset.

**Preflight check** (run before declaring OAuth ready — catches it before a user does):

```bash
az ad app show --id "$ENTRA_APP_ID" --query "api.requestedAccessTokenVersion" -o tsv
# Expect: 2.  null or 1 → tokens will be rejected; apply the fix below.
```

**Fix:**

1. Resolve the App's **object id** (the Graph PATCH needs the object id, not the appId):
   ```bash
   OID=$(az ad app show --id "$ENTRA_APP_ID" --query id -o tsv)
   ```
2. PATCH the manifest via Microsoft Graph (`az ad app update --set api.…` is
   unreliable for this nested field):
   ```bash
   az rest --method PATCH \
     --uri "https://graph.microsoft.com/v1.0/applications/$OID" \
     --headers "Content-Type=application/json" \
     --body '{"api":{"requestedAccessTokenVersion":2}}'
   ```
3. Verify the preflight check now returns `2`.
4. **Reconnect the client.** The old v1 token is cached in the connector —
   disconnect and reconnect the MCP connector so a fresh v2 token is issued. **No
   server redeploy is needed** (the change is on the Entra side, applied at the next
   token issuance).

## Operation: rotate-secret

Update an ACA secret (e.g. client secret, API key). Replaces the secret value; bindings via `secretref:` are unaffected.

**Steps:**

1. Snapshot which env vars reference the secret:
   ```bash
   az containerapp show --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --query "properties.template.containers[0].env[?contains(value, 'secretref:SECRET_NAME')]" -o json
   ```
2. Update the secret value:
   ```bash
   az containerapp secret set --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --secrets "SECRET_NAME=<new-value>"
   ```
3. Trigger a new revision so containers pick up the new secret value (`secret set` alone does NOT restart):
   ```bash
   az containerapp update --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --revision-suffix "rotate-$(date +%Y%m%d-%H%M%S)"
   ```
4. Verify new revision provisioned and healthy.

## Operation: roll-back-revision

Pin traffic to a previous good revision when the latest deploy is broken.

**Steps:**

1. List recent revisions:
   ```bash
   az containerapp revision list --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --query '[].{name:name, active:properties.active, healthy:properties.healthState, created:properties.createdTime}' -o table
   ```
2. Activate the prior good revision:
   ```bash
   az containerapp revision activate --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --revision "<prior-revision-name>"
   ```
3. Shift traffic:
   ```bash
   az containerapp ingress traffic set --name "$ACA_NAME" --resource-group "$ACA_RG" \
     --revision-weight "<prior-revision-name>=100"
   ```
4. Verify FQDN serves the rolled-back image.

## Safety rules

- **Never** run any operation on a Container App outside the project's known `ACA_NAME` + `ACA_RG`. Pre-authorized Bash rules in `.claude/settings.json` should pin both.
- **Never** delete an Entra App Registration or service principal without explicit user confirmation. Deletes break every active token.
- **Never** swallow a Microsoft Graph "Request_BadRequest" or "not subscribed to" error silently — surface it and stop. These signal a tenant config issue that needs human judgment.
- **Snapshot before mutate** is the rule for every operation. The snapshot is the rollback contract.
