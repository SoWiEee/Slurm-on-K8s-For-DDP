-- R19 (v5 review) — sbatch submit helper.
--
-- Standalone module so it can be unit-tested without rendering the chart.
-- The chart's configmap-job-submit.yaml declares the HLP_* config globals
-- and then `{{ .Files.Get "lua/submit_helper.lua" | indent 4 }}`s this file
-- in. The functions read the following globals (all set by the chart):
--
--   HLP_ENABLED       : bool  -- master switch
--   HLP_MEM_ENABLED   : bool
--   HLP_MEM_PER_GPU   : int   -- MB per requested GPU
--   HLP_MEM_PER_CPU   : int   -- MB per requested CPU
--   HLP_MEM_MIN       : int   -- floor on computed memory (MB)
--   HLP_MEM_MAX       : int   -- ceiling on computed memory (MB)
--   HLP_PART_ENABLED  : bool
--   HLP_PART_FALLBACK : str   -- partition used when no rule matches
--   HLP_PART_RULES    : list of { gres=str, partition=str } — first-match wins
--   HLP_QOS_ENABLED   : bool
--   HLP_QOS_DEFAULT   : str   -- "" disables the qos helper
--   HLP_QOS_RULES     : list of { account=str, qos=str }
--
-- Each helper is a no-op if the user already set the field, so this never
-- overrides explicit user intent. apply_submit_helpers() runs all three.

-- Counts the GPU GRES total (sum of trailing :N integers across gpu:*
-- tokens). Returns 0 when tres_per_node has no gpu request.
--   parse_gpu_count("gpu:rtx4070:2,mps:50") -> 2
--   parse_gpu_count("gpu:1")                -> 1
--   parse_gpu_count(nil)                    -> 0
function parse_gpu_count(tres_per_node)
  if not tres_per_node or tres_per_node == "" then return 0 end
  local total = 0
  for tok in string.gmatch(tres_per_node, "([^,]+)") do
    local lower = string.lower(tok)
    if string.sub(lower, 1, 4) == "gpu:" then
      local n = string.match(tok, ":(%d+)$")
      total = total + (tonumber(n) or 1)
    end
  end
  return total
end

-- Heuristic memory request (MB). Never below HLP_MEM_MIN, never above
-- HLP_MEM_MAX. Only called when the user didn't set --mem.
function helper_compute_memory_mb(job_desc)
  local gpus = parse_gpu_count(job_desc and job_desc.tres_per_node)
  local cpus = tonumber((job_desc and job_desc.min_cpus) or 0) or 0
  if cpus < 1 then cpus = 1 end
  local m = gpus * HLP_MEM_PER_GPU + cpus * HLP_MEM_PER_CPU
  if m < HLP_MEM_MIN then m = HLP_MEM_MIN end
  if m > HLP_MEM_MAX then m = HLP_MEM_MAX end
  return m
end

-- First-match GRES → partition routing. Substring match on tres_per_node
-- (case-insensitive). Returns HLP_PART_FALLBACK when nothing matches.
function helper_route_partition(job_desc)
  local tres = (job_desc and job_desc.tres_per_node) or ""
  if tres == "" then return HLP_PART_FALLBACK end
  local lower = string.lower(tres)
  for _, r in ipairs(HLP_PART_RULES) do
    if r.gres and r.gres ~= "" and string.find(lower, string.lower(r.gres), 1, true) then
      return r.partition
    end
  end
  return HLP_PART_FALLBACK
end

-- Account-based QoS lookup. Returns HLP_QOS_DEFAULT when no rule matches
-- the account; returns nil when both rule-match and default are empty so
-- the caller can leave job_desc.qos untouched.
function helper_resolve_qos(job_desc)
  local account = (job_desc and job_desc.account) or ""
  for _, r in ipairs(HLP_QOS_RULES) do
    if r.account ~= "" and r.account == account then
      return r.qos
    end
  end
  if HLP_QOS_DEFAULT == "" then return nil end
  return HLP_QOS_DEFAULT
end

-- Run all enabled helpers. Logs each application via slurm.log_info when
-- available; never overrides a user-provided value. Returns the count of
-- mutations applied, mostly for tests.
function apply_submit_helpers(job_desc)
  if not HLP_ENABLED then return 0 end
  local applied = 0

  -- --mem: only fill when user didn't set it (pn_min_memory ≤ 0).
  if HLP_MEM_ENABLED then
    local cur = tonumber((job_desc and job_desc.pn_min_memory) or 0) or 0
    if cur <= 0 then
      local mem = helper_compute_memory_mb(job_desc)
      job_desc.pn_min_memory = mem
      if slurm and slurm.log_info then
        slurm.log_info(string.format(
          "[helper] memory=%dMB (gpu=%d cpu=%s)", mem,
          parse_gpu_count(job_desc and job_desc.tres_per_node),
          tostring((job_desc and job_desc.min_cpus) or "?")))
      end
      applied = applied + 1
    end
  end

  -- --partition: only fill when empty.
  if HLP_PART_ENABLED then
    local cur = (job_desc and job_desc.partition) or ""
    if cur == "" then
      local part = helper_route_partition(job_desc)
      if part and part ~= "" then
        job_desc.partition = part
        if slurm and slurm.log_info then
          slurm.log_info("[helper] partition=" .. part)
        end
        applied = applied + 1
      end
    end
  end

  -- --qos: only fill when empty.
  if HLP_QOS_ENABLED then
    local cur = (job_desc and job_desc.qos) or ""
    if cur == "" then
      local q = helper_resolve_qos(job_desc)
      if q and q ~= "" then
        job_desc.qos = q
        if slurm and slurm.log_info then
          slurm.log_info("[helper] qos=" .. q .. " (account=" ..
            tostring((job_desc and job_desc.account) or "") .. ")")
        end
        applied = applied + 1
      end
    end
  end

  return applied
end
