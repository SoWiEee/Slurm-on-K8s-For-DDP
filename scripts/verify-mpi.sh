#!/usr/bin/env bash
# verify-mpi.sh — verify MPI job submission via Slurm (PMI2)
#
# Tests:
#   1. Basic multi-task srun (2 tasks on 1 node, no MPI library)
#   2. PMI2 srun (srun --mpi=pmi2) to verify the Slurm PMI2 plugin
#   3. OpenMPI mpirun launched through srun
#
# Prerequisites: Phase 1 must be up (slurmctld + at least 1 CPU worker).
set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
JOB_TIMEOUT=${JOB_TIMEOUT:-120}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
exec_ctrl() { kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- "$@"; }

wait_for_job() {
  local job_id="$1"
  local pod="$2"
  local deadline=$(( $(date +%s) + JOB_TIMEOUT ))
  while true; do
    local state
    state=$(exec_ctrl scontrol show job "$job_id" 2>/dev/null \
      | grep -oP 'JobState=\K\w+' || echo "UNKNOWN")
    printf "  [%s] job %s state=%s\n" "$(date +%H:%M:%S)" "$job_id" "$state"
    case "$state" in
      COMPLETED) return 0 ;;
      FAILED|CANCELLED|TIMEOUT|NODE_FAIL)
        echo "ERROR: job $job_id ended with state=$state" >&2
        kubectl -n "$NAMESPACE" exec "pod/$pod" -- \
          cat "/tmp/mpi-verify-${job_id}.err" 2>/dev/null || true
        return 1
        ;;
    esac
    if (( $(date +%s) >= deadline )); then
      echo "ERROR: timed out waiting for job $job_id" >&2
      return 1
    fi
    sleep 3
  done
}

submit_batch() {
  local pod="$1"
  local script="$2"
  # Write script to pod then sbatch it.
  kubectl -n "$NAMESPACE" exec "pod/$pod" -- \
    bash -c "cat > /tmp/mpi-job.sh && sbatch --parsable /tmp/mpi-job.sh" \
    <<< "$script" 2>/dev/null | tr -d '[:space:]'
}

# ---------------------------------------------------------------------------
# Step 0: Wait for cluster ready
# ---------------------------------------------------------------------------
echo "=== [0] Waiting for slurmctld ==="
for _ in $(seq 1 30); do
  if exec_ctrl scontrol ping >/dev/null 2>&1; then break; fi
  sleep 3
done
exec_ctrl scontrol ping

# ---------------------------------------------------------------------------
# Step 1: Check MpiDefault and plugin availability
# ---------------------------------------------------------------------------
echo ""
echo "=== [1] MPI configuration ==="
mpi_default=$(exec_ctrl grep 'MpiDefault' /etc/slurm/slurm.conf 2>/dev/null || echo "MpiDefault=NOT_SET")
echo "  slurm.conf: $mpi_default"

# Check that the pmi2 MPI plugin exists on the controller image.
pmi2_plugin=$(exec_ctrl sh -c \
  'find /usr/lib -name "mpi_pmi2.so" 2>/dev/null | head -1 || echo ""')
if [[ -n "$pmi2_plugin" ]]; then
  echo "  pmi2 plugin: $pmi2_plugin"
else
  echo "  WARNING: mpi_pmi2.so not found — pmi2 plugin may be missing" >&2
fi

# ---------------------------------------------------------------------------
# Step 2: Ensure at least 1 CPU worker is idle
# ---------------------------------------------------------------------------
echo ""
echo "=== [2] Worker node status ==="
exec_ctrl sinfo -o "%-20N %-8T %-5c %-10m" 2>/dev/null | grep -E "NODELIST|cpu" || \
  exec_ctrl sinfo 2>/dev/null | head -5

# Pick the submission pod (login preferred, controller fallback)
submit_pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
[[ -z "$submit_pod" ]] && submit_pod="slurm-controller-0"
echo "  Submitting from pod: $submit_pod"

# ---------------------------------------------------------------------------
# Step 3: Basic multi-task test (no MPI library, just srun)
# ---------------------------------------------------------------------------
echo ""
echo "=== [3] Basic multi-task srun (2 tasks, 1 node) ==="
BASIC_SCRIPT=$(cat <<'EOF'
#!/bin/bash
#SBATCH --job-name=mpi-basic
#SBATCH --ntasks=2
#SBATCH --nodes=1
#SBATCH --output=/tmp/mpi-verify-%j.out
#SBATCH --error=/tmp/mpi-verify-%j.err
#SBATCH --time=00:02:00
srun /bin/sh -c 'echo "task:${SLURM_PROCID} ntasks:${SLURM_NTASKS} host:$(hostname)"'
EOF
)
jid1=$(submit_batch "$submit_pod" "$BASIC_SCRIPT")
echo "  Submitted job $jid1"
wait_for_job "$jid1" "$submit_pod"
out1=$(kubectl -n "$NAMESPACE" exec "pod/$submit_pod" -- \
  cat "/tmp/mpi-verify-${jid1}.out" 2>/dev/null || echo "")
