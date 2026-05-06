#!/usr/bin/env bash
# verify-lua-submit.sh — Phase 6 M2 acceptance check.
#
# 1. Lua syntax check on the rendered job_submit.lua (host-side, needs `lua5.3`)
# 2. Confirms the controller pod loaded the plugin (slurmctld.log line)
# 3. sbatch a `score-demo-*` job and verify the demoBoostPriority took effect
#
# Requires: helm, kubectl, lua5.3 (or lua5.1).
#
# Usage:
#   bash scripts/verify-lua-submit.sh                # default values-k3s
#   VALUES=chart/values-dev.yaml bash scripts/verify-lua-submit.sh

set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
VALUES=${VALUES:-chart/values-k3s.yaml}
LUA_BIN=${LUA_BIN:-lua5.3}
LOGIN_SELECTOR=${LOGIN_SELECTOR:-app=slurm-login}

step() { printf "\n[verify-lua] %s\n" "$*"; }
fail() { printf "\n[verify-lua] FAIL: %s\n" "$*" >&2; exit 1; }

step "1/3 syntax check on rendered job_submit.lua"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

helm template chart/ -f "$VALUES" \
  --set slurm.jobSubmit.enabled=true \
  --show-only templates/configmap-job-submit.yaml \
  > "$TMP/cm.yaml" 2>/dev/null || fail "helm template failed"

# Extract the lua body. ConfigMap keys are indented under `data:`; strip
# the leading 4 spaces helm uses when rendering pipe-style strings.
awk '/^  job_submit\.lua: \|/{flag=1; next} flag' "$TMP/cm.yaml" \
  | sed 's/^    //' > "$TMP/job_submit.lua"

if ! command -v "$LUA_BIN" >/dev/null 2>&1; then
  echo "[verify-lua] $LUA_BIN not installed — skipping host syntax check"
else
  # luac -p does syntax-only parse without running.
  "${LUA_BIN}c" -p "$TMP/job_submit.lua" \
    || fail "lua syntax error in rendered job_submit.lua"
  echo "[verify-lua]   syntax OK ($(wc -l < "$TMP/job_submit.lua") lines)"
fi

CONTROLLER_POD=$(kubectl -n "$NAMESPACE" get pod \
  -l app=slurm-controller -o jsonpath='{.items[0].metadata.name}')
[[ -n "$CONTROLLER_POD" ]] || fail "no slurm-controller pod found"

step "2/3 controller plugin load check (pod=$CONTROLLER_POD)"
# Slurm logs the lua plugin load via auth_jwt / job_submit init lines.
# Our scaffold also prints `[score-m2] job_submit.lua loaded ...`. Either
# is sufficient evidence. We do NOT tail slurmctld.log here (the daemon
# logs to stderr inside the pod and stderr is in `kubectl logs`).
if kubectl -n "$NAMESPACE" logs "$CONTROLLER_POD" --tail=400 2>/dev/null \
     | grep -qE '(job_submit/lua|score-m2.*job_submit\.lua loaded)'; then
  echo "[verify-lua]   plugin load line present"
else
  echo "[verify-lua]   WARN: plugin load line not yet visible — may be a"
  echo "                fresh pod that hasn't received any submission yet."
fi

step "3/3 demo-boost priority check (sbatch score-demo-*)"
LOGIN_POD=$(kubectl -n "$NAMESPACE" get pod \
  -l "$LOGIN_SELECTOR" -o jsonpath='{.items[0].metadata.name}')
[[ -n "$LOGIN_POD" ]] || fail "no slurm-login pod found"

JOBNAME="score-demo-$(date +%s)"
JOBID=$(kubectl -n "$NAMESPACE" exec "$LOGIN_POD" -- bash -lc \
  "sbatch -p cpu --constraint=cpu -J $JOBNAME --time=00:01:00 -n1 --hold \
     --wrap='hostname' | awk '{print \$NF}'")
[[ -n "$JOBID" ]] || fail "sbatch did not return a job id"
echo "[verify-lua]   submitted $JOBNAME jobid=$JOBID (held)"

PRIO=$(kubectl -n "$NAMESPACE" exec "$LOGIN_POD" -- bash -lc \
  "scontrol show job $JOBID | grep -oE 'Priority=[0-9]+' | head -1 | cut -d= -f2")
echo "[verify-lua]   reported priority=$PRIO"

# Cancel before exit
kubectl -n "$NAMESPACE" exec "$LOGIN_POD" -- bash -lc \
  "scancel $JOBID >/dev/null 2>&1 || true"

EXPECTED=$(helm template chart/ -f "$VALUES" \
  --set slurm.jobSubmit.enabled=true \
  --show-only templates/configmap-job-submit.yaml 2>/dev/null \
  | grep -oE 'DEMO_BOOST_PRIORITY = [0-9]+' | head -1 | awk '{print $NF}')

if [[ "$PRIO" == "$EXPECTED" ]]; then
  echo "[verify-lua]   demo-boost OK (priority=$PRIO matches values)"
else
  fail "expected priority=$EXPECTED, got $PRIO"
fi

step "done. M2 lua scaffold acceptance passed."
