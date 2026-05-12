-- M11 Phase C-3 — unit tests for chart/lua/rl_hook.lua.
--
-- Pure-lua. Run from repo root:
--   lua5.2 tests/lua/rl_hook_test.lua
-- (or any lua interpreter — only string / table / math used).
--
-- The tests inject a fake `io.popen` (via _rl_io_popen) returning canned
-- JSON, so they don't shell out to curl.

local function fail(msg)
  io.stderr:write("FAIL: " .. msg .. "\n")
  os.exit(1)
end

-- slurm globals stub
slurm = { SUCCESS = 0, ERROR = -1, log_info = function(_) end }

-- Load rl_hook.lua relative to this test file.
local script_dir = (arg[0]:match("(.*/)") or "./")
dofile(script_dir .. "../../chart/lua/rl_hook.lua")
assert(rl_call_decide, "rl_call_decide not defined")
assert(rl_apply,       "rl_apply not defined")

-- ---- helpers --------------------------------------------------------------
local PASS, FAILS = 0, 0
local function it(name, f)
  local ok, err = pcall(f)
  if ok then PASS = PASS + 1; print("ok   " .. name)
  else FAILS = FAILS + 1; print("FAIL " .. name .. ": " .. tostring(err)) end
end

local function mock_popen(response_body)
  -- Returns a function with the io.popen signature; the returned file-like
  -- has a single read("*a") that emits the canned body.
  return function(_cmd, _mode)
    local consumed = false
    return {
      read = function(_, _fmt)
        if consumed then return "" end
        consumed = true
        return response_body
      end,
      close = function(_) return true end,
    }
  end
end

local _logs = {}
_rl_log = function(msg) table.insert(_logs, msg) end

-- ---- tests ----------------------------------------------------------------
it("rl_call_decide returns nil when RL_ENABLED=false", function()
  RL_ENABLED = false
  RL_URL = "http://x"
  RL_TIMEOUT_S = 0.1
  local ok, t, reason = rl_call_decide({job_id=1}, 4, 1, 60)
  assert(not ok, "expected ok=false")
  assert(reason == "disabled", "got reason=" .. tostring(reason))
end)

it("rl_call_decide parses priority_boost + flags from JSON", function()
  RL_ENABLED = true
  RL_URL = "http://x"
  RL_TIMEOUT_S = 0.1
  _rl_io_popen = mock_popen(
    '{"priority_boost":1000,"rl_selected":true,"abstain":false,' ..
    '"abstain_reason":null,"rl_selected_job_id":"42",' ..
    '"value":0.65,"entropy":0.12,"shadow":false}')
  local ok, rl, reason = rl_call_decide({job_id=42}, 4, 1, 60)
  assert(ok, "expected ok=true, reason=" .. tostring(reason))
  assert(rl.priority_boost == 1000, "boost=" .. rl.priority_boost)
  assert(rl.rl_selected == true)
  assert(rl.abstain == false)
  assert(math.abs(rl.value - 0.65) < 1e-6, "value=" .. rl.value)
  assert(rl.rl_selected_job_id == "42")
end)

it("rl_apply mutates job_desc.priority when boost > 0 and not abstaining",
   function()
  RL_ENABLED = true
  _rl_io_popen = mock_popen(
    '{"priority_boost":500,"rl_selected":true,"abstain":false,' ..
    '"value":0.4,"entropy":0.2}')
  local jd = {job_id=7, priority=100}
  local applied, info = rl_apply(jd, 4, 1, 60)
  assert(applied == true, "applied=" .. tostring(applied))
  assert(jd.priority == 600, "priority=" .. tostring(jd.priority))
end)

it("rl_apply leaves priority alone when abstaining", function()
  RL_ENABLED = true
  _rl_io_popen = mock_popen(
    '{"priority_boost":0,"rl_selected":false,"abstain":true,' ..
    '"value":-2.5,"entropy":0.8}')
  local jd = {job_id=8, priority=100}
  local applied, info = rl_apply(jd, 4, 1, 60)
  assert(applied == false, "applied=" .. tostring(applied))
  assert(jd.priority == 100, "priority=" .. tostring(jd.priority))
  assert(info.abstain == true)
end)

it("rl_apply tolerates empty curl response (timeout/network failure)",
   function()
  RL_ENABLED = true
  _rl_io_popen = mock_popen("")
  local jd = {job_id=9, priority=50}
  local applied = rl_apply(jd, 4, 1, 60)
  assert(applied == false)
  assert(jd.priority == 50, "priority unchanged on error")
end)

-- ---- summary --------------------------------------------------------------
print(string.format("\n%d passed, %d failed", PASS, FAILS))
if FAILS > 0 then os.exit(1) end
