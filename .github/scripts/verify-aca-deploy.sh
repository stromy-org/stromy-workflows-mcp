#!/usr/bin/env bash
# verify-aca-deploy — prove the revision running THIS build's image is the one
# serving, then health-check that revision directly.
#
# CANONICAL COPY: stromy-org/stromy-org/.github/actions/verify-aca-deploy/verify.sh
# A PUBLIC satellite cannot call a PRIVATE repo's action — the run fails at 0s
# with no jobs and no logs (see the note atop stromy-workflows-mcp's ci.yml), so
# public repos VENDOR this file verbatim. `scripts/audit-aca-verify-drift.py`
# byte-compares every vendored copy against this one. Edit here, then re-run it.
#
# WHY THIS EXISTS (ORG-190, observed 2026-08-01)
#
# `az containerapp update` returns once the control plane ACCEPTS the new
# revision, not once it serves. The app FQDN keeps routing 100% of traffic to
# the OUTGOING revision until rollover completes — so a `/health` poll started
# seconds later returns 200 immediately, against the code being replaced, and
# the job goes green. Observed: "healthy (HTTP 200) on attempt 1", three seconds
# after the update; the real rollover finished minutes later, and a post-deploy
# test run on that green got the OLD code back.
#
# Gate 1 (rollover) is what fixes it: it matches the revision by IMAGE, so the
# outgoing revision cannot satisfy it. Gate 2 then probes that revision's own
# FQDN, which routes only to it — definitive, with no application changes.
#
# WHY WE DO NOT GATE ON `properties.runningState`
#
# The obvious `runningState == "Running"` deadlocks most of this fleet. Measured
# live 2026-08-01, healthy apps reported FOUR values: Running, RunningAtMaxScale,
# ScaledToZero, ActivationFailed. A scale-to-zero MCP with no traffic sits in
# ScaledToZero indefinitely and would never satisfy that gate. It is logged and
# never gated on. `provisioningState == "Failed"` IS fatal — that is unambiguous.
#
# Runs unmodified outside CI: GITHUB_OUTPUT / GITHUB_STEP_SUMMARY are optional.
#
# Required env: APP, RG, IMAGE
# Optional env: HEALTH_PATH (/health) READY_PATH ROLLOVER_TIMEOUT (600)
#               HEALTH_TIMEOUT (300) MIN_TRAFFIC (100)

set -uo pipefail

: "${APP:?APP (container app name) is required}"
: "${RG:?RG (resource group) is required}"
: "${IMAGE:?IMAGE (exact image ref just deployed) is required}"
HEALTH_PATH="${HEALTH_PATH-/health}"
READY_PATH="${READY_PATH-}"
ROLLOVER_TIMEOUT="${ROLLOVER_TIMEOUT:-600}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"
MIN_TRAFFIC="${MIN_TRAFFIC:-100}"

export AZURE_EXTENSION_USE_DYNAMIC_INSTALL=yes_without_prompt

emit() { [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1" >> "$GITHUB_OUTPUT"; return 0; }
fail() { echo "::error::$*" >&2; }

# A floating tag cannot distinguish incoming from outgoing — the outgoing
# revision may carry it too. Refuse rather than verify something meaningless.
case "$IMAGE" in
  *:latest|*:main|*:) fail "image '$IMAGE' is a floating tag; Gate 1 needs an immutable ref (e.g. :sha-<full-sha>)"; exit 1 ;;
  *:*) : ;;
  *) fail "image '$IMAGE' has no tag; Gate 1 needs an immutable ref (e.g. :sha-<full-sha>)"; exit 1 ;;
esac

APP_FQDN=$(az containerapp show --name "$APP" --resource-group "$RG" \
  --query properties.configuration.ingress.fqdn --output tsv 2>/dev/null || true)
emit "app_fqdn=${APP_FQDN}"

# ---------------------------------------------------------------- Gate 1 -----
echo "Gate 1 — waiting for a revision of $APP running:"
echo "  $IMAGE"
echo "to be Provisioned and carrying >=${MIN_TRAFFIC}% traffic (timeout ${ROLLOVER_TIMEOUT}s)"
echo ""

