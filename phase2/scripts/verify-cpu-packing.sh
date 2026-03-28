#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-180s}
VERIFY_TIMEOUT_SECONDS=${VERIFY_TIMEOUT_SECONDS:-180}
PARTITION=${PARTITION:-debug}
CPU_STS=${CPU_STS:-slurm-worker-cpu}
TARGET_NODE=${TARGET_NODE:-slurm-worker-cpu-0}
JOB_COUNT=${JOB_COUNT:-4}
JOB_CPUS=${JOB_CPUS:-1}
JOB_SLEEP_SECONDS=${JOB_SLEEP_SECONDS:-90}
WORKDIR=${WORKDIR:-/tmp/cpu-packing-smoke}

log() { echo "[cpu-packing] $*"; }
die() { echo "[cpu-packing][ERROR] $*" >&2; exit 1; }

require_context() {
  kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$" || die "kube context ${KUBE_CONTEXT} not found"
  kubectl config use-context "$KUBE_CONTEXT" >/dev/null
}

get_login_pod() {
  kubectl -n "$NAMESPACE" get pod -l app=slurm-login --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}'
}

wait_ready() {
  kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT" >/dev/null
  kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT" >/dev/null
  kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$ROLLOUT_TIMEOUT" >/dev/null
  kubectl -n "$NAMESPACE" rollout status statefulset/"$CPU_STS" --timeout="$ROLLOUT_TIMEOUT" >/dev/null
}

controller_exec() {
  kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "$*"
}

login_exec() {
  local pod="$1"; shift
  kubectl -n "$NAMESPACE" exec "pod/${pod}" -- bash -lc "$*"
}

cleanup_jobs() {
  local ids="$1"
  [[ -n "$ids" ]] || return 0
  controller_exec "scancel ${ids} >/dev/null 2>&1 || true" || true
}

