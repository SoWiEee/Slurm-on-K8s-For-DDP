#!/usr/bin/env bash
# 50-job mix for E7. Sourced by run_e7_live.sh inside the login pod.
# Mix breakdown (matches Philly subsample shape):
#   30 short MPS jobs (mps:25, 60-180s)
#   15 medium MPS jobs (mps:50, 300-600s)
#    5 whole-GPU jobs (mps:100, 600-1200s)
set -euo pipefail

submit() {
  local name="$1" mps="$2" sleep_s="$3"
  cat <<EOF | sbatch -J "$name" >/dev/null
#!/bin/bash
#SBATCH --partition=gpu-rtx4070
#SBATCH --gres=mps:${mps}
#SBATCH --cpus-per-task=1
#SBATCH --time=00:30:00
sleep ${sleep_s}
EOF
}

i=0
for _ in $(seq 1 30); do
  submit "e7-short-$i" 25 $((60 + RANDOM % 121)); i=$((i+1))
done
for _ in $(seq 1 15); do
  submit "e7-med-$i" 50 $((300 + RANDOM % 301)); i=$((i+1))
done
for _ in $(seq 1 5); do
  submit "e7-big-$i" 100 $((600 + RANDOM % 601)); i=$((i+1))
done

echo "submitted $i jobs"
squeue -h | wc -l
