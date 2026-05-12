#!/usr/bin/env bash
# M11 Deep RL scheduler — one-shot pipeline:
#   Phase B  PPO training + paired-CI evaluation
#   Phase C  serve smoke + lua unit tests + RLPD scaffold smoke
#   Phase D  live shadow on k3s (image build + helm + sbatch + log capture)
#
# Stages are gated by env vars so a partial re-run is cheap. Default: run all.
#   STAGES=B          # only training + paired-CI
#   STAGES=B,C        # add serve / lua / rlpd smoke
#   STAGES=B,C,D      # add live shadow on k3s
#   STAGES=D          # skip training, reuse newest runs/m11_mppo_*
#
# Knobs (env, all optional):
#   TOTAL_STEPS   500000   PPO training steps
#   N_JOBS        300      synth jobs per episode
#   N_NODES       2        sim cluster shape
#   GPUS_PER_NODE 2
#   TRACE_FAMILY  philly
#   SEEDS         "42 43 44 45 46"   paired-CI seeds
#   NAMESPACE     slurm
#   IMAGE         slurm-rl-scheduler:m11
#   SKIP_BUILD    ""       set non-empty to skip docker build in stage D
#
# Exit codes: 0 ok, 1 stage failure, 2 prerequisite missing.

set -euo pipefail

STAGES=${STAGES:-B,C,D}
TOTAL_STEPS=${TOTAL_STEPS:-500000}
N_JOBS=${N_JOBS:-300}
N_NODES=${N_NODES:-2}
GPUS_PER_NODE=${GPUS_PER_NODE:-2}
TRACE_FAMILY=${TRACE_FAMILY:-philly}
SEEDS=${SEEDS:-"42 43 44 45 46"}
NAMESPACE=${NAMESPACE:-slurm}
IMAGE=${IMAGE:-slurm-rl-scheduler:m11}
SKIP_BUILD=${SKIP_BUILD:-}

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_ROOT"

PY=${PY:-.venv-m11/bin/python}
LUA=${LUA:-lua5.3}

# ---------- helpers --------------------------------------------------------
log() { printf '\n\033[1;36m[m11] %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[m11 WARN] %s\033[0m\n' "$*" >&2; }
die() { printf '\n\033[1;31m[m11 FAIL] %s\033[0m\n' "$*" >&2; exit 1; }
have() { [[ ",$STAGES," == *",$1,"* ]]; }

ensure_venv() {
  if [[ ! -x $PY ]]; then
    log "creating .venv-m11 (one-time, ~2 min)"
    if command -v uv >/dev/null; then
      uv venv .venv-m11
      uv pip install --python "$PY" \
        "stable-baselines3==2.8.0" "sb3-contrib==2.8.0" \
        "torch>=2.4" gymnasium numpy fastapi uvicorn pydantic
    else
      python3 -m venv .venv-m11
      "$PY" -m pip install -U pip
      "$PY" -m pip install "stable-baselines3==2.8.0" "sb3-contrib==2.8.0" \
        "torch>=2.4" gymnasium numpy fastapi uvicorn pydantic
    fi
  fi
}

latest_run() {
  ls -td runs/m11_mppo_* 2>/dev/null | head -1
}

# ---------- Stage B: train + paired-CI -------------------------------------
stage_B() {
  ensure_venv
  log "Phase B-1: training PPO ($TOTAL_STEPS steps, $TRACE_FAMILY)"
  "$PY" -m services.rl_scheduler.ppo_masked_train \
      --total-steps "$TOTAL_STEPS" --eval-freq 25000 --n-envs 4 \
      --n-jobs "$N_JOBS" --n-nodes "$N_NODES" \
      --gpus-per-node "$GPUS_PER_NODE" --trace-family "$TRACE_FAMILY"

  local run
  run=$(latest_run) || die "no runs/m11_mppo_* found"
  log "trained policy: $run"

  log "Phase B-3: paired-CI (3 families × seeds: $SEEDS)"
  "$PY" -m services.rl_scheduler.eval_paired \
      --policy-dir "$run" --seeds $SEEDS \
      --trace-families philly burst ali \
      --n-jobs "$N_JOBS" --n-nodes "$N_NODES" \
      --gpus-per-node "$GPUS_PER_NODE"
}

