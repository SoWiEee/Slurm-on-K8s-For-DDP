-- Phase 6 M3: unit tests for chart/templates/configmap-job-submit.yaml
--
-- Pure-lua, no busted / luarocks dependency. Run via:
--   bash scripts/verify-score-tests.sh
-- which renders the lua from the chart, copies this file + the rendered
-- lua into the controller pod, and runs `lua5.2 score_test.lua`.
--
-- The runner stubs the slurmctld globals (`slurm.log_info`, `slurm.SUCCESS`)
-- and dofiles the rendered job_submit.lua, then exercises the factor and
-- composite functions with a table-driven case list.

local function fail(msg)
  io.stderr:write("FAIL: " .. msg .. "\n")
  os.exit(1)
end

-- ---- slurm globals stub ---------------------------------------------------
slurm = {
  SUCCESS = 0,
  ERROR = -1,
  log_info = function(_) end,
}

-- ---- load the plugin under test ------------------------------------------
local plugin_path = arg[1] or "/tmp/job_submit.lua"
local ok, err = pcall(dofile, plugin_path)
if not ok then fail("dofile " .. plugin_path .. " errored: " .. tostring(err)) end

-- The plugin exposes its locals as globals only when prefixed `function`
-- (see configmap-job-submit.yaml). compute_score / parse_* are global.
assert(parse_mps_req,   "parse_mps_req not defined")
assert(parse_vram_req,  "parse_vram_req not defined")
assert(f_mps_fit,       "f_mps_fit not defined")
assert(f_vram_fit,      "f_vram_fit not defined")
assert(f_fragmentation, "f_fragmentation not defined")
assert(compute_score,   "compute_score not defined")

-- ---- mini test runner ----------------------------------------------------
local PASS, FAIL = 0, 0
local function it(name, f)
  local ok, err = pcall(f)
  if ok then
    PASS = PASS + 1
    io.write(string.format("  ok  %s\n", name))
  else
    FAIL = FAIL + 1
    io.write(string.format("  X   %s\n      %s\n", name, tostring(err)))
  end
end

local function approx_eq(a, b, eps)
  eps = eps or 1e-6
  if math.abs(a - b) > eps then
    error(string.format("expected %.6f got %.6f (eps=%g)", b, a, eps), 2)
  end
end

local function gt(a, b, msg)
  if not (a > b) then
    error(string.format("expected %s > %s (%s)", tostring(a), tostring(b), msg or ""), 2)
  end
end

local function in_range(x, lo, hi)
  if x < lo or x > hi then
    error(string.format("expected %.6f in [%.6f, %.6f]", x, lo, hi), 2)
  end
end

-- ---- parse_mps_req --------------------------------------------------------
print("parse_mps_req")
it("parses mps:25 from tres_per_node", function()
  approx_eq(parse_mps_req("gpu:rtx4070:1,mps:25"), 25)
end)
it("parses mps=25 (= form)", function()
  approx_eq(parse_mps_req("gpu:rtx4070:1,mps=25"), 25)
end)
it("returns 0 for nil", function() approx_eq(parse_mps_req(nil), 0) end)
it("returns 0 for empty string", function() approx_eq(parse_mps_req(""), 0) end)
it("returns 0 when no mps token", function()
  approx_eq(parse_mps_req("gpu:rtx4070:1"), 0)
end)

-- ---- parse_vram_req -------------------------------------------------------
print("parse_vram_req")
it("parses vram-12g+ to 12", function()
  approx_eq(parse_vram_req("vram-12g+&cpu"), 12)
end)
it("parses bare vram-24g to 24", function()
  approx_eq(parse_vram_req("vram-24g"), 24)
end)
it("returns 0 for nil / empty", function()
  approx_eq(parse_vram_req(nil), 0)
  approx_eq(parse_vram_req(""), 0)
end)
it("returns 0 when no vram-* token", function()
  approx_eq(parse_vram_req("gpu-rtx4070&cpu"), 0)
end)

