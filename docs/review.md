# Kelpflux 系統審查報告（v5）

> **評估時間：** 2026-05-21
> **評估快照：** main @ `04984a6`（Phase 6 全部完成 + Phase 7-A OTel + 7-B SSH Login + GitHub Actions CI 上線）
> **評估視角：** HPC 叢集工程師 + K8s SRE + ML systems
> **本輪範圍：**
>   1. 程式碼品質 / 開發品質
>   2. K8s 工程實踐
>   3. **排程與資源分配的客製設計可靠度**（vs 更穩定的替代方案）
>   4. 目標達成度與系統特色（vs Slinky / SUNK / Slonk / ParallelCluster）
> **排除範圍：** 資安、HA、多租戶（依使用者要求；前者保留至 v6，後兩者列入未來工作）

本版相對 v4：v4 聚焦於「Phase 5 完工後仍阻擋上線的工程缺口」；v5 範圍轉向**程式碼層次**、**客製設計的可靠度評估**與**特色達成度**，並把 v4 仍有效的 R 編號議題收進對應章節更新狀態，不再單獨列開放清單。

---

## 0. 執行摘要

Phase 6/7-A/7-B 上線後，系統從「能跑」進入「可以給人用、可以給人看數字」。三件事最值得肯定：

1. **CI 從零到上線**：4 個 workflow（python-lint / sim-unit / operator-test / lua-test）；ruff 全乾淨、57 個單元測試 3 秒內跑完；commit message 維持 conventional commits 紀律
2. **OTel 端到端 trace（Phase 7-A）** 透過 W3C `traceparent` 寫入 `job_desc.admin_comment` 串接 sync Lua submit 與 async Operator 輪詢——這個橋接模式在 Slinky / SUNK / Slonk 都沒見過，是 v4 R16 點名的差異化機會兌現
3. **shadow-mode 安全網一致性**：rl_scheduler、weight_tuner、fragmentation reconciler、runtime_predictor 預設都 shadow mode + silent-fallback 到 score baseline——HPC 生產線該有的工程紀律

但同時累積了四類技術債：

| 類別 | 議題 | 嚴重度 | 章節 |
|---|---|:---:|---|
| 程式碼結構 | `operator/app.py` 906 LoC 單檔；`_process_pool` 178 行 | 🟠 P1 | §1.2 |
| 死碼 | `services/rl_scheduler/{ppo_*,hierarchical,snapshot_agent}.py` 已被 ruff exclude，但仍佔 ~1500 LoC | 🟡 P2 | §1.3 |
| 客製排程脆弱性 | 4 個 score factor + 3-arm UCB1 + 4 條 submit-path 網路呼叫，**飽和條件下未驗證** | 🟠 P1 | §3 |
| 目標差距 | DRL 在 sim 仍輸 score baseline 42–386%（v3 v3 更差） | 🟠 P1 | §4.3 |

對照 README 動機：

| 動機 | 兌現度 | 主要剩餘缺口 |
|---|:---:|---|
| 利用率（MPS 70%+） | 🟢 95% | per-job × per-GPU dashboard 尚缺（R20） |
| 隔離性（CPU/GPU 池獨立） | 🟢 100% | partition + StatefulSet 已實作 |
| 彈性（縮回 0 / 擴出） | 🟢 100% | Operator event-driven loop 已上 |
| 容錯（Checkpoint guard） | 🟡 75% | 單檔 path 限制（R15）仍在；無 chaos 量化（R18） |
| 端到端可觀測 | 🟢 100% | Phase 7-A 完工 |
| DRL 排程超越 score baseline | 🔴 30% | sim eval 仍輸；策略選擇見 §4.3 |

---

## 1. 程式碼品質

### 1.1 整體健康度（量化）

| 指標 | 數值 | 評估 |
|---|---|---|
| Python LoC（非測試） | 2865 (operator) + 2109 (sim) + 5283 (services) + 424 (eval) ≈ 10.7k | 適中 |
| ruff lint（E9/F811/F821/F841/W605） | 全部通過 | ✅ |
| pytest（sim + operator + weight_tuner） | 57 passed, 3 subtests passed in 3.21s | ✅ |
| Lua test | tests/lua/ × 2 檔（rl_hook_test、score_test） | ✅ |
| TODO/FIXME 註記 | 0 個 | ✅（罕見） |
| CI workflows | 5 個（python-lint / sim-unit / operator-test / lua-test / chart-ci） | ✅ |
| 超過 500 LoC 的單檔 | 7 個 | 🟡 應拆 |

ruff 規則集刻意保守——只開致命錯誤類，沒開風格類。對論文專案是合理折衷。

### 1.2 結構問題：`operator/app.py` 過於龐大

`OperatorApp` 一個 class 同時做：watch 三條（K8s STS / K8s Pod / Slurm diff）、reconcile queue、scale-up / scale-down 決策、drain-then-scale 狀態機、checkpoint guard、fragmentation 子 thread、OTel span lifecycle、Prometheus metrics。

| 函式 | LoC | 問題 |
|---|---:|---|
| `OperatorApp.__init__` | ~70 | 11 個 dict 狀態欄位，註解比程式碼還長 |
| `OperatorApp.run` | 115 | 啟動順序 + 第一輪 prime + watcher thread 起 + main loop 混雜 |
| `_process_pool` | 178 | 一個 pool 一輪 reconcile 的所有邏輯 |
| `_do_scale_down` | 121 | drain 起、完成、強制 timeout kill、checkpoint guard 互動 |

**建議重構（不需在 thesis 寫完前做，但要記在 backlog）：**

```text
operator/
  app.py                  ← 縮成 ~200 行（wire + main loop）
  watchers.py             ← _watch_statefulsets / _watch_pods / _poll_slurm_state
  reconciler.py           ← _process_pool + 狀態機
  scale_actions.py        ← _do_scale_up / _do_scale_down
  trace_lifecycle.py      ← OTel span 管理
```

