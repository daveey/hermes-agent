#!/usr/bin/env bash
# Selects and runs the `gh workflow run` dispatches for .github/workflows/state-contract-dispatch.yml.
#
# Inputs (env): GITHUB_EVENT_NAME, GITHUB_REPOSITORY, GITHUB_SHA, GITHUB_STEP_SUMMARY (optional),
# SCHEDULE_CRON (github.event.schedule; only set on schedule events), GH_TOKEN (for gh).
#
#   push                     core main moved a file state's tests read:
#                              bb-dev-tests.yml (unchanged) and
#                              hermes-sync-tests.yml -f core_ref=$GITHUB_SHA (mods stays at state's pin).
#   schedule '17 11 * * *'   daily backstop, unchanged: bb-dev-tests.yml and hermes-sync-tests.yml, no inputs.
#   anything else            hourly schedule or workflow_dispatch = the upstream canary: hermes-sync-tests.yml
#                              -f mods_ref=<mods main HEAD> -f core_ref=<core main HEAD>, no skip.
#
# `gh` resolves from PATH so tests can put a fake first.
set -euo pipefail

repo="${GITHUB_REPOSITORY:?}"
event="${GITHUB_EVENT_NAME:?}"
sha="${GITHUB_SHA:?}"
cron="${SCHEDULE_CRON:-}"
summary="${GITHUB_STEP_SUMMARY:-/dev/null}"
mods_repo="${MODS_REPOSITORY:-daveey/hermes-mods}"
daily_cron='17 11 * * *'

note() { echo "$*" | tee -a "$summary"; }

dispatch() { # dispatch <workflow> [-f k=v ...]
  local wf="$1"; shift
  gh workflow run "$wf" --repo "$repo" --ref state "$@"
}

state_sha=$(gh api "repos/${repo}/commits/state" --jq .sha)

if [ "$event" = push ] || { [ "$event" = schedule ] && [ "$cron" = "$daily_cron" ]; }; then
  for wf in bb-dev-tests.yml hermes-sync-tests.yml; do
    args=()
    if [ "$event" = push ] && [ "$wf" = hermes-sync-tests.yml ]; then
      args=(-f "core_ref=${sha}")
    fi
    dispatch "$wf" ${args[@]+"${args[@]}"}
    note "dispatched $wf on state ${state_sha} (core main ${sha}, event ${event}${args[*]:+, ${args[*]}})"
  done
  exit 0
fi

# Upstream canary (hourly cron or manual): test both upstream heads against state's tests.
mods_head=$(gh api "repos/${mods_repo}/commits/main" --jq .sha)
core_head=$(gh api "repos/${repo}/commits/main" --jq .sha)
pins=$(gh api "repos/${repo}/contents/.github/workflows/hermes-sync-tests.yml?ref=state" --jq .content \
  | base64 -d | grep -E '^[[:space:]]+(MODS|CORE)_FIXTURE_SHA:' | sed -E 's/^[[:space:]]+//' || true)

dispatch hermes-sync-tests.yml -f "mods_ref=${mods_head}" -f "core_ref=${core_head}"

# gh workflow run prints no run id: find the run by its canary title (set by state's run-name).
title="hermes-sync-tests upstream canary mods=${mods_head} core=${core_head}"
run_id=""
for _ in 1 2 3 4 5 6; do
  run_id=$(gh run list --repo "$repo" --workflow hermes-sync-tests.yml --branch state --event workflow_dispatch \
    --limit 10 --json databaseId,displayTitle \
    --jq "[.[] | select(.displayTitle == \"${title}\")][0].databaseId // empty" || true)
  [ -n "$run_id" ] && break
  sleep 5
done

if [ -n "$run_id" ]; then
  run_text="https://github.com/${repo}/actions/runs/${run_id}"
else
  run_text="run not found yet; title: ${title}"
fi

{
  echo "### Upstream canary (${event})"
  echo "- mods main HEAD: \`${mods_head}\`"
  echo "- core main HEAD: \`${core_head}\`"
  echo "- state HEAD: \`${state_sha}\`"
  echo "- state pins:"
  echo '```'
  echo "$pins"
  echo '```'
  echo "- dispatched hermes-sync-tests.yml on state: ${run_text}"
} | tee -a "$summary"