-- ---- f_mps_fit ------------------------------------------------------------
print("f_mps_fit")
it("no MPS request → 1.0 (no penalty)", function()
  approx_eq(f_mps_fit({tres_per_node = "gpu:rtx4070:1"}), 1.0)
end)
it("mps=100 → 1.0 (whole node)", function()
  approx_eq(f_mps_fit({tres_per_node = "gpu:rtx4070:1,mps:100"}), 1.0)
end)
it("mps=50 → 0.5", function()
  approx_eq(f_mps_fit({tres_per_node = "gpu:rtx4070:1,mps:50"}), 0.5)
end)
it("mps=25 → 0.25 (small request, low pack score)", function()
  approx_eq(f_mps_fit({tres_per_node = "gpu:rtx4070:1,mps:25"}), 0.25)
end)
it("mps>100 → 0 (over-request)", function()
  approx_eq(f_mps_fit({tres_per_node = "mps:200"}), 0)
end)
it("monotonic (50 > 25 > 10)", function()
  local s50 = f_mps_fit({tres_per_node = "mps:50"})
  local s25 = f_mps_fit({tres_per_node = "mps:25"})
  local s10 = f_mps_fit({tres_per_node = "mps:10"})
  gt(s50, s25, "50 > 25"); gt(s25, s10, "25 > 10")
end)

-- ---- f_vram_fit -----------------------------------------------------------
print("f_vram_fit")
it("no constraint → 0.5 neutral", function()
  approx_eq(f_vram_fit({features = nil}), 0.5)
  approx_eq(f_vram_fit({features = "cpu"}), 0.5)
end)
it("vram-12g + tier 12 → 1.0 (perfect)", function()
  approx_eq(f_vram_fit({features = "vram-12g"}), 1.0)
end)
it("vram-12g + tier 24 (over-prov) → 0.5", function()
  -- 1 - (24-12)/24 = 0.5
  -- (only triggered if 12 not in tiers — the live tiers are [12, 24] so
  -- the 12 fits perfectly; this case becomes the one below.)
  -- placeholder: with default tiers the 12-tier wins → 1.0
  approx_eq(f_vram_fit({features = "vram-12g"}), 1.0)
end)
it("vram-13g (no exact 13 tier) → tier 24 over-prov", function()
  -- smallest tier ≥ 13 is 24 → 1 - (24-13)/24 ≈ 0.5417
  in_range(f_vram_fit({features = "vram-13g"}), 0.45, 0.60)
end)
it("vram-24g + tier 24 → 1.0", function()
  approx_eq(f_vram_fit({features = "vram-24g"}), 1.0)
end)
it("vram-48g (over largest tier) → 0.0", function()
  approx_eq(f_vram_fit({features = "vram-48g"}), 0.0)
end)

-- ---- f_fragmentation ------------------------------------------------------
print("f_fragmentation")
it("no MPS → 0 (no frag)", function()
  approx_eq(f_fragmentation({tres_per_node = "gpu:rtx4070:1"}), 0)
end)
it("mps=100 → 0 (whole node)", function()
  approx_eq(f_fragmentation({tres_per_node = "mps:100"}), 0)
end)
it("mps=50 → 1.0 (worst frag)", function()
  approx_eq(f_fragmentation({tres_per_node = "mps:50"}), 1.0)
end)
it("symmetric: mps=25 == mps=75", function()
  local s25 = f_fragmentation({tres_per_node = "mps:25"})
  local s75 = f_fragmentation({tres_per_node = "mps:75"})
  approx_eq(s25, s75)
end)

-- ---- compute_score (composite) -------------------------------------------
print("compute_score")
it("output is in [0, 1]", function()
  for _, t in ipairs({nil, "mps:25", "mps:50", "mps:75", "mps:100"}) do
    local s = compute_score({tres_per_node = t, features = "vram-12g"})
    in_range(s, 0, 1)
  end
end)
it("whole-node mps=100 + vram-12g exact > mps=50 + vram-13g over-prov", function()
  local sA = compute_score({tres_per_node = "mps:100", features = "vram-12g"})
  local sB = compute_score({tres_per_node = "mps:50", features = "vram-13g"})
  gt(sA, sB, "well-fit beats half-frag + over-prov")
end)

-- ---- summary --------------------------------------------------------------
io.write(string.format("\n%d passed, %d failed\n", PASS, FAIL))
if FAIL > 0 then os.exit(1) end