拆完後每個檔 < 250 LoC、獨立可測試（目前 operator 只有 fragmentation + ghost_detector 有單元測試；scale-up/down 邏輯純靠 e2e）。

### 1.3 死碼：`services/rl_scheduler/` 中已淘汰的模組

`ruff.toml` 明確 exclude 以下檔案，意味著它們已不維護：

```
services/rl_scheduler/hierarchical.py        (461 LoC)  — D-LinUCB outer loop archived
services/rl_scheduler/ppo_train.py           (~210 LoC) — 早期 PPO 版本
services/rl_scheduler/ppo_masked_train.py    (~210 LoC) — PPO 加 action mask
services/rl_scheduler/snapshot_agent.py      (~110 LoC) — 早期 snapshot interface
eval/scripts/eval_hierarchical.py            (~50 LoC)
```

合計約 **1040 LoC 死碼**。CLAUDE.md 提到 `hierarchical.py`「archived」，但檔案還在 repo——對新讀者（包含未來的 thesis reviewer）會造成混淆。

**建議：** 確認無 import 後 `git rm`（archive 該去 git history，不該佔 working tree）。若想保留 PPO 作為 baseline 比較，至少在檔頂加 `"""DEPRECATED — kept for reference, not maintained."""`。

### 1.4 Lua 程式碼

`chart/lua/rl_hook.lua` 整體寫得很好（`pcall` 防爆、`curl --max-time` 防 hang、silent fallback），但有兩個小瑕疵：

**(a) `_bool_field` 有 dead code：**

```lua
local v = string.match(s, '"' .. name .. '"%s*:%s*(true|false)')  -- Lua pattern 無 alternation
```

Lua 的 pattern 不支援 `|`（那是 PCRE）。第一個 `string.match` 永遠 nil，實際靠後面兩行分別比對。功能正確，第一行可以刪。

**(b) 正則 JSON 解析的脆弱性：**

`_num_field` / `_str_field` 用 string.match 撈 scalar——**只在 serve.py 回傳 flat JSON 時可靠**。任何巢狀物件、escape 引號、scientific notation 都會 silent miss。目前 `DecideResponse` 全是 scalar，但未來若加 nested 欄位這層解析會悄悄壞掉。建議在 serve.py 加 integration test 斷言 Lua 端能解析新版回應。

### 1.5 Python 模組組織

**做得好：**

- `sim/` 與 `services/rl_scheduler/` 邊界清楚：sim 不 import torch，rl_scheduler 可 import sim
- `operator/fragmentation.py` 純 Python（無 K8s/Slurm 依賴），dataclass + 工廠注入 actuator——**極佳的可測試性**（19 個單元測試）
- `services/runtime_predictor/` 三件套（features/train/app）切得乾淨，替換模型骨架只需動 train.py
- `weight_tuner/bandit.py` UCB1 / LinUCB / RandomPolicy 同一介面，方便演算法比較

**待改進：**

- `services/rl_scheduler/serve.py` 與 `serve_otel.py` 分開——後者 ~70 行，是 OTel 沒裝時 import 不會炸的權宜之計。可 inline 進 serve.py 並 `try/except ImportError`
- `operator/app.py` 第 36 行 `import os` 散落在 imports 中間（ruff isort `I` 規則未開）
- `services/runtime_predictor/tests/` 缺 lightgbm/httpx 直接 import 失敗，**operator-test workflow 沒測這個服務**——若 runtime_predictor 是上線功能，CI 缺塊

---

## 2. K8s 工程實踐

依使用者要求，**不審 securityContext、HA、多租戶**；本節聚焦「正確性」與「運維友好度」。

### 2.1 Helm chart 整體評估

`helm lint chart/` → 通過（只有 icon 建議）。`helm template chart/` → 無錯。整體成熟：

| 實踐 | 狀態 | 備註 |
|---|:---:|---|
| `_helpers.tpl` 抽離 labels + partitionsJson | ✅ | 單一事實來源（slurm.conf + operator routing） |
| ConfigMap 改動觸發 pod roll via `checksum/config-*` annotation | ✅ | static / nodes / task-prolog 三條 |
| 預設 NetworkPolicy default-deny ingress + egress | ✅ | 白名單必要路徑 |
| `OnDelete` update strategy on worker StatefulSet | ✅ | 防 helm upgrade 殺 running job |
| `kubeVersion: ">=1.28.0-0"` | ✅ | Chart.yaml 明確 |
| Pre-decl values 給未啟用組件 | ✅ | overlays 一次寫對 |
| `PodDisruptionBudget` 每個關鍵 Deployment | ✅ | operator / login / controller |
| `runtimeClassName: nvidia` 條件渲染（只在 k3s + GPU pool） | ✅ | 防 Kind 缺 device-plugin |
| `helm-unittest` 28 條 | ✅ | render output 驗證 |

### 2.2 Operator 部署模式

| 項目 | 現況 | 評估 |
|---|---|---|
| Probes | readiness `test -f /tmp/operator-ready`；liveness `stat -c %Y /tmp/operator-alive` 配 120s 過期 | 🟡 heartbeat 寫法**聰明但脆弱**——`stat` 行為差異或時鐘漂移會誤判 |
| Resources | values.yaml **無** `operator.resources`；operator.yaml template 也沒 resource 區塊 | 🔴 **缺失**——詳見 §2.3 |
| RBAC scope | namespace-scoped Role + `pods/exec` | 🟡 `pods/exec` 是完整 pod 控制力，需註明是 slurmrestd 故障時的 fallback 路徑（operator.yaml 註解已說明） |
| ServiceAccount | 專屬 `slurm-elastic-operator`，無 cluster role | ✅ |
| Service `:8000` metrics | ✅ | Prometheus 直接 scrape |
| OTel env vars 條件注入 | `{{- if .Values.monitoring.otel.enabled }}` | ✅ |
| Leader election | 無（v4 R6 仍未解決） | 🟠 P1 — 短期靠單副本，thesis defense 時要有答案 |

