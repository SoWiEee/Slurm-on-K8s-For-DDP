#!/usr/bin/env bash
# verify-phase5.sh — verify Lmod module loading and MPI job submission
#
# Flow:
#   1. Check lmod is installed on the login pod
#   2. module avail (shows openmpi/4.1, python3/3.10, cuda/stub)
#   3. module load openmpi/4.1 and verify env vars set correctly
#   4. module purge and verify env cleaned
#   5. Submit sbatch job that does module load + srun --mpi=pmi2
#   6. Check job output contains both MPI ranks
set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
JOB_TIMEOUT=${JOB_TIMEOUT:-120}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

pass() { echo "  PASS: $*"; }
fail() { echo "  FAIL: $*" >&2; exit 1; }
warn() { echo "  WARN: $*" >&2; }

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# On Windows Git Bash (MINGW), kubectl arguments that look like Unix paths
# (e.g. /tmp/foo) are silently rewritten to Windows paths before kubectl sees
# them.  Setting MSYS_NO_PATHCONV=1 disables this conversion for the duration
# of the command, so paths inside the pod stay correct.
kexec() { MSYS_NO_PATHCONV=1 kubectl -n "$NAMESPACE" exec "$@"; }

# Run a login-shell command inside a pod so /etc/profile.d/* gets sourced.
exec_login() {
  local pod="$1"; shift
  kexec "pod/$pod" -- bash -l -c "$*"
}

wait_for_job() {
  local jid="$1"
  local deadline=$(( $(date +%s) + JOB_TIMEOUT ))
  while true; do
    local state
    state=$(kexec pod/slurm-controller-0 -- \
      scontrol show job "$jid" 2>/dev/null | grep -oP 'JobState=\K\w+' || echo "UNKNOWN")
    printf "  [%s] job %s state=%s\n" "$(date +%H:%M:%S)" "$jid" "$state"
    case "$state" in
      COMPLETED) return 0 ;;
      FAILED|CANCELLED|TIMEOUT|NODE_FAIL)
        echo "  job $jid ended with state=$state" >&2
        # Find worker node and show error log
        local node
        node=$(kexec pod/slurm-controller-0 -- \
          sacct -j "$jid" -X -n -P -o "NodeList" 2>/dev/null | tr -d ' \r' | head -1 || echo "")
        [[ -n "$node" ]] && kexec "pod/$node" -- \
          bash -c "cat /tmp/phase5-verify-${jid}.err" 2>/dev/null | sed 's/^/  ERR: /' || true
        return 1 ;;
    esac
    (( $(date +%s) >= deadline )) && { echo "  timed out waiting for job $jid" >&2; return 1; }
    sleep 3
  done
}

# Determine which worker pod ran a completed job (Slurm node name = pod name).
# Use -P (parseable) to avoid sacct's fixed-width column truncation.
job_worker_pod() {
  local jid="$1"
  kexec pod/slurm-controller-0 -- \
    sacct -j "$jid" -X -n -P -o "NodeList" 2>/dev/null \
    | tr -d ' \r' | head -1
}

# ---------------------------------------------------------------------------
# 0. Wait for cluster
# ---------------------------------------------------------------------------
echo "=== [0] Waiting for slurmctld ==="
for _ in $(seq 1 30); do
  if kexec pod/slurm-controller-0 -- scontrol ping >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
kexec pod/slurm-controller-0 -- scontrol ping

# Pick submission pod: login preferred, fall back to controller
login_pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
[[ -z "$login_pod" ]] && login_pod="slurm-controller-0"
echo "  Submission pod: $login_pod"

# ---------------------------------------------------------------------------
# 1. Lmod installed?
# ---------------------------------------------------------------------------
echo ""
echo "=== [1] Lmod installation ==="
lmod_ver=$(exec_login "$login_pod" 'module --version 2>&1 | head -2' || echo "")
if echo "$lmod_ver" | grep -qi "lmod\|version"; then
  echo "  $lmod_ver" | sed 's/^/  /'
  pass "Lmod module system found"
else
  fail "Lmod not found — did the image rebuild include lmod?"
fi

# Verify /etc/profile.d/lmod.sh exists (makes 'module' available in login shells)
if exec_login "$login_pod" 'test -f /etc/profile.d/lmod.sh'; then
  pass "/etc/profile.d/lmod.sh present"
else
  fail "/etc/profile.d/lmod.sh missing"
fi

# ---------------------------------------------------------------------------
# 2. module avail
# ---------------------------------------------------------------------------
echo ""
echo "=== [2] module avail ==="
avail_out=$(exec_login "$login_pod" 'module avail 2>&1' || echo "")
echo "$avail_out" | head -30 | sed 's/^/  /'

if echo "$avail_out" | grep -q "openmpi"; then
  pass "openmpi module visible"
