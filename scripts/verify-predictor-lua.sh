#!/usr/bin/env bash
# verify-predictor-lua.sh — Phase 6 M6 acceptance.
#
# End-to-end exercise of the lua → predictor wiring without touching the
# live cluster. Steps:
#
#   1. Train an M5 model on a synthetic-with-signal trace (reuses the
#      test fixture from M5).
#   2. Start uvicorn on 127.0.0.1:$PORT serving runtime_predictor.app.
#   3. Render job_submit.lua with slurm.jobSubmit.predictor.enabled=true
#      and url=http://127.0.0.1:$PORT/predict.
#   4. Drive the rendered plugin from a small lua harness that:
#        - stubs the `slurm` global
#        - loads the plugin (which evaluates top-level `log_info` lines)
#        - calls slurm_job_submit with a synthetic job_desc
#        - asserts job_desc.time_limit was mutated to a sensible value
#   5. Stop uvicorn; re-run the harness; assert it returns SUCCESS and
#      did NOT raise (pcall + curl --max-time fallback).
#
# Acceptance bullet "controller log confirms lua got prediction" is
# satisfied by step 4. Bullet "predictor down → lua fallback ok" by
# step 5. Bullet "bf_* rate increases vs M1 baseline" lives in M8.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PORT=${PORT:-8123}
PY=${PY:-.venv-m5/bin/python}
LUA=${LUA:-lua}
TMP=$(mktemp -d)
trap 'cleanup' EXIT

PRED_PID=""
cleanup() {
  if [[ -n "$PRED_PID" ]] && kill -0 "$PRED_PID" 2>/dev/null; then
    kill "$PRED_PID" 2>/dev/null || true
    wait "$PRED_PID" 2>/dev/null || true
  fi
  rm -rf "$TMP"
}

step() { printf "\n[verify-pl] %s\n" "$*"; }
fail() { printf "\n[verify-pl] FAIL: %s\n" "$*" >&2; exit 1; }

[[ -x "$PY" ]]   || fail "python venv not at $PY (see scripts/verify-runtime-predictor.sh)"
command -v "$LUA" >/dev/null || fail "lua interpreter '$LUA' not in PATH (apt install lua5.3)"
command -v curl   >/dev/null || fail "curl not in PATH"

# ---------------------------------------------------------------------------
step "1/5 train an M5 model on synthetic-with-signal trace"
PYTHONPATH=services "$PY" - <<PY
import json, random, sys
sys.path.insert(0, "services")
from sim.loader import generate_philly_like
from runtime_predictor.train import train as train_fn

rng = random.Random(99)
jobs = generate_philly_like(n_jobs=600, seed=42)
base_by_gpu = {1: 600., 2: 1800., 4: 5400., 8: 14400.}
user_factor = {f"u{i:02d}": 0.4 + (i*0.13) % 1.6 for i in range(40)}
out = []
for j in jobs:
    d = j.as_dict()
    d["partition"] = "gpu"
    d["runtime"] = float(base_by_gpu[d["gpu_count"]] * user_factor.get(d["user"],1.0) * rng.lognormvariate(0,0.4))
    out.append(d)
open("$TMP/trace.json","w").write(json.dumps(out))
m = train_fn("$TMP/trace.json", "$TMP/model.pkl", n_estimators=150)
print("  metrics:", m)
PY

# ---------------------------------------------------------------------------
step "2/5 start uvicorn on 127.0.0.1:$PORT"
MODEL_PATH="$TMP/model.pkl" MIN_TRAIN_SAMPLES=100 PYTHONPATH=services \
  "$PY" -m uvicorn runtime_predictor.app:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning &> "$TMP/uvicorn.log" &
PRED_PID=$!

# wait for /healthz
for _ in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 0.1
done
curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null \
  || { tail -20 "$TMP/uvicorn.log"; fail "uvicorn did not come up"; }
curl -fsS "http://127.0.0.1:$PORT/readyz" | tee "$TMP/readyz.json"
echo