### 2.3 重大發現：operator / controller / login 缺 resource limits

```bash
$ grep -A2 "resources:" chart/templates/operator.yaml
# 沒有任何 resource 區塊
```

`values.yaml` 也沒有 `operator.resources` 欄位。對比 `weightTuner` / `rlScheduler` / `runtimePredictor` 都有 `resources: requests/limits`，operator 是漏網之魚；controller 與 login（v4 R11）也是。

**影響：**
- pod 進入 BestEffort QoS，**OOM 第一個被殺**
- HPA / descheduler / bin-packing scheduler 看不到資源需求
- 在資源緊張的 k3s 單機上，operator 與 monitoring 競爭時最先被驅逐

**修法（10 行 × 3 模板）：**

```yaml
resources:
  requests:
    cpu: {{ .Values.operator.resources.requests.cpu | default "100m" | quote }}
    memory: {{ .Values.operator.resources.requests.memory | default "256Mi" | quote }}
  limits:
    cpu: {{ .Values.operator.resources.limits.cpu | default "500m" | quote }}
    memory: {{ .Values.operator.resources.limits.memory | default "512Mi" | quote }}
```

login pod 要更大（4 CPU / 8 GB）以防使用者意外大記憶體分配。

### 2.4 Workers 設計檢視（`chart/templates/workers.yaml`）

整個 chart 最複雜的模板（234 行 Go template + bash），但寫得清楚。

**做得好：**
- `updateStrategy: OnDelete`（R10）——helm upgrade 不會自動 roll worker
- `preStop` hook drain → sleep 10 → SIGTERM
- `resources.requests == resources.limits` → Guaranteed QoS（R7），與 Slurm NodeName CPUs/RealMemory 對齊
- `nvidia.com/gpu` 條件 gate——Kind 環境不會卡 device-plugin 配額
- `pgrep -x slurmd && pgrep -x munged` 雙條件 readiness——只看 slurmd 不夠（munged 死了也算掛）
- ConfigMap checksum annotation 三條——任一改動觸發 roll

**待加強：**
- `command:` 內 bash script 80+ 行，**`|| true` 用很多次**抵消了 `set -euo pipefail`。建議拆 sub-script 寫進 ConfigMap
- `imagePullPolicy` 預設值若是 `Always`，離線環境會壞——values.yaml 應明示 `IfNotPresent`
- `livenessProbe` 只 `pgrep -x slurmd`——若 slurmd deadlock 但 process 活著，probe 抓不到。對 thesis demo 足夠

### 2.5 GPU Operator 隔離（值得讚許）

CLAUDE.md 與 Chart.yaml 都記錄了「**GPU Operator 故意不放進 chart**」的決定：

> NVIDIA GPU Operator … hardcodes `Release.Namespace` for all its DaemonSets and needs that namespace to be PSS=privileged for hostPath access to /dev/nvidia*, /run/nvidia/mps, etc. Our slurm namespace is PSS=baseline …

**正確且少見的工程紀律**。多數 Slurm-on-K8s 範例會圖方便把整個系統放 privileged，或硬塞 GPU operator 進同 namespace 然後一路放寬權限。Kelpflux 把 GPU operator 隔到自己的 `gpu-operator` namespace，slurm namespace 只留 device-plugin-config ConfigMap 和 node-labeler Job——**這是論文應該強調的工程選擇**（§4.2 銅獎之一）。

### 2.6 監控與 OTel（Phase 7-A）

`chart/templates/monitoring/` 八個模板全部用同一個 `{{ if .Values.monitoring.enabled }}` gate——dev 環境一鍵關掉，production 一鍵打開。

OTel 鏈路：

```text
Lua submit hook ─┐
                 ├─→ serve.py /decide 開 job_submit span → traceparent
                 │      ↓ 寫進 admin_comment
slurmctld ───────┘
                        ↓
Operator polling loop ─→ 讀 admin_comment 還原 context
                        → queue_wait span (PENDING)
                        → job_running span (RUNNING transition)
                        → 結束 span 當 job 離開 squeue
                        → scale_up_decision + k8s_provisioning span（含歷史 start_time_ns）
                        → Prometheus exemplar 連 traceID 進 Tempo
```

**這個架構在開源 Slurm-on-K8s 圈是新的**（對比 Slinky/SUNK/Slonk 均無），詳見 §4.2 第 1 點。

### 2.7 其他不應忽視的小議題（K 系列）

| # | 議題 | 嚴重度 | 修法成本 |
|---|---|:---:|---|
| K1 | operator/controller/login 缺 resources requests/limits | 🟠 P1 | 10 行 yaml/template × 3 |
| K2 | NodePort 30022 在 k3s 上暴露外網但無 LoadBalancer/Ingress 選項 | 🟡 P2 | service.type 可選性增加 |
| K3 | Tempo PVC 用 RWO + 5Gi 寫死，無 retention 配置 | 🟡 P2 | values 暴露 retention 參數 |
| K4 | `values-k3s.yaml` 與 `values.yaml` drift 偵測缺失 | 🟢 P3 | 寫個 `scripts/check-values-drift.sh` |
| K5 | NFS PVC 沒 `mountOptions`（沿用 v4 R14） | 🟠 P1 | StorageClass 加 `rsize=1M, wsize=1M, hard, intr` |
| K6 | `chart-ci.yml` 是否跑 `helm lint` + `helm template` + `kubeval` 待驗證 | 🟡 P2 | 半天 |

---

## 3. 排程與資源分配的客製設計可靠度 ★

這是本輪審查的核心新增章節。Kelpflux 在 Slurm 之上疊了**四層客製**：score function（Lua）、weight tuner（UCB1）、runtime predictor（LightGBM）、RL scheduler（DSAC）。問題：這套設計**穩定嗎？有沒有更穩的替代方案？**

