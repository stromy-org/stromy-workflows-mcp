# Workflow entitlement — model and runbook

Who may run which hosted workflow. The architectural rationale lives in
`infra-docs/ai/workflow-execution-plane.md` § *Workflow entitlement*; this file is
the operational half — how to grant, revoke, and verify.

## Model

Four authorization layers, each answering a different question. Entitlement is the
second; the others were already in place.

| Layer | Question | Where |
|---|---|---|
| Authentication | who are you? | Entra app role → `scoping.resolve_scope` (default-deny) |
| **Workflow entitlement** | **which workflows are yours?** | `components/resources/entitlements.json` |
| Run tenancy | whose runs are these? | `service._require_run_scope` |
| Configuration tier | which fields may you set? | `x-tier` in the contract |

The registry is a join table — workflow → entitled client slugs → the client-facing
`wf-*` skill:

```json
{
  "workflows": {
    "stakeholder_analysis_workflow": {
      "clients": ["dukestrategies"],
      "skill": "wf-stakeholder-analysis",
      "note": "Duke Strategies pilot (ORG-PLAN-070)."
    }
  }
}
```

- **Default-deny.** Absent, or `"clients": []`, means operator-only.
- **A client's reach is the union of its `client.<slug>` roles**, exactly as for run
  tenancy — but `start_run` checks the *resolved run owner*, so holding an entitled
  role does not let a caller start a run owned by an unentitled one.
- **`operator` is unrestricted** and bypasses the registry before it is read.
- **This file grants nothing by itself.** The `client.<slug>` app role must already
  exist in `terraform/entra.tf` (`local.workflow_app_roles`) and be assigned to the
  person. A slug listed here with no matching role is inert.

## Runbook — granting a client a workflow

1. **Confirm the Entra role exists and is assigned.** `client.<slug>` must be in
   `local.workflow_app_roles` in `terraform/entra.tf`, with an
   `azuread_app_role_assignment` for each person. A *new* client slug means a
   terraform change — SECURITY-SENSITIVE, so it stops for an explicit operator ack
   (`/terraform-ops`). Adding an *existing* client to another workflow does not.
2. **Add the slug** to that workflow's `clients` array here. Keep `note` current —
   it is the only record of *why* the grant exists.
3. **If the client should also get the driving skill**, add the plugin to the
   `sync-manifest.json` mirror for `wf-<name>` and regenerate stubs. Invariant #19
   fails CI on the mismatch in either direction, so these must move together.
4. **Verify locally:** `uv run python scripts/check_entitlements.py`, then
   `python3 scripts/validate-plugin-completeness.py --plugin <plugin>` from
   stromy-org.
5. **PR, merge, redeploy.** `components/` is baked into the image, so the grant is
   live only after `deploy-aca.yml` runs. It is not a live flag flip.
6. **Tell the grantee to sign in fresh.** App-role changes enter the `roles` claim
   only on a new sign-in; allow a few minutes for Entra propagation. Without this
   step every grant reads as broken for ~10 minutes.

## Runbook — revoking

Remove the slug from `clients` and redeploy. The client immediately stops seeing the
workflow and cannot start new runs. **Existing runs are unaffected** — they are
governed by run tenancy (`client_slug` on the row), not by entitlement, so the client
keeps reading its own history. If the intent is to cut off history too, remove the
Entra role assignment instead. Drop the `wf-*` skill from the plugin in the same
change, or Invariant #19 fails.

## Entitlement is re-checked on a retry, not inherited

`retry_run` resolves entitlement **again**, at retry time, against the original
workflow and the run's recorded owner. So the revocation above has a sharper effect
than "cannot start new runs": a client whose entitlement was removed also cannot
*rerun* the work it used to be allowed to start, even though the parent run row is
still theirs to read.

That is the intended asymmetry. Reading history is run tenancy; spending provider
budget on a fresh attempt is entitlement, and inheriting the original start's decision
would let a withdrawn grant keep costing money.

The retry surface takes **no `client_context`** — structurally, not by validation.
Owner, workflow and configuration all come from the parent row, so there is no
parameter through which a caller could aim a retry at another client's slug.
`tests/test_retry_surface.py` asserts both the re-check and the absent parameter.

## Adding a new workflow

`scripts/check_entitlements.py` fails CI until the new contract has an entry, so the
exposure decision cannot be skipped. Default to:

```json
"my_new_workflow": { "clients": [], "skill": null, "note": "Internal." }
```

`"clients": []` is a perfectly good answer — it means operator-only. Open it to a
client later, deliberately, via the grant runbook above.

## What this does NOT cover

`fs_read`/`fs_list` serve the `skills/` jail with **no scope awareness**, so any
authenticated caller can read a `wf-*` skill body whether or not they are entitled to
the workflow. This is accepted: those bodies are how-to prose carrying no tier-3
pinned values and no config-key schema, so the exposure is catalog-level, not
data-level. Tool calls remain fully gated.

The consequence is load-bearing: **`fs_roots` must stay `["skills"]`.** Widening it to
`components` would hand every contract *and this registry* to any caller of any role.
`tests/test_server.py::test_fs_roots_never_widen_beyond_skills` locks that in.