else
  warn "openmpi module not visible — was lmod-modulefiles.yaml applied?"
fi
if echo "$avail_out" | grep -q "python3"; then
  pass "python3 module visible"
else
  warn "python3 module not visible"
fi

# ---------------------------------------------------------------------------
# 3. module load openmpi/4.1 → check env vars
# ---------------------------------------------------------------------------
echo ""
echo "=== [3] module load openmpi/4.1 ==="
load_out=$(exec_login "$login_pod" \
  'module load openmpi/4.1 && echo "MPI_HOME=$MPI_HOME" && echo "SLURM_MPI_TYPE=$SLURM_MPI_TYPE" && mpirun --version 2>&1 | head -2' \
  2>/dev/null || echo "")
echo "$load_out" | sed 's/^/  /'

if echo "$load_out" | grep -q "MPI_HOME="; then
  pass "MPI_HOME is set after module load"
else
  warn "MPI_HOME not set — check openmpi/4.1.lua paths"
fi
if echo "$load_out" | grep -q "Open MPI"; then
  pass "mpirun --version shows Open MPI"
else
  warn "mpirun --version did not show Open MPI"
fi

# ---------------------------------------------------------------------------
# 4. module purge → env cleaned
# ---------------------------------------------------------------------------
echo ""
echo "=== [4] module purge ==="
purge_out=$(exec_login "$login_pod" \
  'module load openmpi/4.1 && module purge && echo "MPI_HOME=${MPI_HOME:-UNSET}"' \
  2>/dev/null || echo "")
echo "$purge_out" | sed 's/^/  /'
if echo "$purge_out" | grep -q "MPI_HOME=UNSET"; then
  pass "module purge cleaned MPI_HOME"
else
  warn "MPI_HOME still set after purge (may be inherited from shell)"
fi

# ---------------------------------------------------------------------------
# 5. sbatch job: module load + srun --mpi=pmi2
# ---------------------------------------------------------------------------
echo ""
echo "=== [5] sbatch MPI job with module load ==="

BATCH_SCRIPT='#!/bin/bash
#SBATCH --job-name=phase5-mpi
#SBATCH --ntasks=2
#SBATCH --nodes=1
#SBATCH --output=/tmp/phase5-verify-%j.out
#SBATCH --error=/tmp/phase5-verify-%j.err
#SBATCH --time=00:03:00

# Source Lmod so "module" command is available inside the batch job
source /etc/profile.d/lmod.sh

module purge
module load openmpi/4.1
module list

echo "--- MPI_HOME=${MPI_HOME:-NOT_SET} ---"
echo "--- SLURM_MPI_TYPE=${SLURM_MPI_TYPE:-NOT_SET} ---"

srun --mpi=pmi2 /bin/sh -c '\''echo "rank:${SLURM_PROCID} ntasks:${SLURM_NTASKS} host:$(hostname) mpi_home:${MPI_HOME}"'\'''

jid=$(kexec "pod/$login_pod" -- \
  bash -c "cat > /tmp/phase5-job.sh <<'BATCHEOF'
${BATCH_SCRIPT}
BATCHEOF
sbatch --parsable /tmp/phase5-job.sh" 2>/dev/null | tr -d '[:space:]')

echo "  Submitted job $jid"
wait_for_job "$jid"

# ---------------------------------------------------------------------------
# 6. Check output  (output file lives on the WORKER pod, not login pod)
# ---------------------------------------------------------------------------
echo ""
echo "=== [6] Job output ==="
worker_pod=$(job_worker_pod "$jid")
echo "  Job ran on pod: ${worker_pod:-unknown}"
out=$(kexec "pod/$worker_pod" -- \
  bash -c "cat /tmp/phase5-verify-${jid}.out" 2>/dev/null || echo "")
echo "$out" | sed 's/^/  /'

if echo "$out" | grep -q "rank:0" && echo "$out" | grep -q "rank:1"; then
  pass "MPI ranks 0 and 1 both executed"
else
  fail "Expected rank:0 and rank:1 in output"
fi

if echo "$out" | grep -q "mpi_home:/usr/lib"; then
  pass "MPI_HOME was set inside the job (module load worked in sbatch)"
else
  warn "MPI_HOME not visible inside job — check that lmod.sh is sourced in script"
fi

# module list output goes to stderr in Lmod; check the .err file on the worker
err=$(kexec "pod/$worker_pod" -- \
  bash -c "cat /tmp/phase5-verify-${jid}.err" 2>/dev/null || echo "")
if echo "$err" | grep -q "Currently Loaded\|openmpi"; then
  pass "module list in job stderr confirms openmpi loaded"
else
  warn "module list not found in job stderr — check /etc/lmod/modulespath in worker image"
fi

echo ""
echo "=== Phase 5 (Lmod + MPI) verification PASSED ==="