### 3.1 客製化堆疊現況

```
sbatch foo.sh
  ↓
slurmctld
  ↓ JobSubmitPlugins=lua
job_submit.lua（plugin load 時拉一次 /weights）
  ├─→ runtime_predictor /predict   ← Phase 6 M6（已啟用）
  ├─→ rl_scheduler   /decide       ← Phase 6 M11（shadow mode 預設）
  └─→ 計算 priority = score_gain × (α·mps_fit + β·vram_fit − δ·frag + ε·rt_short)
                                                                ↑
                                                          α,δ,ε 來自 UCB1 bandit
  ↓
slurmctld 用 priority 排序 pending queue
  ↓
slurmd 跑 job
  ↓
operator 觀察 squeue → scale up/down worker StatefulSet
  ↓ shadow
fragmentation reconciler（Gandiva-lite）→ 偵測碎片化 → 候選 requeue
```

每個 sbatch 在 worst case 觸發 **2 條對外網路呼叫**（runtime_predictor、rl_scheduler；weight_tuner 只在 plugin load 時拉一次），加 score 計算 + slurmctld 排序。整條路徑全部**設計成 silent fallback**。

### 3.2 可靠度評估（按關鍵性排序）

#### ✅ 做得好的地方

| # | 設計 | 為何穩 |
|---|---|---|
| R-1 | Lua `pcall` + `curl --max-time` + 失敗回 score baseline | 任何服務 down/慢都不會 block slurmctld；最差情況退回手工 score |
| R-2 | weight_tuner 只在 plugin load 時拉一次 `/weights` | 線上 submit latency **不依賴**weight_tuner 可用性；service 掛掉只影響權重更新 |
| R-3 | runtime_predictor 冷啟動有 `bootstrapFloorSeconds=14400` fallback | 模型沒訓好時不會回詭異值 |
| R-4 | rl_scheduler 預設 `shadowMode=true`、`valueAbstain=-1.0`、`entropyAbstain=1.5` | 模型不確定時 abstain，不會把錯誤決策推上去 |
| R-5 | fragmentation reconciler 預設 `shadowMode=true` + rate-limit (`maxRequeuesPerHour=5`, `maxTargetsPerDecision=4`) | 即使誤判也不會在 5 分鐘內把整個 cluster 重排 |
| R-6 | operator `_PoolEventQueue` 同 pool dedup | 多 watcher 同時 fire 不會把同 pool 反覆 reconcile |
| R-7 | `OnDelete` worker update strategy + `preStop` drain | helm upgrade 不殺 running job |
| R-8 | `slurm.k8s/last-scale-up-at` annotation 持久化 cooldown | operator pod restart 不會立即觸發 scale-down |

這 8 條合起來是這個系統真正成熟的部分。

#### 🟠 可靠度風險（按嚴重度排序）

**S1 [P1]：4-factor score function + 3-arm UCB1 在飽和條件下未驗證**

當前 score 公式：

```
priority = score_gain × (α·f_mps_fit + β·f_vram_fit − δ·f_fragmentation + ε·f_runtime_short)
```

問題：
- **4 個 factor + 3 個可調權重 = 7 個 free parameter**——對只有 17 個 action 的小 cluster，這個 search space 與 RL 同樣稀疏
- UCB1 用 mean-JCT 當 reward，但 mean-JCT **受 workload mix 影響極大**——不同時段的 trace 分佈不同會讓 bandit 在 arm 之間反覆橫跳
- 唯一驗證資料來自 sim（`tests/lua/score_test.lua` + sim eval）；**真機在 saturated condition 下沒有 ablation**

**比較：Slurm 原生 multifactor 的權重**（PriorityWeightAge、PriorityWeightFairshare、PriorityWeightQOS）通常**靜態 config**，半年到一年 admin 手動調一次。穩定性高、可解釋性高，但反應慢。

**Kelpflux 的選擇是「動態 = 更好」的賭注**——對研究專案合理，對 production 需要更多驗證。

> **建議：** thesis evaluation 加一節「Score Function Stability Under Load」，跑 5 種不同 workload mix（philly / burst / ali / 50-50 mix / 過載 mix），記錄 bandit converged arm 與 JCT。如果 bandit 在 mix 切換時花 > 30 分鐘才收斂，就要在文中坦承這個限制。

**S2 [P1]：score function 邏輯在 Lua + Python 兩處實作**

`sim/scheduler/score.py` 註解明確說：

> The Lua plugin in `chart/templates/configmap-job-submit.yaml` evaluates the same factors at sbatch time. Here we reimplement them in Python so the offline simulator can run without slurmctld. **Coefficients and tier list match the chart defaults — keep them in sync when tuning.**

「keep them in sync」是已知的 **drift hazard**。一個地方改了 vram_tiers，另一個沒改，sim 與真機行為就不一致——這會讓 thesis 的「sim 結果可外推到真機」主張站不住。

**修法選項：**

| 方案 | 工程量 | 收益 |
|---|---|---|
| A. 寫個 Lua↔Python parity test：給定相同 (job, cluster state) 兩邊算出的 score 必須一致 | 1 天 | 抓住 drift，但仍需手工同步 |
| B. 把 score 計算抽到一個 service（runtime_predictor 一樣的模式），Lua 用 curl 拉、sim 用 import 算 | 3–5 天 | 單一事實來源；增加一條 sbatch network call |
| C. 用 sqlite / 共用 config 檔，兩邊都 parse | 2 天 | 折衷；需要 deploy 對齊 |

**建議走 A**——成本低、能抓 95% 的 drift。

**S3 [P1]：4 條 submit-path 網路依賴的失敗矩陣未測**

當前 submit path 對外依賴（按時間順序）：

