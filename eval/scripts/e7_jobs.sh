#!/usr/bin/env bash
# E7 live workload — 20 jobs, single-RTX4070 friendly.
# Sourced via `kubectl -n slurm exec ... -- bash -lc "$(cat e7_jobs.sh) <pass_tag>"`.
#
# Mix (total ~240,000 mps-seconds → ~40 min optimal wall-clock on 100-slot RTX 4070):
#   12 short jobs   — mps:25,  60-120s    (small contention pressure)
#    6 medium jobs  — mps:50, 180-300s   (queue builders)
#    2 large jobs   — mps:100, 240-360s  (head-of-line blockers)
#
# Job names are prefixed with $TAG so we can pair across passes by suffix.
set -euo pipefail

TAG="${1:-pass}"
SHARED=/shared/jobs/e7
mkdir -p "$SHARED"
cd "$SHARED"

submit() {
  local idx="$1" mps="$2" sleep_s="$3"
  cat <<EOF | sbatch -J "${TAG}-${idx}" -o "$SHARED/${TAG}-${idx}.log" >/dev/null
#!/bin/bash
#SBATCH --partition=gpu-rtx4070
#SBATCH --gres=mps:${mps}
#SBATCH --cpus-per-task=1
#SBATCH --time=00:15:00
echo "[\$(date +%s)] start ${TAG}-${idx} mps=${mps} sleep=${sleep_s}"
sleep ${sleep_s}
echo "[\$(date +%s)] end"
EOF
}

# Use a tiny deterministic-ish seed per run so the mix is reproducible.
SEED="${RANDOM_SEED:-42}"
awk -v seed="$SEED" 'BEGIN{srand(seed); for(i=0;i<24;i++) printf "%d\n", rand()*1000}' > /tmp/e7_seq.txt
mapfile -t SEQ < /tmp/e7_seq.txt

i=0
for k in 0 1 2 3 4 5 6 7 8 9 10 11; do
  d=$(( 60 + ${SEQ[$k]} % 61 ))     # 60-120s
  submit "${i}-s" 25 "$d"; i=$((i+1))
done
for k in 12 13 14 15 16 17; do
  d=$(( 180 + ${SEQ[$k]} % 121 ))   # 180-300s
  submit "${i}-m" 50 "$d"; i=$((i+1))
done
for k in 18 19; do
  d=$(( 240 + ${SEQ[$k]} % 121 ))   # 240-360s
  submit "${i}-l" 100 "$d"; i=$((i+1))
done

echo "[e7_jobs] submitted $i jobs tagged ${TAG}-"
squeue -h -u root | wc -l
