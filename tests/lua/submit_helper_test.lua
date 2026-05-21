-- R19 (v5 review) — unit tests for chart/lua/submit_helper.lua.
--
-- Pure-lua. Run from repo root:
--   luajit tests/lua/submit_helper_test.lua
-- (or any lua interpreter — only string/table/math used).

local function fail(msg)
  io.stderr:write("FAIL: " .. msg .. "\n")
  os.exit(1)
end

-- slurm globals stub
slurm = { SUCCESS = 0, ERROR = -1, log_info = function(_) end }

-- Defaults that the chart would inject. Each test overrides as needed.
HLP_ENABLED       = true
HLP_MEM_ENABLED   = true
HLP_MEM_PER_GPU   = 4096
HLP_MEM_PER_CPU   = 256
HLP_MEM_MIN       = 1024
HLP_MEM_MAX       = 65536
HLP_PART_ENABLED  = true
HLP_PART_FALLBACK = "cpu"
HLP_PART_RULES    = {
  { gres = "gpu:rtx4080", partition = "gpu-rtx4080" },
  { gres = "gpu:rtx4070", partition = "gpu-rtx4070" },
  { gres = "gpu:",        partition = "gpu-rtx4070" },
}
HLP_QOS_ENABLED   = true
HLP_QOS_DEFAULT   = "normal"
HLP_QOS_RULES     = {
  { account = "research", qos = "high" },
  { account = "ci",       qos = "low" },
}

-- Load module under test.
local script_dir = (arg[0]:match("(.*/)") or "./")
dofile(script_dir .. "../../chart/lua/submit_helper.lua")
assert(parse_gpu_count,          "parse_gpu_count not defined")
assert(helper_compute_memory_mb, "helper_compute_memory_mb not defined")
assert(helper_route_partition,   "helper_route_partition not defined")
assert(helper_resolve_qos,       "helper_resolve_qos not defined")
assert(apply_submit_helpers,     "apply_submit_helpers not defined")

-- mini test runner
local PASS, FAILS = 0, 0
local function it(name, f)
  local ok, err = pcall(f)
  if ok then PASS = PASS + 1; print("ok   " .. name)
  else FAILS = FAILS + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end
local function eq(a, b)
  if a ~= b then error(string.format("expected %s, got %s", tostring(b), tostring(a)), 2) end
end

-- ---- parse_gpu_count ------------------------------------------------------
it("parse_gpu_count: explicit count", function()
  eq(parse_gpu_count("gpu:rtx4070:2,mps:50"), 2)
end)
it("parse_gpu_count: count omitted defaults to 1", function()
  eq(parse_gpu_count("gpu:rtx4070,mps:25"), 1)
end)
it("parse_gpu_count: bare gpu:1", function()
  eq(parse_gpu_count("gpu:1"), 1)
end)
it("parse_gpu_count: multiple gpu tokens sum", function()
  eq(parse_gpu_count("gpu:rtx4070:1,gpu:rtx4080:2"), 3)
end)
it("parse_gpu_count: no gpu", function()
  eq(parse_gpu_count("mps:25"), 0)
  eq(parse_gpu_count(""), 0)
  eq(parse_gpu_count(nil), 0)
end)

-- ---- helper_compute_memory_mb --------------------------------------------
it("memory: gpu + cpu sum", function()
  -- 1 GPU * 4096 + 4 CPU * 256 = 5120
  eq(helper_compute_memory_mb({tres_per_node="gpu:rtx4070:1", min_cpus=4}), 5120)
end)
it("memory: clamped to MIN floor for cpu-only minimal", function()
  -- 0 GPU * 4096 + 1 CPU * 256 = 256 → clamped to HLP_MEM_MIN=1024
  eq(helper_compute_memory_mb({tres_per_node="", min_cpus=1}), 1024)
end)
it("memory: cpus<1 treated as 1", function()
  eq(helper_compute_memory_mb({tres_per_node="gpu:rtx4070:1"}), 4096 + 256)
end)
it("memory: clamped to MAX ceiling", function()
  -- 16 GPU * 4096 = 65536 (exactly MAX); 17 * 4096 = 69632 → clamp 65536
  eq(helper_compute_memory_mb({tres_per_node="gpu:rtx4070:17", min_cpus=0}), 65536)
end)

