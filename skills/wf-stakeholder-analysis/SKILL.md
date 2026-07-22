---
name: wf-stakeholder-analysis
description: Run a hosted stakeholder-acceptance analysis from a client plugin: gather the decision and evidence settings, validate the safe workflow configuration, start the asynchronous run, handle questionnaire review, and return its report links. Use whenever a client asks for stakeholder mapping, acceptance analysis, resistance analysis, coalition analysis, or a stakeholder report, even if they do not mention workflows.
---

# Hosted stakeholder analysis

Use the `stromy-workflows` workspace connector to run the analysis on the hosted
execution plane. The connector owns the compute and lifecycle; this skill owns the
client interview and a truthful account of what will run.

## Inputs from client-data

- `companies/{client_slug}/company_context.json` — public company facts used to
  understand the decision context and avoid asking questions the overlay already answers

## Resolving the client brand (read first)

Client context lives in the **invoking plugin's** `companies/<slug>/` overlay on the
filesystem — never on this MCP and never under `client-data/clients/…`.

1. Identify the invoking plugin from the `<plugin>:wf-stakeholder-analysis` namespace.
   Resolve the overlay inside that plugin only; do not search every installed plugin and
   choose a convenient match.
2. Locate that plugin's `companies/<slug>/` directory. A kebab plugin namespace may map
   to a no-hyphen folder (`duke-strategies` → `companies/dukestrategies/`).
3. Resolve the overlay state:
   - **Zero overlays → STOP.** Say that the plugin has no client overlay in the sandbox
     and that you will not fabricate client context.
   - **Exactly one → use it and state the client.**
   - **Several plausible overlays and no reliable invoking-plugin match → ASK** which
     plugin/client the user intends.
4. Read `companies/{client_slug}/company_context.json` with normal file tools, not MCP
   `fs_read`. If the declared file itself is missing, STOP and surface the install/data
   problem rather than inventing company facts.

## Connection preflight

The hosted service uses interactive OAuth and therefore appears as the **Stromy
Workflows workspace connector**. It is intentionally absent from the plugin's MCP
configuration. If its tools are unavailable, tell the user to connect that workspace
connector and stop. Do not fall back to a local checkout, shell command, or another
client's connection.

## Interview and configuration

1. Call `describe_workflow(name="stakeholder_analysis_workflow")`. Treat the returned
   contract as authoritative; do not rely on remembered fields.
2. Ask every visible tier-1 question whose answer is not already present in the user's
   request. Group compatible questions into one short structured interview. The
   decision/proposal being assessed must be explicit. An evidence-folder field is
   optional unless the live contract says otherwise; only accept a path the hosted
   service has provisioned, never a path from the user's local machine.
3. Use the resolved overlay slug as the tier-2 `brand_slug` when that field exists.
   Offer tier-2 settings only when they materially affect the result (report title and
   output formats). Never ask about, expose, or submit tier-3 provider controls such as
   model tiers, chunking, retries, internal stages, or budget caps.
4. Re-emit the full proposed configuration in this plain Markdown block on every
   revision:

   ```markdown
   ## Stakeholder-analysis run
   - Client: …
   - Decision or change: …
   - Evidence source: … / none supplied
   - Report title: …
   - Deliverables: …
   - Other defaults accepted: …
   ```

   Keep this as a review surface in chat. Do not claim it is a rendered canvas.
5. Call `validate_config(name="stakeholder_analysis_workflow", config=…)`. If validation
   fails, explain the field-level issue, revise the same summary, and validate again.
   Only a normalized, validated config may be started.
6. Ask for a final go-ahead immediately before starting because the next call launches
   paid hosted compute. On confirmation call `start_run` with:
   - the workflow name;
   - the normalized config;
   - `client_context={"client_slug": "<resolved slug>"}`;
   - a stable idempotency key for this confirmed submission, so a retry cannot create a
     duplicate run.

## Follow the run to completion

Keep the returned `run_id` visible. Poll `run_status(run_id)` with increasing intervals
(about 2, 5, 10, then 20 seconds, capped at 30 seconds). A cold start is normal; a tool
error or explicit `failed` status is not. Do not start a replacement run merely because
the first poll is slow.

- **`queued` / `running`:** report concise progress and continue polling.
- **`paused`:** present the complete interrupt/questionnaire payload in chat. Let the
  user review or edit it, show the exact resume payload, then call `resume_run` only
  after their confirmation. Continue polling the same `run_id`.
- **`completed`:** call `get_results(run_id)` and surface every available artifact link,
  distinguishing the durable destination link from any temporary download link.
- **`failed`:** report the stored error and `run_id`; do not imply a report exists.
- **`cancelled`:** report that terminal state and stop.

Use `list_runs` only to recover a run the user already started or to resolve an explicit
history request. Client role scoping is enforced server-side, but still avoid listing
history unless it helps the task.

## Truthfulness and safety

- Never put secrets, tokens, raw credentials, or local filesystem paths in config.
- Never synthesize evidence, questionnaire answers, stakeholder names, or report links.
- Never treat a successful `start_run` as a successful analysis; only `completed` plus
  `get_results` establishes delivery.
- If the user leaves before completion, hand back the `run_id` and the exact instruction
  to resume status checking later.
