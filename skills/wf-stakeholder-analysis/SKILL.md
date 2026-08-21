---
name: wf-stakeholder-analysis
client_summary: "Map who supports or resists a decision, and what would move them."
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

If a run fails for missing credentials, or the user asks whose account pays for it, see
**Provider keys: whose account pays** in `wf-guide`. In short: check
`get_credential_status` first — on `credential_policy: operator` there is nothing for the
user to connect — and if a key is needed, mint a browser link with
`create_credential_registration_link`. Never accept an API key in the conversation.

## Where the evidence comes from

The run sources its own evidence: a research-orchestration stage gathers source
material as part of the flow and hands it to document loading. Client documents are
**optional, additive input** — when supplied through an input session, the runner
rehydrates them into that same document-loading stage alongside whatever sourcing
gathers. Two things follow:

- **Never ask the user for a path, folder, or uploaded location**, and never accept one
  they volunteer. Paths on their machine are unreachable from the hosted service, and
  paths inside the service are not theirs to name. Documents travel only through an
  input session (below).
- **Offer — never require — client documents.** If the user wants specific documents
  considered, run the input-session flow; otherwise the run proceeds on gathered
  public evidence alone.

### Supplying client documents (input session)

**Real files beat re-typed text.** When the user mentions a document, ask them to
attach it to the conversation (or grant folder access) rather than pasting its
contents — chat-pasted text arrives reformatted or clipped, and a reconstruction is
not the document. Reserve inline content for text genuinely authored in this
conversation. What you attach is what the run reads.

Create one session per run with `create_input_session`, choosing the right channel per
file:

- **Text authored in this conversation** (a briefing drafted and signed off here): pass
  it inline as `{"name": "briefing.md", "content": "<the full text>"}`. Omit
  `size_bytes`; the server stages the text immediately and there is no browser step.
  .md/.txt only, up to ~128 KB per file. A document the user *pasted* is not this
  case — ask for the file instead.
- **Files whose bytes you hold in the sandbox** (attached to the conversation, or read
  via granted folder access): declare `{"name": "brief.pdf", "size_bytes": <exact
  bytes>}` measured from the real file, then **drive the upload yourself** — no
  browser step for the user. From the returned `upload_url` (shape
  `<base>/uploads/<session_id>?t=<token>`): `POST <base>/uploads/<session_id>/urls`
  with body `{"token": "<token>"}` to mint one short-lived write URL per declared
  file, then stream each file with
  `curl -X PUT -H "x-ms-blob-type: BlockBlob" -H "content-type: <media_type>"
  --data-binary @<file> "<url>"`. Bytes go sandbox → storage directly; never inline
  or re-encode them into a tool call. If the sandbox cannot reach those hosts,
  fall back to the browser flow below and say so.
- **Files only the user holds**: declare them with the exact `size_bytes`
  (finalization rejects any mismatch — if you cannot measure the file, ask the user
  to attach it first, which also upgrades it to the channel above). Give the user
  the `upload_url`; their browser uploads directly to storage, never through the
  agent.

Call `finalize_input_session(handle)` — immediately when every file was inline or
agent-uploaded, or after the user confirms their browser uploads landed
(`get_input_session` shows per-file progress). Submit the returned `inputset:<uuid>`
handle as the workflow's `input_set`
config field. Never invent a handle, and never reuse another run's — a session attaches
to exactly one run.

## Interview and configuration

**Repeat runs skip the interview.** When the user asks to run it "again", "like last
time", or against a prior run's setup: find that run (`list_runs`, or the `run_id`
they name), call `run_status` on it, and seed from its `config` — it is already
filtered to what this caller may see and resubmits cleanly. Change only what the user
wants changed; a fresh `input_set` is always needed when documents are attached (a
session binds to exactly one run). The pre-flight confirmation block below still runs
in full — recall replaces the interview, never the approval.

1. Call `describe_workflow(name="stakeholder_analysis_workflow")`. Treat the returned
   contract as authoritative; do not rely on remembered fields. `list_workflows`
   returns only a chooser's summary — it is not a substitute for this call.
2. Ask every visible tier-1 question whose answer is not already present in the user's
   request. Group compatible questions into one short structured interview. Today that
   is one question: the decision or proposed change being assessed, which must be
   explicit. **Never ask the user for a file path or evidence folder** — the service
   gathers its own evidence, and documents the user wants considered travel through an
   input session (see "Where the evidence comes from"), never a path.