main() {
  require_context
  wait_ready

  log "forcing ${CPU_STS} to a single ready replica"
  kubectl -n "$NAMESPACE" scale statefulset/"$CPU_STS" --replicas=1 >/dev/null
  kubectl -n "$NAMESPACE" rollout status statefulset/"$CPU_STS" --timeout="$ROLLOUT_TIMEOUT" >/dev/null

  local replicas ready
  replicas=$(kubectl -n "$NAMESPACE" get statefulset/"$CPU_STS" -o jsonpath='{.spec.replicas}')
  ready=$(kubectl -n "$NAMESPACE" get statefulset/"$CPU_STS" -o jsonpath='{.status.readyReplicas}')
  [[ "$replicas" == "1" && "$ready" == "1" ]] || die "${CPU_STS} is not at exactly one ready replica"

  local login_pod
  login_pod="$(get_login_pod)"
  [[ -n "$login_pod" ]] || die "no running slurm-login pod found"

  log "checking target node ${TARGET_NODE}"
  controller_exec "scontrol show node '${TARGET_NODE}' >/dev/null"

  log "preparing job scripts in ${WORKDIR} inside ${login_pod}"
  login_exec "$login_pod" "mkdir -p '${WORKDIR}'"

  local submit_script
  submit_script="$(mktemp)"
  cat > "$submit_script" <<'SUBMIT'
#!/usr/bin/env bash
set -euo pipefail
: "${WORKDIR:?}"
: "${JOB_COUNT:?}"
: "${JOB_SLEEP_SECONDS:?}"
: "${PARTITION:?}"
: "${JOB_CPUS:?}"
: "${TARGET_NODE:?}"

mkdir -p "$WORKDIR"
for i in $(seq 1 "$JOB_COUNT"); do
  job_script="$WORKDIR/job-${i}.sh"
  cat > "$job_script" <<JOBEOF
#!/usr/bin/env bash
set -euo pipefail
echo "jobid=${SLURM_JOB_ID} host=$(hostname -s) cpus=${SLURM_CPUS_PER_TASK:-1}"
sleep ${JOB_SLEEP_SECONDS}
JOBEOF
  # Replace the placeholder DEL character back to '$' after the outer heredoc is written.
  perl -0pi -e 's/\x7f/\$/g' "$job_script"
  chmod +x "$job_script"
  sbatch --parsable -p "$PARTITION" -N1 -n1 --cpus-per-task="$JOB_CPUS" -w "$TARGET_NODE" "$job_script"
done
SUBMIT
  kubectl -n "$NAMESPACE" cp "$submit_script" "${login_pod}:${WORKDIR}/submit.sh" >/dev/null
  rm -f "$submit_script"

  log "submitting ${JOB_COUNT} jobs requesting ${JOB_CPUS} CPU each onto ${TARGET_NODE}"
  local submitted_raw submitted_ids submitted_csv
  submitted_raw="$(login_exec "$login_pod" "WORKDIR='${WORKDIR}' JOB_COUNT='${JOB_COUNT}' JOB_SLEEP_SECONDS='${JOB_SLEEP_SECONDS}' PARTITION='${PARTITION}' JOB_CPUS='${JOB_CPUS}' TARGET_NODE='${TARGET_NODE}' bash '${WORKDIR}/submit.sh'" | tr -d '\r')"
  echo "$submitted_raw" | sed 's/^/[cpu-packing] submitted job: /'
  submitted_ids="$(printf '%s\n' "$submitted_raw" | awk 'NF{print $1}')"
  submitted_csv="$(printf '%s\n' "$submitted_ids" | paste -sd, -)"
  [[ -n "$submitted_csv" ]] || die "failed to capture submitted job ids"
  trap 'cleanup_jobs "$submitted_csv"' EXIT

  log "waiting for all jobs to reach RUNNING on ${TARGET_NODE}"
  local deadline=$(( $(date +%s) + VERIFY_TIMEOUT_SECONDS ))
  local observed_running=0
  while true; do
    local snapshot line_count bad_count
    snapshot="$(controller_exec "squeue -h -j '${submitted_csv}' -o '%i|%T|%N|%C'" | tr -d '\r')"
    echo "$snapshot" | sed 's/^/[cpu-packing] state: /'

    line_count=$(printf '%s\n' "$snapshot" | awk 'NF' | wc -l | tr -d ' ')
    bad_count=$(printf '%s\n' "$snapshot" | awk -F'|' -v node="$TARGET_NODE" -v cpus="$JOB_CPUS" '
      NF {
        if ($2 != "RUNNING" || $3 != node || $4 != cpus) bad++
      }
      END { print bad+0 }
    ')

    if [[ "$line_count" == "$JOB_COUNT" && "$bad_count" == "0" ]]; then
      observed_running=1
      break
    fi

    if (( $(date +%s) >= deadline )); then
      break
    fi
    sleep 3
  done

  if [[ "$observed_running" != "1" ]]; then
    echo "[cpu-packing][ERROR] jobs did not all reach RUNNING on ${TARGET_NODE} within timeout" >&2
    controller_exec "squeue -l || true; echo; scontrol show job ${submitted_csv//,/ } || true" >&2 || true
    exit 1
  fi

  log "verifying the worker pool did not scale beyond one replica"
  replicas=$(kubectl -n "$NAMESPACE" get statefulset/"$CPU_STS" -o jsonpath='{.spec.replicas}')
  ready=$(kubectl -n "$NAMESPACE" get statefulset/"$CPU_STS" -o jsonpath='{.status.readyReplicas}')
  echo "[cpu-packing] ${CPU_STS} replicas=${replicas} ready=${ready}"
  [[ "$replicas" == "1" ]] || die "${CPU_STS} scaled out; this was not single-worker packing"

  log "sampling job output files"
  login_exec "$login_pod" "for id in ${submitted_csv//,/ }; do echo \"--- job \\$id ---\"; sed -n '1,20p' '${WORKDIR}/slurm-'\"\\$id\"'.out' 2>/dev/null || true; done"

  log "success: ${JOB_COUNT} concurrent 1-CPU jobs were observed RUNNING on ${TARGET_NODE} with ${CPU_STS}=1"

  cleanup_jobs "$submitted_csv"
  trap - EXIT
}

main "$@"