| 階段 | 服務 | 必要性 | 失敗行為 |
|---|---|---|---|
| plugin load（slurmctld 啟動） | weight_tuner /weights | 否 | 用 chart default 權重 |
| 每個 sbatch | runtime_predictor /predict | 否（Phase 6 M5 wired） | f_runtime_short = 0.5（中立） |
| 每個 sbatch | rl_scheduler /decide | 否（shadow 預設） | 跳過 RL 路徑 |
| 每個 sbatch | （score 本身在 Lua 算，無網路依賴） | — | — |

**每條都有 silent fallback**，但**全部同時掛**時的綜合行為沒驗證。例如：
- runtime_predictor 掛 → ε·f_runtime_short ≈ 0.5 × ε
- rl_scheduler 掛 → 跳過 RL，純 score
- weight_tuner 掛（從未啟動）→ A/D/E 用 chart default（0.40, 0.20, 0.30）

最差情況：sbatch 仍然能成功提交，priority 計算降級為「chart default 權重 × 缺 runtime_predictor 的 score」——**仍是合理的 baseline**，這個降級是優雅的。

但 latency 怎麼樣？目前 Lua 用 `curl --max-time 0.15`（rl_scheduler）+ `curl --max-time 0.5`（weight_tuner）+ runtime_predictor timeout（200ms in scheduler.md M6 risk note）。worst case 一個 sbatch 加 **~850ms 延遲**——這對互動式 sbatch 還可以，但對腳本批量 submit 1000 個 job 會累積到 14 分鐘。

> **建議：**
> 1. 寫 `scripts/chaos/submit-with-services-down.sh`：先殺 rl_scheduler、再殺 runtime_predictor、再殺 weight_tuner，每殺一個跑 100 次 sbatch 測 p99 latency
> 2. CI 跑這個的精簡版（只殺 rl_scheduler、跑 10 次）

**S4 [P2]：所有外掛 service 都是 single replica**

`chart/values.yaml` 的 `rlScheduler`、`weightTuner`、`runtimePredictor` 都沒有 `replicaCount` 欄位，模板預設 1 副本。雖然有 silent fallback 不會 block submit，但**重啟期間（image pull / OOM）所有 sbatch 都會降級**。

對 thesis demo 沒關係（單機 k3s 也不需要副本），但對「production grade」主張要打折。

**修法：** values.yaml 暴露 `replicaCount`、加 PodDisruptionBudget、加 anti-affinity。weight_tuner 因為有 state（UCB1 bandit），要做 leader election 才能多副本（或寫入 NFS 共享 state）；rl_scheduler 與 runtime_predictor 是 stateless，可直接多副本。

**S5 [P2]：fragmentation reconciler 從未脫離 shadow mode**

CLAUDE.md 確認：「The fragmentation reconciler (`operator/fragmentation.py`) is currently `shadowMode=true` — it logs but does not requeue victims.」

這代表 v4 提到的 Gandiva-lite 演算法**寫了單元測試（19 個 PASS）但實機從未驗證**。問題：
- 真實 cluster 的 squeue refresh cadence vs decider 的 `min_interval_seconds=60` 互動沒測
- 真實 ckpt restart 成本（13GB ckpt 從 NFS 重載要多久）沒量化
- 真實 priority gap 在 multifactor 啟用後是不是符合 `priority_gap=0` 的假設沒驗

**這對「容錯」與「彈性」的兌現度都有影響**——v4 表中 75% 的容錯兌現度，是因為這條未驗證的路徑。

**建議：**
1. 在 sim 加 fragmentation 場景測試（5 個低 prio mps:25 job 卡住 3 個高 prio mps:50 job 的場景），驗證 decider 出的 victim 是正確的
2. 在真機開 shadow mode 觀察 1 週的 log，確認 decider 沒在誤判
3. 第 3 週才 flip 到 `shadowMode=false`，加 rate limit + dashboard 監控

**S6 [P3]：runtime_predictor 冷啟動偏差**

預設 `minTrainSamples=100`、`bootstrapFloorSeconds=14400`（4 小時）。新 cluster / 新使用者的前 100 個 job：
- runtime 預測都回 14400 秒
- `f_runtime_short = exp(−14400 / runtime_horizon)`，runtime_horizon=3600 → `f_runtime_short ≈ 0.018`
- 所有新 job 的 ε·f_runtime_short 都被壓到接近 0
- ε=0.30（chart default）下，這個 factor 對 priority 貢獻 ~5（相對其他項 100–400），影響很小

**結論：** 冷啟動偏差確實存在，但量級小到不影響排程，是可接受的設計。**但要在 thesis 寫明這個 graceful degradation**，不要假設 reader 會自己想到。

### 3.3 替代方案比較

| 方案 | 穩定性 | 反應速度 | 工程量 | 適合場景 |
|---|:---:|:---:|:---:|---|
| **A. Slurm-native multifactor（PriorityWeightAge/Fairshare/QOS）** | ⭐⭐⭐⭐⭐ | ⭐ | 0（內建） | 大型生產 HPC、admin 半年調一次 |
| **B. Kubernetes-native Kueue / Volcano** | ⭐⭐⭐⭐ | ⭐⭐⭐ | 中（取代 operator 一大塊） | K8s-first、容忍失去 Slurm CLI 相容 |
| **C. Kelpflux 現狀**（score + bandit + RL） | ⭐⭐⭐ | ⭐⭐⭐⭐ | 高（已投入） | 研究、能容忍未驗證情境 |
| **D. 簡化版 Kelpflux**：保留 score function + 拿掉 RL/bandit | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | 低（拔掉 service） | 「保留 MPS-aware 排程，但回到靜態權重」的妥協 |

**我的建議路徑：**

對 **thesis 主軸**——保留 C（現狀），但在 evaluation 章節加：
1. **C vs A 對比**（同 workload 跑 Slurm multifactor 看 JCT），證明動態調權有實質收益
2. **C 在 service 失敗下的降級行為量化**（§3.2 S3 建議的 chaos 測試）
3. **D 作為 sensitivity analysis**：「如果只保留 score function，效能差距多大？」——這給未來生產化提供路徑