3. Offer tier-2 settings only when they materially affect the result (report title and
   output formats). Never ask about, expose, or submit tier-3 provider controls such as
   model tiers, chunking, retries, internal stages, or budget caps. The client's brand
   is **derived server-side** from the run owner, so do not ask for it or submit it —
   still say which client you resolved, so a wrong overlay is visible to the user.
4. Call `validate_config` **before** you show the user anything to approve, passing
   both the config and the client context you intend to start with:

   ```
   validate_config(
     name="stakeholder_analysis_workflow",
     config=…,
     client_context={"client_slug": "<resolved slug>"},
   )
   ```

   The reply is `{"config": …, "client_slug": …}`. If validation fails, explain the
   field-level issue, revise, and validate again. Only a normalized, validated config
   may be started.

   The returned `client_slug` is the **server's** resolved run owner. Show that value
   in the block below, not the slug you resolved from the overlay. The owner decides
   whose brand the deliverable ships in, so it is the one line the user most needs to
   be true — and until the server echoes it back, it is only your local guess. If it
   differs from the overlay you resolved, stop and say so rather than continuing.
5. Re-emit the full proposed configuration in this plain Markdown block on every
   revision, using the values `validate_config` returned:

   ```markdown
   ## Stakeholder-analysis run
   - Client: … (server-confirmed run owner)
   - Decision or change: …
   - Client documents: … (finalized `inputset:` handle + file names, or "none")
   - Report title: …
   - Deliverables: …
   - Other defaults accepted: …
   ```

   This is a **pre-flight confirmation block**, not a deliverable canvas: the user is
   approving values that will fire a billed run, not co-authoring content that will
   ship. So keep it in chat as plain Markdown, show the exact values as they will be
   submitted, re-emit it in full on every revision, and require an explicit go-ahead.
   Never emit it as an artifact — an editable canvas has no notion of "the exact bytes
   I am about to submit", which is the whole point of this step.
6. Ask for a final go-ahead immediately before starting because the next call launches
   paid hosted compute. On confirmation call `start_run` with:
   - the workflow name;
   - the `config` object `validate_config` returned, verbatim;
   - `client_context={"client_slug": "<the client_slug validate_config returned>"}` —
     the same owner the user approved, never a re-derived one;
   - a stable idempotency key for this confirmed submission, so a retry cannot create a
     duplicate run.

## Follow the run to completion

Keep the returned `run_id` visible. Poll `run_status(run_id)` with increasing intervals
(about 2, 5, 10, then 20 seconds, capped at 30 seconds). A cold start is normal; a tool
error or explicit `failed` status is not. Do not start a replacement run merely because
the first poll is slow.

- **`queued` / `running`:** report concise progress and continue polling. When the
  response carries a `progress` block, say which stage the run is on rather than
  "still running" — `progress.node` is the stage it last finished and
  `progress.nodes_completed` how many are done. `heartbeat_at` is when the runner last
  reported in; a heartbeat that has not moved for many minutes on a long stage is
  normal, and is not grounds for starting a replacement run.
- **`paused`:** present the complete interrupt/questionnaire payload in chat. Let the
  user review or edit it, show the exact resume payload, then call `resume_run` only
  after their confirmation. Continue polling the same `run_id`.
- **`completed`:** call `get_results(run_id)` and surface every available artifact link,
  distinguishing the durable destination link from any temporary download link.
  Each `published` artifact carries a `download_url` that **expires** (see
  `download_url_ttl_seconds`, typically 15 minutes). Say so when you hand one over,
  and if the user comes back later, call `get_results` again to mint a fresh link
  rather than re-sending the stale one or reporting the artifact as lost — the
  artifact is durable, only the link is short-lived. An artifact returned *without*
  a `download_url` still exists (its `sha256` and `size_bytes` are shown); the link
  could not be minted this call, so retry `get_results` before escalating.
- **`failed`:** report the stored error and `run_id`; do not imply a report exists.
  The `failure` block, when present, says which stage died (`failure.stage`) and whether
  the run is worth retrying (`failure.retryable`). If it is, offer `retry_run(run_id)`
  and explain what that does in plain terms: it starts a **new** run that reuses the
  work the failed one already completed, so it is usually much quicker and cheaper than
  starting over. Note the new `run_id` it returns and poll *that* one — the original
  stays failed, on purpose, as the record of what happened.
  Only a failed run can be retried. Do not offer it for a `paused` run (that resumes) or
  a `completed` one (its results are already there). If `retry_run` reports that the run
  has passed retention, its working files have been cleaned up: say so and offer a fresh
  `start_run` instead of retrying in a loop.
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
