#!/usr/bin/env bash
# verify-score-tests.sh — run tests/lua/score_test.lua against the rendered
# job_submit.lua. Phase 6 M3 acceptance.
#
# Strategy: render the chart with jobSubmit.enabled=true, extract the lua
# body, then ship both the rendered lua and the test runner into the
# controller pod (which has lua5.2). This keeps the test runtime aligned
# with what slurmctld actually uses.
#
# Usage:
#   bash scripts/verify-score-tests.sh
#   VALUES=chart/values-dev.yaml bash scripts/verify-score-tests.sh

set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
VALUES=${VALUES:-chart/values-k3s.yaml}
LUA_BIN=${LUA_BIN:-lua5.2}

step() { printf "\n[score-tests] %s\n" "$*"; }
fail() { printf "\n[score-tests] FAIL: %s\n" "$*" >&2; exit 1; }

CONTROLLER_POD=$(kubectl -n "$NAMESPACE" get pod \
  -l app=slurm-controller -o jsonpath='{.items[0].metadata.name}')
[[ -n "$CONTROLLER_POD" ]] || fail "no slurm-controller pod found"

step "1/3 render job_submit.lua from chart"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

helm template chart/ -f "$VALUES" \
  --set slurm.jobSubmit.enabled=true \
  --show-only templates/configmap-job-submit.yaml \
  > "$TMP/cm.yaml" 2>/dev/null || fail "helm template failed"

awk '/^  job_submit\.lua: \|/{flag=1; next} flag' "$TMP/cm.yaml" \
  | sed 's/^    //' > "$TMP/job_submit.lua"

[[ -s "$TMP/job_submit.lua" ]] || fail "rendered lua is empty"
echo "[score-tests]   rendered $(wc -l < "$TMP/job_submit.lua") lines"

step "2/3 copy lua + tests into controller pod ($CONTROLLER_POD)"
kubectl -n "$NAMESPACE" cp "$TMP/job_submit.lua" \
  "$CONTROLLER_POD:/tmp/job_submit_under_test.lua" >/dev/null
kubectl -n "$NAMESPACE" cp tests/lua/score_test.lua \
  "$CONTROLLER_POD:/tmp/score_test.lua" >/dev/null

step "3/3 run lua tests inside controller (lua5.2)"
kubectl -n "$NAMESPACE" exec "$CONTROLLER_POD" -- \
  "$LUA_BIN" /tmp/score_test.lua /tmp/job_submit_under_test.lua \
  || fail "lua tests failed"

step "done. M3 score factor unit tests passed."