# ---------------------------------------------------------------------------
step "3/5 render job_submit.lua with predictor pointed at 127.0.0.1:$PORT"
helm template "$ROOT/chart" \
  --set slurm.jobSubmit.enabled=true \
  --set slurm.jobSubmit.predictor.enabled=true \
  --set slurm.jobSubmit.predictor.url="http://127.0.0.1:$PORT/predict" \
  --set slurm.jobSubmit.predictor.timeoutMs=2000 \
  --show-only templates/configmap-job-submit.yaml \
  > "$TMP/cm.yaml" 2>/dev/null
awk '/^  job_submit\.lua: \|/{flag=1; next} flag' "$TMP/cm.yaml" \
  | sed 's/^    //' > "$TMP/job_submit.lua"
[[ -s "$TMP/job_submit.lua" ]] || fail "rendered lua is empty"
# Compile-only syntax check (luac doesn't execute → no missing-globals).
luac -p "$TMP/job_submit.lua" 2>"$TMP/lua-syntax.err" \
  || { cat "$TMP/lua-syntax.err"; fail "lua syntax check failed"; }

# Harness — stubs slurm, drives slurm_job_submit, asserts time_limit was set.
cat > "$TMP/harness.lua" <<'LUA'
local plugin = arg[1]
local mode   = arg[2] or "live"  -- "live" | "fallback"

slurm = {SUCCESS=0, ERROR=-1, log_info = function(m) io.stderr:write("[slurm.log] "..m.."\n") end}
local ok, err = pcall(dofile, plugin)
if not ok then io.stderr:write("dofile error: "..tostring(err).."\n"); os.exit(2) end

local jd = {
  name          = "demo-mps25",
  user_name     = "u00",
  partition     = "gpu",
  features      = "vram-12g",
  tres_per_node = "gpu:rtx4070:1,mps:25",
  min_nodes     = 1,
  time_limit    = 0,    -- user did not set --time
}
local rc = slurm_job_submit(jd, {}, 1000)
if rc ~= slurm.SUCCESS then
  io.stderr:write("non-success rc: "..tostring(rc).."\n"); os.exit(3)
end
print(string.format("rc=%d time_limit=%s priority=%s",
  rc, tostring(jd.time_limit), tostring(jd.priority)))
if mode == "live" then
  if not jd.time_limit or jd.time_limit <= 0 then
    io.stderr:write("FAIL: live mode expected time_limit > 0, got "..tostring(jd.time_limit).."\n")
    os.exit(4)
  end
  if jd.time_limit > 4 * 60 then
    -- Predictor is clamped at fallbackHours·60 = 240 min in this run.
    io.stderr:write("FAIL: time_limit "..jd.time_limit.." min exceeds fallback ceiling 240\n")
    os.exit(5)
  end
elseif mode == "fallback" then
  -- predictor unreachable: lua must NOT throw, must NOT mutate time_limit
  if jd.time_limit ~= 0 then
    io.stderr:write("FAIL: fallback mode mutated time_limit to "..tostring(jd.time_limit).."\n")
    os.exit(6)
  end
end
LUA

# ---------------------------------------------------------------------------
step "4/5 drive lua against the live predictor"
"$LUA" "$TMP/harness.lua" "$TMP/job_submit.lua" live 2> "$TMP/harness-live.err" \
  | tee "$TMP/harness-live.out"
grep -q "time_limit=[1-9]" "$TMP/harness-live.out" \
  || { cat "$TMP/harness-live.err"; fail "live: time_limit not set by lua"; }
grep -q "applied predicted time_limit" "$TMP/harness-live.err" \
  || { cat "$TMP/harness-live.err"; fail "live: lua did not log [predictor] applied"; }

# ---------------------------------------------------------------------------
step "5/5 stop predictor and confirm lua falls back without error"
kill "$PRED_PID" 2>/dev/null || true
wait "$PRED_PID" 2>/dev/null || true
PRED_PID=""
sleep 0.3   # let TCP close

"$LUA" "$TMP/harness.lua" "$TMP/job_submit.lua" fallback 2> "$TMP/harness-fb.err" \
  | tee "$TMP/harness-fb.out"
grep -q "time_limit=0" "$TMP/harness-fb.out" \
  || { cat "$TMP/harness-fb.err"; fail "fallback: lua mutated time_limit despite curl failure"; }
grep -q "skipped" "$TMP/harness-fb.err" \
  || { cat "$TMP/harness-fb.err"; fail "fallback: lua did not log [predictor] skipped"; }

step "M6 verify ok"