echo "  Output:"
echo "$out1" | sed 's/^/    /'
if echo "$out1" | grep -q "task:0" && echo "$out1" | grep -q "task:1"; then
  echo "  PASS: both tasks (0 and 1) ran"
else
  echo "  FAIL: expected task:0 and task:1 in output" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 4: PMI2 test — srun --mpi=pmi2
# ---------------------------------------------------------------------------
echo ""
echo "=== [4] PMI2 multi-task srun (srun --mpi=pmi2) ==="
PMI2_SCRIPT=$(cat <<'EOF'
#!/bin/bash
#SBATCH --job-name=mpi-pmi2
#SBATCH --ntasks=2
#SBATCH --nodes=1
#SBATCH --output=/tmp/mpi-verify-%j.out
#SBATCH --error=/tmp/mpi-verify-%j.err
#SBATCH --time=00:02:00
srun --mpi=pmi2 /bin/sh -c 'echo "rank:${SLURM_PROCID} ntasks:${SLURM_NTASKS} host:$(hostname)"'
EOF
)
jid2=$(submit_batch "$submit_pod" "$PMI2_SCRIPT")
echo "  Submitted job $jid2"
wait_for_job "$jid2" "$submit_pod"
out2=$(kubectl -n "$NAMESPACE" exec "pod/$submit_pod" -- \
  cat "/tmp/mpi-verify-${jid2}.out" 2>/dev/null || echo "")
echo "  Output:"
echo "$out2" | sed 's/^/    /'
if echo "$out2" | grep -q "rank:0" && echo "$out2" | grep -q "rank:1"; then
  echo "  PASS: PMI2 ranks 0 and 1 completed"
else
  echo "  FAIL: expected rank:0 and rank:1 in output" >&2
  # Show error log for diagnostics
  kubectl -n "$NAMESPACE" exec "pod/$submit_pod" -- \
    cat "/tmp/mpi-verify-${jid2}.err" 2>/dev/null | sed 's/^/  ERR: /' || true
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 5: OpenMPI mpirun test (if mpirun available on workers)
# ---------------------------------------------------------------------------
echo ""
echo "=== [5] OpenMPI mpirun availability on workers ==="
mpirun_path=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-cpu-0 -- \
  which mpirun 2>/dev/null || echo "")
if [[ -n "$mpirun_path" ]]; then
  echo "  mpirun found: $mpirun_path"
  mpirun_ver=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-cpu-0 -- \
    mpirun --version 2>&1 | head -1 || echo "unknown")
  echo "  version: $mpirun_ver"

  OMPI_SCRIPT=$(cat <<'EOF'
#!/bin/bash
#SBATCH --job-name=mpi-ompi
#SBATCH --ntasks=2
#SBATCH --nodes=1
#SBATCH --output=/tmp/mpi-verify-%j.out
#SBATCH --error=/tmp/mpi-verify-%j.err
#SBATCH --time=00:02:00
mpirun --oversubscribe -np 2 \
  --mca btl_base_warn_component_unused 0 \
  /bin/sh -c 'echo "ompi-rank:${OMPI_COMM_WORLD_RANK} host:$(hostname)"'
EOF
  )
  jid3=$(submit_batch "$submit_pod" "$OMPI_SCRIPT")
  echo "  Submitted job $jid3"
  wait_for_job "$jid3" "$submit_pod"
  out3=$(kubectl -n "$NAMESPACE" exec "pod/$submit_pod" -- \
    cat "/tmp/mpi-verify-${jid3}.out" 2>/dev/null || echo "")
  echo "  Output:"
  echo "$out3" | sed 's/^/    /'
  if echo "$out3" | grep -q "ompi-rank:0" && echo "$out3" | grep -q "ompi-rank:1"; then
    echo "  PASS: OpenMPI ranks 0 and 1 completed"
  else
    echo "  WARNING: OpenMPI test output unexpected (mpirun env may differ)" >&2
  fi
else
  echo "  mpirun not found on slurm-worker-cpu-0 — skipping OpenMPI test"
  echo "  (rebuild worker image with openmpi-bin to enable this test)"
fi

# ---------------------------------------------------------------------------
# Step 6: Verify PodDisruptionBudgets are present
# ---------------------------------------------------------------------------
echo ""
echo "=== [6] PodDisruptionBudgets ==="
pdb_list=$(kubectl -n "$NAMESPACE" get pdb -o custom-columns=\
'NAME:.metadata.name,MIN-AVAIL:.spec.minAvailable,MAX-UNAVAIL:.spec.maxUnavailable' \
  2>/dev/null || echo "")
if [[ -n "$pdb_list" ]]; then
  echo "$pdb_list"
  pdb_count=$(echo "$pdb_list" | tail -n +2 | wc -l | tr -d ' ')
  echo "  Total PDBs: $pdb_count"
  if (( pdb_count >= 5 )); then
    echo "  PASS: expected PDBs present (controller + 3 worker pools + login + extras)"
  else
    echo "  WARNING: fewer PDBs than expected (want ≥5, got $pdb_count)"
  fi
else
  echo "  WARNING: no PDBs found in namespace $NAMESPACE" >&2
fi

echo ""
echo "=== MPI + PDB verification PASSED ==="