# ---------- Stage C: serve/lua/rlpd smoke ----------------------------------
stage_C() {
  ensure_venv
  local run
  run=$(latest_run) || die "stage C needs a trained policy (run stage B first)"

  log "Phase C-1: serve endpoint smoke"
  "$PY" -m services.rl_scheduler.serve --policy-dir "$run" --port 8002 \
      >/tmp/rl_serve_smoke.log 2>&1 &
  local serve_pid=$!
  # shellcheck disable=SC2064
  trap "kill $serve_pid 2>/dev/null || true" EXIT
  sleep 4
  curl -fsS http://127.0.0.1:8002/healthz \
      || die "serve healthz failed; see /tmp/rl_serve_smoke.log"
  echo
  "$PY" -m services.rl_scheduler.snapshot_agent \
      --serve-url http://127.0.0.1:8002 --source sim --once
  kill "$serve_pid" 2>/dev/null || true
  trap - EXIT

  log "Phase C-3: lua hook unit tests"
  if command -v "$LUA" >/dev/null; then
    "$LUA" tests/lua/rl_hook_test.lua
  else
    warn "$LUA not installed — skipping lua tests"
  fi

  log "Phase C-4: RLPD scaffold smoke (2k offline, 5 updates)"
  "$PY" -m services.rl_scheduler.rlpd_finetune \
      --base-policy "$run" --offline-steps 2000 \
      --n-updates 5 --utd-ratio 2 --n-jobs 100 \
      --out-dir /tmp/m11_rlpd_smoke
}

# ---------- Stage D: live shadow on k3s ------------------------------------
stage_D() {
  command -v sudo >/dev/null || die "stage D needs sudo for k3s containerd"
  command -v docker >/dev/null || die "stage D needs docker"
  command -v helm >/dev/null || die "stage D needs helm"
  sudo kubectl -n "$NAMESPACE" get sts/slurm-controller >/dev/null 2>&1 \
      || die "stage D needs slurm-platform already deployed in ns/$NAMESPACE"

  if [[ -z $SKIP_BUILD ]]; then
    log "Phase D-1: docker build $IMAGE"
    docker build -f services/rl_scheduler/Dockerfile -t "$IMAGE" .
    log "Phase D-1: ctr import into k3s containerd"
    docker save "$IMAGE" | sudo k3s ctr images import -
  else
    warn "SKIP_BUILD set — reusing existing image $IMAGE"
  fi

  log "Phase D-2: helm upgrade with rlScheduler.enabled=true"
  helm upgrade slurm-platform chart/ -n "$NAMESPACE" \
      -f chart/values-k3s.yaml --reset-then-reuse-values --no-hooks \
      --set rlScheduler.enabled=true \
      --set rlScheduler.lua.enabled=true

  log "Phase D-2: restart controller, wait for rl-scheduler"
  sudo kubectl -n "$NAMESPACE" rollout restart sts/slurm-controller
  sudo kubectl -n "$NAMESPACE" wait --for=condition=Ready \
      pod -l app=slurm-controller --timeout=180s
  sudo kubectl -n "$NAMESPACE" wait --for=condition=Available \
      deploy/rl-scheduler --timeout=120s

  local rl_svc
  rl_svc=$(sudo kubectl -n "$NAMESPACE" get svc rl-scheduler \
      -o jsonpath='{.spec.clusterIP}')
  log "Phase D-3: push snapshot to rl-scheduler ($rl_svc)"
  ensure_venv
  "$PY" -m services.rl_scheduler.snapshot_agent \
      --serve-url "http://$rl_svc:8002" --source sim --once --n-jobs 50

  local login
  login=$(sudo kubectl -n "$NAMESPACE" get pod -l app=slurm-login \
      -o jsonpath='{.items[0].metadata.name}')
  log "Phase D-4: sbatch 10 shadow jobs via $login"
  for i in $(seq 1 10); do
    sudo kubectl -n "$NAMESPACE" exec "$login" -- \
        sbatch --wrap='sleep 3' --job-name="rl-shadow-$i" -p cpu \
        | tail -1
  done

  sleep 4
  log "Phase D-4: collect shadow decisions"
  mkdir -p docs/m11_phase_d
  sudo kubectl -n "$NAMESPACE" logs slurm-controller-0 --tail=600 \
      | grep -E '\[rl\]|\[score-m3\]' \
      | tee docs/m11_phase_d/shadow_decisions.log >/dev/null
  sudo kubectl -n "$NAMESPACE" logs deploy/rl-scheduler --tail=200 \
      > docs/m11_phase_d/serve.log
  local n_decide n_abstain n_noboost
  n_decide=$(grep -c '\[rl\]' docs/m11_phase_d/shadow_decisions.log || true)
  n_abstain=$(grep -c 'abstain' docs/m11_phase_d/shadow_decisions.log || true)
  n_noboost=$(grep -c 'no-boost' docs/m11_phase_d/shadow_decisions.log || true)
  printf '\n  decisions=%d  abstain=%d  no-boost=%d\n' \
      "$n_decide" "$n_abstain" "$n_noboost"
  log "artifacts saved to docs/m11_phase_d/"
}

# ---------- dispatch -------------------------------------------------------
log "STAGES=$STAGES  repo=$REPO_ROOT"
have B && stage_B
have C && stage_C
have D && stage_D
log "done"