對 **若論文走「實用平台」框架**——可以提供 D 作為「production-recommended profile」，C 作為「research mode」，文件清楚分開。

### 3.4 結論

當前客製排程設計的可靠度評估：

| 面向 | 評分 | 說明 |
|---|:---:|---|
| Submit-path 失敗韌性 | ⭐⭐⭐⭐ | 4 條網路依賴都有 silent fallback，最差情況優雅降級 |
| Score function 設計 | ⭐⭐⭐ | 4 factor + 3 bandit arm 在 saturated 下未驗證 |
| Lua/Python parity | ⭐⭐ | sim 與真機計算分兩處實作，drift hazard |
| 外掛 service 部署 | ⭐⭐⭐ | 單副本 + shadow mode 為主，可接受但非 HA |
| Fragmentation reconciler | ⭐⭐ | 19 個單元測試 PASS，實機從未驗證 |
| Operator 與 Slurm 互動 | ⭐⭐⭐⭐ | drain-then-scale、cooldown、event dedup 都成熟 |

**整體**：⭐⭐⭐ — 對研究專案 + 單機 thesis demo 足夠，對 multi-tenant production 需要 §3.2 S1/S2/S3/S5 四個 P1 補完。

---

## 4. 目標達成度與系統特色

### 4.1 README 動機項對照

| 動機 | 技術手段 | 兌現度 | 證據 |
|---|---|:---:|---|
| **利用率 70%+（MPS 共享）** | gres mps:25、`f_mps_fit` bin-pack 分數 | 🟢 95% | gres.conf 正確、score function unit test |
| **隔離性（CPU/GPU pool 分離）** | 多 pool partition、partition→StatefulSet | 🟢 100% | values.yaml pools[] 已實作 |
| **彈性（縮回 0 / 擴出）** | Operator + scale annotation cooldown | 🟢 100% | `minReplicas: 0` 已驗 |
| **容錯（checkpoint guard）** | `CHECKPOINT_PATH` 檔案存在檢查 + grace | 🟡 75% | 單檔限制（R15）仍在 + 無 chaos 量化 |
| **端到端可觀測** | Prometheus（20+ metrics）+ Grafana + Tempo + OTel | 🟢 100% | Phase 7-A 完工 |
| **DRL 排程超越 score baseline** | DSAC + PER + n-step + shaping + CQL | 🔴 30% | sim eval 仍輸 42–386% |

**核心動機 5/6 兌現，DRL 是最大缺口**——§4.3 詳述。

### 4.2 系統特色清單（vs Slinky / SUNK / Slonk / AWS ParallelCluster / Volcano）

依「業界沒人做的程度 × 技術新穎度 × 對論文敘事的幫助」三軸排序：

#### 🏆 特色 1：traceparent-in-admin_comment 的 sync/async 橋接（**最強差異化**）

Lua submit hook 是 sync，Operator polling loop 是 async；要把兩者用同一個 trace 串起來，業界做法是要嘛犧牲完整性（兩段分開），要嘛強制改 slurmctld 行為（侵入性高）。

Kelpflux 的解法：

```text
serve.py 在 /decide 時開 root span
  → 序列化成 W3C traceparent
  → 塞進 DecideResponse JSON
Lua hook 取出 traceparent
  → 寫進 job_desc.admin_comment（Slurm 內建欄位，落地到 squeue）
Operator 看到 PENDING job
  → 讀 admin_comment, parse traceparent
  → extract_context() 還原 OTel context
  → 開 queue_wait span 接在 root 底下
```

**為何新：**
- 不需 slurmctld 改動或 plugin
- 不需側通道
- 完全無侵入：OTel 沒啟用時 `admin_comment` 仍可正常用
- W3C 標準格式，下游 OTel 工具能接

**論文敘事價值：** 可以是一節「Distributed Tracing in Hybrid Sync/Async Schedulers」，配時序圖很有畫面。建議寫成 thesis chapter 3 的 mini case study。

#### 🥈 特色 2：MPS-aware score function with bandit weight tuning

```
priority = α·f_mps_fit + β·f_vram_fit − δ·f_fragmentation + ε·f_runtime_short
```

每個 factor 有 unit test 驗證（`tests/lua/score_test.lua`）。`f_runtime_short` 接 LightGBM runtime predictor（冷啟動有 fallback floor），`f_mps_fit` 直接做 bin-pack 評分。

外層 weight_tuner 用 UCB1 bandit 線上調 (α, δ, ε)，β 固定。**這比 Slurm 原生 PriorityWeight 之類的固定權重靈活很多**——多數 HPC 系統 admin 半年手動調一次，這個 bandit 5 分鐘一輪自動跑。

**論文敘事：** evaluation 章節可加一節 ablation「weight tuner on/off vs handcoded」，量化 bandit 收益。**注意**：§3.2 S1 指出 bandit 在 saturated condition 下未驗證——若收益不顯著，要在 thesis 坦承。

#### 🥉 特色 3：Checkpoint-aware scale-down + GPU Operator namespace 隔離

兩個工程選擇加起來形成「production-grade」差異化：

- **Checkpoint-aware scale-down**：v4 列為「沒人做」的功能。Slinky / SUNK / Volcano 都靠手動或 Pod Disruption Budget；Kelpflux 直接在 operator 層檢查 ckpt mtime + grace period
- **GPU Operator namespace 隔離**：§2.5 已述

兩者合起來是「在不放寬安全前提下保住功能完整」的工程信號。

#### 銅獎候選：sim-to-real RLPD scaffolding

DSAC + PER + n-step + score warmup + offline-online mixing + curriculum 全部接好，sim → 訓練 → serve → shadow → live log → RLPD fine-tune 全管線跑得通。**雖然目前還輸 score baseline**，但 infrastructure 完整度遠超大多數論文 prototype。

