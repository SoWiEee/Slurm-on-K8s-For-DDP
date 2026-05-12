-- M11 Phase C-3 — RL scheduler client for job_submit.lua.
--
-- Standalone module so it can be unit-tested without rendering the chart.
-- The chart's configmap-job-submit.yaml `dofile`s the rendered copy and
-- calls ``rl_apply(job_desc, mps_req, gpu_count, runtime_s)`` from inside
-- ``slurm_job_submit``.
--
-- The function calls the RL serve endpoint (`services/rl_scheduler/serve.py`,
-- POST /decide) via ``curl`` with a tight ``--max-time`` so a hung service
-- can't stall slurmctld.  On any failure path it returns silently — the
-- score-baseline path remains in effect (safety net via fallback, not
-- mid-call exception).
--
-- Globals consumed (set by job_submit.lua at chart-render time):
--   RL_ENABLED    : bool  -- master kill-switch
--   RL_URL        : str   -- e.g. http://rl-scheduler:8002/decide
--   RL_TIMEOUT_S  : float -- curl --max-time
--
-- Globals tests override:
--   _rl_io_popen  : function(cmd, mode) -> file-like  (mock curl response)
--   _rl_log       : function(msg)                     (mock log sink)

local function _log(msg)
  if _rl_log then _rl_log(msg); return end
  if slurm and slurm.log_info then slurm.log_info(msg) end
end

-- Minimal JSON-field extraction.  We only need a handful of scalars from
-- the /decide response; full JSON parsing is overkill and pulls deps.
local function _num_field(s, name)
  local v = string.match(s, '"' .. name .. '"%s*:%s*(%-?%d+%.?%d*)')
  return v and tonumber(v) or nil
end
local function _bool_field(s, name)
  local v = string.match(s, '"' .. name .. '"%s*:%s*(true|false)')
  if v == nil then
    if string.match(s, '"' .. name .. '"%s*:%s*true') then return true end
    if string.match(s, '"' .. name .. '"%s*:%s*false') then return false end
  end
  return v == "true"
end
local function _str_field(s, name)
  return string.match(s, '"' .. name .. '"%s*:%s*"([^"]*)"')
end

-- Build the POST body for /decide.
local function _build_body(job_desc, mps_req, gpu_count, runtime_s)
  local job_id   = tostring((job_desc and job_desc.job_id) or 0)
  local now      = tonumber(os.time())
  local gpu_type = (job_desc and job_desc._gpu_type_hint) or "rtx4070"
  return string.format(
    '{"job_id":"%s","mps_req":%d,"gpu_count":%d,"gpu_type":"%s",' ..
    '"runtime":%.1f,"submit_ts":%d}',
    job_id, mps_req or 0, gpu_count or 1, gpu_type, runtime_s or 0, now)
end

-- Call /decide.  Returns (ok, table, reason).  table fields:
--   priority_boost, rl_selected, abstain, value, entropy
function rl_call_decide(job_desc, mps_req, gpu_count, runtime_s)
  if not RL_ENABLED then return false, nil, "disabled" end
  local body   = _build_body(job_desc, mps_req, gpu_count, runtime_s)
  local body_q = "'" .. string.gsub(body, "'", "'\\''") .. "'"
  local cmd = string.format(
    "curl -fsS --max-time %.3f -X POST -H 'Content-Type: application/json' " ..
    "-d %s %q 2>/dev/null",
    RL_TIMEOUT_S or 0.15, body_q, RL_URL or "")
  local popen = _rl_io_popen or io.popen
  local fh = popen(cmd, "r")
  if not fh then return false, nil, "popen-failed" end
  local resp = fh:read("*a") or ""
  fh:close()
  if resp == "" then return false, nil, "empty-response" end
  local boost = _num_field(resp, "priority_boost")
  if not boost then return false, nil, "no-boost-field" end
  return true, {
    priority_boost     = math.floor(boost + 0.5),
    rl_selected        = _bool_field(resp, "rl_selected"),
    abstain            = _bool_field(resp, "abstain"),
    value              = _num_field(resp, "value") or 0.0,
    entropy            = _num_field(resp, "entropy") or 0.0,
    rl_selected_job_id = _str_field(resp, "rl_selected_job_id"),
  }, nil
end

-- Convenience: wrap call + apply to job_desc.priority.  Logs decision.
-- Returns (applied, info_table).
function rl_apply(job_desc, mps_req, gpu_count, runtime_s)
  -- pcall returns (pcall_ok, fn_ret1, fn_ret2, fn_ret3); rl_call_decide
  -- itself returns (call_ok, table, reason).
  local pcall_ok, call_ok, rl, reason = pcall(rl_call_decide, job_desc,
                                              mps_req, gpu_count, runtime_s)
  if not pcall_ok then
    _log("[rl] lua-error: " .. tostring(call_ok))
    return false, nil
  end
  if not call_ok or not rl then
    _log("[rl] skipped (" .. tostring(reason) .. ")")
    return false, nil
  end
  if rl.abstain then
    _log(string.format("[rl] abstain (value=%.3f entropy=%.3f)",
                       rl.value, rl.entropy))
    return false, rl
  end
  if rl.priority_boost > 0 then
    job_desc.priority = (job_desc.priority or 0) + rl.priority_boost
    _log(string.format(
      "[rl] selected=%s boost=+%d new_prio=%d value=%.3f entropy=%.3f",
      tostring(rl.rl_selected), rl.priority_boost,
      job_desc.priority, rl.value, rl.entropy))
    return true, rl
  end
  _log(string.format("[rl] no-boost selected_id=%s value=%.3f",
                     tostring(rl.rl_selected_job_id), rl.value))
  return false, rl
end