REV=""; REV_FQDN=""
started=$SECONDS
last=""
while [ $((SECONDS - started)) -lt "$ROLLOVER_TIMEOUT" ]; do
  revs=$(az containerapp revision list --name "$APP" --resource-group "$RG" \
    --output json 2>/dev/null || echo '[]')

  # Match ANY container in the revision, not containers[0] — a sidecar must not
  # shift the index out from under the match.
  match=$(printf '%s' "$revs" | jq -c --arg img "$IMAGE" '
    [ .[]
      | select( [ (.properties.template.containers // [])[].image ] | index($img) )
      | { name:    .name,
          prov:    (.properties.provisioningState // "Unknown"),
          running: (.properties.runningState      // "Unknown"),
          traffic: (.properties.trafficWeight     // 0),
          fqdn:    (.properties.fqdn              // "") } ]
    | sort_by(.traffic) | last // empty' 2>/dev/null || true)

  if [ -z "$match" ] || [ "$match" = "null" ]; then
    state="no revision running this image yet"
  else
    prov=$(printf    '%s' "$match" | jq -r .prov)
    running=$(printf '%s' "$match" | jq -r .running)
    traffic=$(printf '%s' "$match" | jq -r .traffic)
    name=$(printf    '%s' "$match" | jq -r .name)

    if [ "$prov" = "Failed" ]; then
      echo ""
      fail "Revision $name FAILED to provision. The deploy did not land."
      echo "  az containerapp logs show -n $APP -g $RG --revision $name --tail 200" >&2
      exit 1
    fi

    if [ "$prov" = "Provisioned" ] && [ "$traffic" -ge "$MIN_TRAFFIC" ] 2>/dev/null; then
      REV="$name"; REV_FQDN=$(printf '%s' "$match" | jq -r .fqdn)
      echo ""
      echo "Gate 1 passed after $((SECONDS - started))s"
      echo "  revision     : $REV"
      echo "  provisioned  : $prov"
      echo "  traffic      : ${traffic}%"
      echo "  runningState : $running   (informational — ScaledToZero is healthy here)"
      emit "revision=$REV"
      emit "revision_fqdn=$REV_FQDN"
      break
    fi
    state="revision $name: $prov, traffic ${traffic}%, runningState $running"
  fi

  # Reprint only on change — keeps a multi-minute rollover readable.
  if [ "$state" != "$last" ]; then
    printf '  [%4ds] %s\n' "$((SECONDS - started))" "$state"
    last="$state"
  fi
  sleep 10
done

if [ -z "$REV" ]; then
  echo ""
  fail "Timed out after ${ROLLOVER_TIMEOUT}s: no revision running $IMAGE reached Provisioned with >=${MIN_TRAFFIC}% traffic."
  echo "Last observed: ${last:-<nothing>}" >&2
  echo "  az containerapp revision list -n $APP -g $RG -o table" >&2
  exit 1
fi

[ -n "$HEALTH_PATH" ] || { echo "HEALTH_PATH empty — skipping Gate 2."; exit 0; }

# ---------------------------------------------------------------- Gate 2 -----
# Prefer the revision's own hostname: it routes ONLY to the revision Gate 1 just
# verified, so a 200 there proves the new code answered. The app FQDN is a real
# fallback (Gate 1 established this revision holds the traffic) but it is weaker,
# so it is labelled rather than silently substituted.
if [ -n "$REV_FQDN" ]; then
  HOST="$REV_FQDN"; MODE="revision-pinned"
  echo ""
  echo "Gate 2 — probing the revision's own FQDN; a 200 here can only come from $REV."
elif [ -n "$APP_FQDN" ]; then
  HOST="$APP_FQDN"; MODE="app-fqdn"
  echo ""
  echo "::warning::Revision $REV exposes no FQDN (internal ingress?); falling back to the app FQDN."
  echo "Gate 1 established $REV holds the traffic, but this probe is NOT revision-pinned."
else
  fail "No FQDN available for $APP — cannot health-check."
  exit 1
fi
emit "verification=$MODE"

probe() {
  # Declared separately: within a single `local a=… b=$a`, bash does not have
  # `a` bound yet when it expands `$a`, so `set -u` aborts the function.
  local probe_path="$1"
  local label="$2"
  local url="https://${HOST}${probe_path}"
  local started=$SECONDS code last=""
  echo ""
  echo "$label: $url (timeout ${HEALTH_TIMEOUT}s)"
  while [ $((SECONDS - started)) -lt "$HEALTH_TIMEOUT" ]; do
    # --max-time 30, not 10: a scale-to-zero revision must cold start, and a
    # cold start that is merely slow is not a failure.
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$url" || echo 000)
    if [ "$code" = "200" ]; then
      echo "  200 after $((SECONDS - started))s"
      return 0
    fi
    [ "$code" != "$last" ] && printf '  [%4ds] HTTP %s\n' "$((SECONDS - started))" "$code"
    last="$code"
    sleep 10
  done
  fail "$label never returned 200 within ${HEALTH_TIMEOUT}s (last HTTP ${last:-000}): $url"
  echo "  az containerapp logs show -n $APP -g $RG --revision $REV --tail 200" >&2
  return 1
}

probe "$HEALTH_PATH" "Health" || exit 1
[ -n "$READY_PATH" ] && { probe "$READY_PATH" "Readiness" || exit 1; }

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Deploy verified"
    echo ""
    echo "| | |"
    echo "|---|---|"
    echo "| revision | \`$REV\` |"
    echo "| image | \`$IMAGE\` |"
    echo "| verification | \`$MODE\` |"
  } >> "$GITHUB_STEP_SUMMARY"
fi

echo ""
echo "Deploy verified: $REV is serving ($MODE)."