#### 銅獎候選：Service degradation safety nets 的一致性

§3.2 R-1 至 R-5 列出的 silent fallback 模式在 rl_scheduler、weight_tuner、runtime_predictor、fragmentation reconciler 都一致——**這是「整套系統可以放到 production 試用」的工程信號**。

### 4.3 DRL 落後 baseline 的根因 + 路徑選擇

§6（eval-writeup）已記錄 v3 衰退分析，這裡補充策略：

**已知問題：**
1. 1×1 cluster action space 只有 17——RL 學不出比手工 score 好的策略
2. JCT reward 太稀疏——episode 結束才有大信號
3. v3 curriculum 把訓練量稀釋到只有 ~24 個 n_jobs=50 episode

**三條路徑：**

| 路徑 | 投入 | 預期回報 | 風險 |
|---|:---:|:---:|---|
| A. 2×2 cluster + n_actions=65 + 500k steps（不開 curriculum） | 3–4 週 | 可能持平 baseline | 仍可能輸 |
| B. score-residual learning：RL 學 `score + Δ` 而非從零 | 2 週 | **大概率超 baseline** | 偏離 pure RL 敘事 |
| C. 接受 RL 輸，論文重心改成「sim-to-real 平台 + OTel 差異化」 | 1 週改文 | 100% 兌現 | 失去 RL 主題 |

**建議：B + C 混合**——

- 主敘事走 C（平台 + OTel + MPS score function 三大特色）
- B 作為「探索性結果」放 appendix（score-residual RL 是新穎的 hybrid，即使量化收益小也有方法論貢獻）
- A 留作 future work

### 4.4 業界比較（v5 更新）

| 面向 | 本專案 | Slinky | SUNK | AWS ParallelCluster | Volcano |
|---|:---:|:---:|:---:|:---:|:---:|
| Helm 一條指令部署 | ✅ | ⚠️（多 chart） | ✅ | n/a | ✅ |
| GPU MPS sharing | ✅ | ⚠️（timeSlicing） | ✅ | ✅ | ✅ |
| **端到端 Job lifecycle trace（OTel）** | **✅（7-A 完成）** | ❌ | ❌ | ❌ | ❌ |
| Per-job × per-GPU × full-lifecycle dashboard | 🟡 部分（缺 GPU 細粒度） | ❌ | ❌ | ⚠️ | ❌ |
| ML-aware scheduling（runtime / score） | ✅（Phase 6 完成）| ❌ | ❌ | ❌ | ⚠️ |
| **Sim-to-real DRL platform** | **✅** | ❌ | ❌ | ❌ | ❌ |
| **UCB1 bandit weight tuning** | **✅** | ❌ | ❌ | ❌ | ❌ |
| Chaos / failure injection test suite | ❌（R18） | ❌ | ❌ | ⚠️ | ❌ |
| Checkpoint-aware scale-down | ✅ | ❌ | ❌ | ❌ | ❌ |
| Drain timeout（hang job 不卡死） | ✅ | ⚠️ | ✅ | ✅ | ✅ |
| Fairshare / QoS 啟用 | ❌（4-A/B/C） | ✅ | ✅ | ✅ | ✅ |
| HA Controller | ❌ | ✅ | ✅ | ✅ | ✅ |
| 共享 FS | NFS（瓶頸） | StorageClass | Lustre | FSx | StorageClass |

**獨佔的三件事：**
1. 端到端 OTel trace + Prometheus exemplar 連 Tempo
2. UCB1 bandit 自動 tune score weights
3. Sim-to-real DRL 平台（PPO / DSAC / RLPD 全管線）

**短期沒打算追平：** HA controller、Fairshare、共享 FS 升級（保留至 v6 / 未來工作）

---

## 5. 改進優先順序總表（v5）

按「**對 thesis 兌現度影響 × 工程量**」綜合排序。★ 是「做了會出色」而非「不做會壞」。

### 5.1 修了不會壞、不修會被問

| # | 議題 | 類別 | 嚴重度 | 工程量 |
|---|---|---|:---:|:---:|
| C1 | 補 `services/rl_scheduler/` 單元測試（dsac/per/n-step） | 測試覆蓋 | 🟠 P1 | 1 天 |
| K1 | operator/controller/login 補 resources requests/limits | K8s | 🟠 P1 | 10 行 yaml × 3 |
| S1 | Score function stability under load 量化 + ablation | 排程可靠度 | 🟠 P1 | 1 週 sim 跑 |
| S2 | Lua/Python score parity test | 排程可靠度 | 🟠 P1 | 1 天 |
| S3 | Submit-path 失敗矩陣 chaos 測試（殺 rl/predictor/wt） | 排程可靠度 | 🟠 P1 | 半週 |
| R6 | Operator leader election（短期：reduce RTO；長期：active-passive） | 可靠度 | 🟠 P1 | 中 |
| R14/K5 | NFS mountOptions tuning | 儲存 | 🟠 P1 | 1 行 |

### 5.2 修了會更好但 thesis 沒它也行

| # | 議題 | 類別 | 嚴重度 | 工程量 |
|---|---|---|:---:|:---:|
| C2 | 拆 `operator/app.py`（906→5×~200） | 程式碼結構 | 🟡 P2 | 2 天 |
| C3 | 刪除 `hierarchical.py` / `ppo_*.py` / `snapshot_agent.py` 死碼 | 程式碼乾淨 | 🟡 P2 | 10 分鐘 |
| C5 | 拆 `docs/note.md` 72KB | 文檔可讀性 | 🟡 P2 | 1 天 |
| S5 | Fragmentation reconciler 真機 shadow validation | 排程可靠度 | 🟡 P2 | 2 週觀察 |
| S4 | rl/predictor/wt service 多副本 + PDB | K8s | 🟡 P2 | 半天 + 1 天 leader election |
| R11 | Login pod resource limit（已併入 K1） | — | — | — |
| R15 | Checkpoint guard 多 pattern | 彈性 | 🟡 P2 | 半天 |
| 4-A/B/C | QoS / Preempt / Fairshare 啟用 | 排程 | 🟡 P2 | 中 |

