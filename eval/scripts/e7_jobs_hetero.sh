#!/usr/bin/env bash
# E7 heterogeneous workload — 20 jobs across 5 "users" (via --comment hack
# in the patched lua plugin), varied mps + runtime within each user
# bucket. The predictor's top features (user_mean_log_rt, user_freq) only
# vary if the user identifier differs, which on this single-OS-user
# cluster we fake with --comment=u\d+ — the lua plugin reads that into
# the predictor request body in place of user_name.
#
# Bucket layout (matches sim/loader.py's user_stats space):
#   u05 (low mean_log_rt = "fast user")    — 4 jobs
#   u20 (lowest mean_log_rt = "fastest")   — 4 jobs
#   u01 (mid mean_log_rt)                  — 4 jobs
#   u10 (high mean_log_rt = "slow")        — 4 jobs
#   u34 (highest mean_log_rt = "slowest")  — 4 jobs
#
# All jobs sleep for SAME actual runtimes per (mps tier) so a paired
# comparison stays honest — heterogeneity is in the *predictor input*,
# not the wall-clock work.
set -euo pipefail

TAG="${1:?usage: e7_jobs_hetero.sh <tag>}"
SHARED=/shared/jobs/e7
mkdir -p "$SHARED"
cd "$SHARED"

submit() {
  local idx="$1" mps="$2" sleep_s="$3" user="$4"
  cat <<EOF | sbatch -J "${TAG}-${idx}" --comment="${user}" -o "$SHARED/${TAG}-${idx}.log" >/dev/null
#!/bin/bash
#SBATCH --partition=gpu-rtx4070
#SBATCH --gres=mps:${mps}
#SBATCH --cpus-per-task=1
#SBATCH --time=00:15:00
echo "[\$(date +%s)] start ${TAG}-${idx} mps=${mps} user=${user} sleep=${sleep_s}"
sleep ${sleep_s}
echo "[\$(date +%s)] end"
EOF
}

# 20 jobs: 5 user buckets × 4 jobs each (mix of mps 25/50/100, mixed runtimes).
# Layout matches the homogeneous e7_jobs.sh so paired comparison vs the
# previous vendor.csv stays meaningful (mps/runtime distribution preserved).
USERS=(u05 u20 u01 u10 u34)
SEED="${RANDOM_SEED:-42}"
awk -v seed="$SEED" 'BEGIN{srand(seed); for(i=0;i<20;i++) printf "%d\n", rand()*1000}' > /tmp/e7h_seq.txt
mapfile -t SEQ < /tmp/e7h_seq.txt

i=0
# 12 short jobs (mps:25, 60-120s) — 2-3 per user
for k in 0 1 2 3 4 5 6 7 8 9 10 11; do
  d=$(( 60 + ${SEQ[$k]} % 61 ))
  u=${USERS[$((k % 5))]}
  submit "${i}-s" 25 "$d" "$u"; i=$((i+1))
done
# 6 medium jobs (mps:50, 180-300s) — 1-2 per user
for k in 12 13 14 15 16 17; do
  d=$(( 180 + ${SEQ[$k]} % 121 ))
  u=${USERS[$((k % 5))]}
  submit "${i}-m" 50 "$d" "$u"; i=$((i+1))
done
# 2 large jobs (mps:100, 240-360s)
for k in 18 19; do
  d=$(( 240 + ${SEQ[$k]} % 121 ))
  u=${USERS[$((k % 5))]}
  submit "${i}-l" 100 "$d" "$u"; i=$((i+1))
done

echo "[e7h_jobs] submitted $i jobs tagged ${TAG}-, users=${USERS[*]}"
squeue -h -u root | wc -l
