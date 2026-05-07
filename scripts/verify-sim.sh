#!/usr/bin/env bash
# verify-sim.sh — Phase 6 M4 acceptance.
#
# Runs the offline trace replay simulator over a 1000-job synthetic
# Philly-style subsample with the three baseline schedulers (FCFS,
# multifactor, M3 score) and asserts:
#
#   1. all 1000 jobs complete under each scheduler
#   2. each scheduler emits a per-job CSV
#   3. wall-clock time per scheduler < 60 s
#   4. unittest suite under sim/tests is fully green
#
# Usage:
#   bash scripts/verify-sim.sh
#   N_JOBS=2000 bash scripts/verify-sim.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

N_JOBS=${N_JOBS:-1000}
SEED=${SEED:-42}
NODES=${NODES:-4}
GPUS=${GPUS:-4}
OUT="$ROOT/sim/data/out"
TRACE="$ROOT/sim/data/philly_subsample.json"

mkdir -p "$OUT"

step() { printf "\n[verify-sim] %s\n" "$*"; }
fail() { printf "\n[verify-sim] FAIL: %s\n" "$*" >&2; exit 1; }

step "1/3 unittest discover under sim/tests"
python3 -m unittest discover -v sim.tests 2>&1 | tail -20
[[ ${PIPESTATUS[0]} -eq 0 ]] || fail "unit tests failed"

step "2/3 generate synthetic trace + run FCFS (writes $TRACE)"
python3 -m sim.runner \
  --synth-jobs "$N_JOBS" --synth-seed "$SEED" \
  --write-trace "$TRACE" \
  --scheduler fcfs --nodes "$NODES" --gpus-per-node "$GPUS" \
  --output "$OUT/fcfs.csv" --summary-json "$OUT/fcfs.json" \
  | tee "$OUT/fcfs.line.json"

for sch in multifactor score; do
  step "3/3 run $sch over $TRACE"
  python3 -m sim.runner \
    --trace "$TRACE" \
    --scheduler "$sch" --nodes "$NODES" --gpus-per-node "$GPUS" \
    --output "$OUT/${sch}.csv" --summary-json "$OUT/${sch}.json" \
    | tee "$OUT/${sch}.line.json"
done

step "checking acceptance criteria"
python3 - "$OUT" "$N_JOBS" <<'PY'
import json, sys, os
out, n_expected = sys.argv[1], int(sys.argv[2])
ok = True
for sch in ("fcfs", "multifactor", "score"):
    p = os.path.join(out, f"{sch}.json")
    csv_path = os.path.join(out, f"{sch}.csv")
    s = json.load(open(p))
    line_csv = sum(1 for _ in open(csv_path)) - 1  # minus header
    print(f"  {sch:11s}  n_jobs={s['n_jobs']:4d}  csv_rows={line_csv}  wall={s['wall_seconds']}s  jct_mean={s['jct_mean']:.1f}  bf_rate={s['bf_rate']:.3f}  util={s['utilization']:.3f}")
    if s["n_jobs"] != n_expected: print(f"    !! expected {n_expected}"); ok = False
    if s["wall_seconds"] >= 60: print(f"    !! wall >= 60s"); ok = False
    if line_csv != n_expected: print(f"    !! csv rows != {n_expected}"); ok = False
sys.exit(0 if ok else 1)
PY

step "M4 verify ok"