### 5.3 修了會出色 ★

| # | 議題 | 類別 | 性質 | 工程量 |
|---|---|---|:---:|:---:|
| ★F1 | thesis 重新組織敘事框架成「平台 + OTel + MPS score function」 | 論文敘事 | ★★★ | 1 週改文 |
| ★F2 | 補 score-residual RL ablation | 演算法貢獻 | ★★ | 2 週實驗 |
| ★F3 | 加「Engineering Choices for Production-Grade Slurm-on-K8s」一節 | 工程深度 | ★ | 半週寫文 |
| ★R18 | Chaos / failure injection test suite | 容錯量化 | ★★ | 中 |
| ★R19 | submit helper 補 `--mem` / partition / qos | 易用性 | ★ | 中 |
| ★R20 | Per-job × per-GPU × full-lifecycle dashboard | 視覺化 | ★ | 中 |
| ★R16 | OTel chart artifact 發佈（GitHub Pages / OCI registry） | 散播 | ★ | 1 天 |

### 5.4 鎖定中（v4 沿用、現況未變）

| # | 議題 | 狀態 | 解鎖條件 |
|---|---|:---:|---|
| R9 | cgroup-based proctrack — deferred | 🔒 | R13 完成且 chart 加 PSS=privileged overlay |
| R13 | Slurm 21.08 → 23.11 升級 — 嘗試後 deferred | 🔒 | PSS=privileged overlay；或 Slurm 上游 `--without-systemd`；或 source build 鎖 23.02 LTS |
| 5-D | 雙網路 / Multus | ⛔ | 移除 roadmap（單機 2 GPU 走 loopback） |

---

## 6. 給 thesis 收尾的建議切入順序

對單人 1 學期工程量的最務實順序（**較 v4 收緊到 8 週**，因為 Phase 6/7 已完工，剩下是補完 + 寫文）：

```
Week 1 (NOW)：F1 — 重新組織 thesis 敘事框架（平台/OTel/score function 三大特色）
              C3 — 刪死碼（10 分鐘換潔淨 repo snapshot）
              K1 — 補 resources limits（避免 demo OOM）
Week 2     ：S1 + S2 — Score function ablation + Lua/Python parity test
              C1 — 補 rl_scheduler unit tests
Week 3     ：S3 + R18 — Submit-path chaos 測試（順便算 evaluation 容錯數字）
              R20 起步 — per-job GPU dashboard mock
Week 4     ：F2 — score-residual RL ablation 實驗（可選）
              R20 完成 — per-job dashboard 上線
Week 5     ：F3 — 寫「Engineering Choices」章節
              R6 — operator leader election（短期版）
Week 6–8   ：Thesis writeup + figures + appendix
```

**論文章節骨架建議：**

```
1. Introduction & Motivation
2. Background（Slurm、K8s、MPS、Distributed Tracing、RLPD）
3. System Architecture
   3.1 Layered design（Slurm + K8s + 4 客製層）
   3.2 OTel sync/async bridge ★（特色 1）
4. Scheduler Design
   4.1 MPS-aware score function ★（特色 2）
   4.2 UCB1 weight tuning ★（特色 2）
   4.3 DRL exploration (DSAC + RLPD) — 含 sim eval 落後分析
5. Engineering Choices for Production-Grade Slurm-on-K8s ★（特色 3 + 銅獎候選）
   5.1 Worker update strategy & drain semantics
   5.2 GPU Operator namespace isolation
   5.3 Service degradation safety nets（§3.2 R-1..R-8）
   5.4 ConfigMap checksum rolling
6. Evaluation
   6.1 Score baseline vs FCFS / multifactor
   6.2 DRL vs score baseline（坦承落後 + 分析根因）
   6.3 Weight tuner ablation（S1）
   6.4 Submit-path chaos test（S3）
   6.5 OTel overhead measurement
7. Related Work（Slinky/SUNK/Slonk/ParallelCluster/Volcano 比較表）
8. Conclusion & Future Work（HA、多租戶、cgroup 升級路徑）
Appendix A: Reproducibility（每個 figure 附 git SHA + cmd）
Appendix B: v3 ablation 衰退分析（eval-writeup §6 移過來）
Appendix C: Score-residual RL（如果 F2 做）
```

---

## 7. 收尾觀察

**這個專案的真正強項**不是 DRL，是「**整套可重現的 Slurm-on-K8s research/production testbed**」：

- Helm chart 成熟到一行指令上線
- 模擬器、訓練、serve、live deployment 全管線串通
- OTel trace 串起 sync/async 邊界（業界沒人做）
- shadow mode + silent fallback 一致的工程紀律
- 文檔完整到 reviewer 可以 reproduce

**這個專案的真正弱項**是「**研究敘事與工程貢獻不匹配**」：

- 論文標題或主軸若還是「DRL scheduler」，evaluation 章節會打臉自己
- 動態權重 + 4 factor score + RL fallback 的「客製排程穩定性」沒被當作主貢獻寫
- v3 衰退分析是個誠實的結果，但需要框架支撐才能變成 thesis contribution

**v5 結論：** 工程能力遠超論文標題目前的承諾。建議用 F1 把標題與敘事重新校準，把已經做出來的「平台特色 + OTel 橋接 + score function 工程深度」推到 thesis 主貢獻；DRL 作為 honest negative result + future work，反而是更紮實的學術姿態。

---

*v5 審核以 Phase 6/7-A/7-B 完工 + GitHub Actions CI 上線後的 main @ `04984a6` 為基底；資安、HA、多租戶依使用者要求暫不審。下次 v6 預計在 thesis chapter 3 寫完後重審「論文敘事 vs 程式碼證據」一致性，並補 securityContext 審計。*