-- ---- helper_route_partition -----------------------------------------------
it("partition: first match wins (rtx4080)", function()
  eq(helper_route_partition({tres_per_node="gpu:rtx4080:1"}), "gpu-rtx4080")
end)
it("partition: rtx4070 specific rule", function()
  eq(helper_route_partition({tres_per_node="gpu:rtx4070:1,mps:25"}), "gpu-rtx4070")
end)
it("partition: generic gpu: fallback rule", function()
  eq(helper_route_partition({tres_per_node="gpu:1"}), "gpu-rtx4070")
end)
it("partition: case-insensitive substring match", function()
  eq(helper_route_partition({tres_per_node="GPU:RTX4080:1"}), "gpu-rtx4080")
end)
it("partition: empty tres falls back to default", function()
  eq(helper_route_partition({tres_per_node=""}), "cpu")
  eq(helper_route_partition({}), "cpu")
end)
it("partition: no match falls back", function()
  eq(helper_route_partition({tres_per_node="cpu:8"}), "cpu")
end)

-- ---- helper_resolve_qos ---------------------------------------------------
it("qos: account match wins over default", function()
  eq(helper_resolve_qos({account="research"}), "high")
  eq(helper_resolve_qos({account="ci"}), "low")
end)
it("qos: unmatched account → default", function()
  eq(helper_resolve_qos({account="random-user"}), "normal")
end)
it("qos: empty account → default", function()
  eq(helper_resolve_qos({}), "normal")
end)
it("qos: empty default + no match → nil (caller leaves untouched)", function()
  local saved = HLP_QOS_DEFAULT
  HLP_QOS_DEFAULT = ""
  eq(helper_resolve_qos({account="random"}), nil)
  HLP_QOS_DEFAULT = saved
end)

-- ---- apply_submit_helpers — orchestration -------------------------------
it("apply: fills all three when user set none", function()
  local jd = {tres_per_node="gpu:rtx4070:1", min_cpus=4, account="research"}
  local n = apply_submit_helpers(jd)
  eq(n, 3)
  eq(jd.pn_min_memory, 4096 + 4*256)
  eq(jd.partition, "gpu-rtx4070")
  eq(jd.qos, "high")
end)
it("apply: respects user-set --mem", function()
  local jd = {tres_per_node="gpu:rtx4070:1", min_cpus=4, pn_min_memory=8192}
  apply_submit_helpers(jd)
  eq(jd.pn_min_memory, 8192)  -- unchanged
end)
it("apply: respects user-set partition", function()
  local jd = {tres_per_node="gpu:rtx4070:1", partition="user-chosen"}
  apply_submit_helpers(jd)
  eq(jd.partition, "user-chosen")
end)
it("apply: respects user-set qos", function()
  local jd = {tres_per_node="gpu:rtx4070:1", account="research", qos="user-chosen"}
  apply_submit_helpers(jd)
  eq(jd.qos, "user-chosen")
end)
it("apply: master switch disables all helpers", function()
  local saved = HLP_ENABLED
  HLP_ENABLED = false
  local jd = {tres_per_node="gpu:rtx4070:1", min_cpus=4}
  eq(apply_submit_helpers(jd), 0)
  eq(jd.pn_min_memory, nil)
  eq(jd.partition, nil)
  eq(jd.qos, nil)
  HLP_ENABLED = saved
end)
it("apply: per-helper switch (memory off, others on)", function()
  local saved = HLP_MEM_ENABLED
  HLP_MEM_ENABLED = false
  local jd = {tres_per_node="gpu:rtx4070:1", account="ci"}
  apply_submit_helpers(jd)
  eq(jd.pn_min_memory, nil)
  eq(jd.partition, "gpu-rtx4070")
  eq(jd.qos, "low")
  HLP_MEM_ENABLED = saved
end)
it("apply: nil job_desc field tolerated", function()
  -- helper_compute_memory_mb / helper_route_partition / helper_resolve_qos
  -- must all tolerate empty/nil fields without crashing
  local jd = {}
  local n = apply_submit_helpers(jd)
  -- mem: 1*256 → clamped to 1024 → applied
  -- partition: "" → fallback "cpu" → applied
  -- qos: "" account → default "normal" → applied
  eq(n, 3)
  eq(jd.pn_min_memory, 1024)
  eq(jd.partition, "cpu")
  eq(jd.qos, "normal")
end)

-- summary
print(string.format("\n%d passed, %d failed", PASS, FAILS))
if FAILS > 0 then os.exit(1) end
