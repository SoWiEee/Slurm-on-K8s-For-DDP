#!/usr/bin/env bash
# verify-runtime-predictor.sh — Phase 6 M5 acceptance.
#
# Five gates, all must be green:
#
#   1. pytest under services/runtime_predictor/tests   (14 cases)
#   2. helm-unittest tests/runtime_predictor_test.yaml (10 cases)
#   3. CLI training run on M4 synthetic-with-signal trace, MAE < 1.0
#   4. /predict p95 latency < 50 ms (TestClient harness)
#   5. helm template renders the deployment + cronjob + network-policy
#
# Bullet 4 is folded into pytest (test_predict_p95_under_50ms). The CronJob
# rotation guarantee (acceptance bullet #3 in scheduler.md M5) is covered
# by test_rotate_keeps_exactly_one_backup.
#
# Usage:
#   bash scripts/verify-runtime-predictor.sh
#   PY=.venv-m5/bin/python bash scripts/verify-runtime-predictor.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=${PY:-.venv-m5/bin/python}

step() { printf "\n[verify-rp] %s\n" "$*"; }
fail() { printf "\n[verify-rp] FAIL: %s\n" "$*" >&2; exit 1; }

[[ -x "$PY" ]] || fail "python not found at $PY (run: uv venv .venv-m5 && uv pip install --python .venv-m5/bin/python -r services/runtime_predictor/requirements.txt pytest httpx)"

step "1/5 pytest service unit + integration tests"
PYTHONPATH=services "$PY" -m pytest -q services/runtime_predictor/tests \
  || fail "pytest failed"

step "2/5 helm-unittest runtime_predictor_test.yaml"
helm unittest "$ROOT/chart" -f 'tests/runtime_predictor_test.yaml' \
  || fail "helm-unittest failed"

step "3/5 CLI train on synthetic-with-signal trace (MAE < 1.0)"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
PYTHONPATH=services "$PY" - <<PY
import json, random
from sim.loader import generate_philly_like
rng = random.Random(99)
jobs = generate_philly_like(n_jobs=800, seed=42)
base = {1:600,2:1800,4:5400,8:14400}
uf = {f"u{i:02d}": 0.4 + (i*0.13) % 1.6 for i in range(40)}
out = []
for j in jobs:
    d = j.as_dict(); d["partition"] = "gpu"
    d["runtime"] = float(base[d["gpu_count"]] * uf.get(d["user"], 1.0) * rng.lognormvariate(0, 0.4))
    out.append(d)
open("$TMP/trace.json","w").write(json.dumps(out))
PY

PYTHONPATH=services "$PY" -m runtime_predictor.train \
  --trace "$TMP/trace.json" \
  --output "$TMP/model.pkl" \
  --n-estimators 200 \
  --mae-threshold 1.0 \
  | tee "$TMP/train.json"

step "4/5 CronJob rotation: <output>.bak preserved after re-train"
PYTHONPATH=services "$PY" -m runtime_predictor.train \
  --trace "$TMP/trace.json" \
  --output "$TMP/model.pkl" \
  --rotate \
  --n-estimators 50 >/dev/null
[[ -f "$TMP/model.pkl.bak" ]] || fail "rotate did not produce model.pkl.bak"
PYTHONPATH=services "$PY" -c "
import pickle, sys
head = pickle.load(open('$TMP/model.pkl','rb'))
prev = pickle.load(open('$TMP/model.pkl.bak','rb'))
print(f'  head={head[\"model_version\"]} bak={prev[\"model_version\"]}')
assert head['model_version'] != prev['model_version'], 'no rotation'
"

step "5/5 helm template renders the trio when enabled"
helm template "$ROOT/chart" --set runtimePredictor.enabled=true \
  --show-only templates/runtime-predictor/deployment.yaml \
  --show-only templates/runtime-predictor/cronjob.yaml \
  --show-only templates/runtime-predictor/network-policy.yaml \
  > "$TMP/render.yaml" 2>/dev/null || fail "helm template failed"
grep -q "kind: PersistentVolumeClaim" "$TMP/render.yaml" || fail "PVC missing"
grep -q "kind: Deployment"             "$TMP/render.yaml" || fail "Deployment missing"
grep -q "kind: Service"                "$TMP/render.yaml" || fail "Service missing"
grep -q "kind: CronJob"                "$TMP/render.yaml" || fail "CronJob missing"
grep -q "kind: NetworkPolicy"          "$TMP/render.yaml" || fail "NetworkPolicy missing"

step "M5 verify ok"
