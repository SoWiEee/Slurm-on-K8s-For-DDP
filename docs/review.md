# Kelpflux 系統審查報告（v6）

> **評估時間：** 2026-05-31
> **評估快照：** main @ `b77f3f3`
> **評估視角：** HPC 叢集工程師 + K8s SRE + ML systems
> **評估範圍：** 目前上線規格、DRL/live scheduler、Helm/K8s、CI、可觀測性、論文/研究可交付性。

---

## 0. 執行摘要

目前 Kelpflux 已經不是早期 prototype，而是一套可部署、可觀測、可驗證的 Slurm-on-K8s research platform。核心能力已到位：

| 面向 | 現況 | 評估 |
|---|---|---|
| Slurm-on-K8s 基礎 | Helm chart、controller/login/worker/operator、multi-pool routing | 穩定可 demo |
| MPS-aware 排程 | Lua submit score、VRAM/MPS/fragmentation/runtime factors、score parity test | 工程完整 |
| DRL live scheduler | DSAC checkpoint baked into image，`rlScheduler.shadowMode=false` 可 live boost | 可上線，但效果仍需量化 |
| 可觀測性 | Prometheus、Grafana、Tempo、OTel、scheduler live dashboard、per-job GPU dashboard | 明顯強項 |
| CI | 7 個 workflow：lint、chart、Lua、operator、sim、RL unit、score parity | 已覆蓋主要風險 |
| 失敗降級 | runtime predictor / RL / weight tuner 皆 silent fallback；chaos script 已補 | 設計正確，但仍需跑實機數據 |

**最重要的判斷：** 這個系統目前最強的貢獻不是「DRL 已經打敗 baseline」，而是「把 Slurm、K8s、MPS、DRL、OTel 串成可重現的實驗平台」。若論文或專題敘事仍只押在 DRL 效能優於 score baseline，風險偏高；若主軸改成 **ML-assisted resource allocation platform + observability + safe live rollout**，目前證據更完整。

目前仍值得優先改進的地方剩下五類：

1. **DRL 評估缺口**：DSAC 已能 live，但尚未證明在 2×2 cluster 或真實 workload 下優於 score baseline。
2. **2×2 cluster 實驗環境**：目前單機 1×GPU 的 action space 太小，無法支撐 DRL 做出真正不同於 heuristic 的決策。
3. **生產化韌性**：operator 無 leader election；RL/predictor/weight-tuner 仍單副本；runtime predictor 模型 PVC 為 RWO。
4. **fragmentation requeue**：程式與測試存在，但上線仍應保持 shadow；目前 victim selection 曾在 eval 中造成 JCT regression。
5. **研究結果包裝**：需要把 evaluation 寫成可重現的實驗矩陣，而不是只展示系統能跑。

---

## 1. 目前系統狀態

### 1.1 上線路徑

目前 live submit path 是：

```text
user sbatch
  -> slurmctld loads job_submit.lua
  -> submit helper 補齊 mem / partition / qos
  -> Lua score function 計算 submit-time priority delta
  -> optional runtime_predictor /predict
  -> optional rl_scheduler /decide
  -> Slurm multifactor priority + explicit priority delta 排序
  -> worker StatefulSet / slurmd 執行 job
  -> operator 觀察 squeue/sinfo 並 scale up/down
  -> Prometheus/Grafana/Tempo/OTel 收集 metrics/traces
```

`rlScheduler.shadowMode=false` 已設定，代表 RL 選中的 job 可以拿到 positive `priorityBoost`。同時 `serve.py` 仍有 `VALUE_ABSTAIN`、`ENTROPY_ABSTAIN`、snapshot TTL 與 no-op action 作為保護。

### 1.2 ML / DRL 管線

目前實際有用於 live 的 DRL 演算法是 **DSAC**，搭配：

| 元件 | 檔案 | 角色 |
|---|---|---|
| DSAC policy | `services/rl_scheduler/dsac.py` | 離散 action scheduling policy |
| Training | `services/rl_scheduler/sim_train.py` | simulator 中訓練 DSAC checkpoint |
| Live serving | `services/rl_scheduler/serve.py` | `/snapshot`、`/decide`、`/metrics` |
| Replay / fine-tune | `services/rl_scheduler/rlpd_finetune.py` | ReplayBuffer / PrioritizedReplayBuffer，支援 RLPD 方向 |
| Live daemon | `services/rl_scheduler/live_daemon.py` | 連接 live cluster snapshot / placement |

PPO 目前只剩 `smoke_ppo.py` 這類 smoke / historical utility，**不是目前 live scheduler 的主線**。文件若仍把 PPO 寫成主演算法，應修正為「歷史探索或 smoke test」。

### 1.3 CI 與測試

目前 CI 已覆蓋：

| Workflow | 覆蓋範圍 |
|---|---|
| `python-lint.yml` | ruff check |
| `chart-ci.yml` | helm lint / template / helm-unittest / negative storage test |
| `lua-test.yml` | standalone Lua helper tests |
| `operator-test.yml` | operator / fragmentation / autoscale tests |
| `sim-unit.yml` | simulator unit tests |
| `rl-scheduler-test.yml` | RL scheduler serve + replay buffer unit tests |
| `score-parity-test.yml` | Helm-rendered Lua score vs Python reference parity |

這表示過去 review 中的 C1、S2 都已經補上。S3 也已有 `scripts/chaos/submit-with-services-down.sh`，但那是 live cluster script，不是 CI 內可完全自動化的測試。

### 1.4 可觀測性

目前 Grafana dashboard 包含：

| Dashboard | 用途 |
|---|---|
| `scheduler-live.json` | RL scheduler live 決策、snapshot、boost、abstain metrics |
| `per-job-gpu.json` | per-job / per-GPU drill-down 與 Tempo deep-link |
| `operator.json` | autoscaling / reconcile / provisioning latency |
| `gpu.json` | DCGM GPU metrics |
| `bridge-overview.json` | Slurm/K8s bridge overview |
| `sla-efficiency.json` | SLA / efficiency overview |

可觀測性已經是系統強項；下一步不是再加 dashboard，而是把 dashboard 截圖與 trace 範例放進 evaluation / thesis，證明系統能解釋每一次資源分配決策。

---

## 2. 系統強項

### 2.1 Safe live scheduling path

submit path 上的外掛 service 都設計成 optional：

| 服務 | 失敗時行為 | 評估 |
|---|---|---|
| runtime predictor | predictor factor 回中性值 / 不覆蓋 time limit | 合理 |
| RL scheduler | Lua hook skip RL，回到 score baseline | 合理 |
| weight tuner | plugin load 失敗時用 chart default weights | 合理 |
| OTel | tracing disabled / unavailable 不影響 submit | 合理 |

這是 HPC submit path 應該有的保守設計。即使 RL 已經 live，系統仍能在 model 不確定、snapshot stale、service down 時回到 baseline。

### 2.2 Observability is a real differentiator

`traceparent` 寫入 `job_desc.admin_comment`，再由 operator 還原 OTel context，是這個系統最有辨識度的工程設計。它解決了 Slurm submit hook 是同步、operator reconcile 是非同步的 trace 斷裂問題，而且不需要改 Slurm core。

這個特色比「又一個 RL scheduler」更容易說服 reviewer：它能展示每個 job 從 submit、queue wait、scale up、running 到完成的全鏈路，並能把 scheduler decision、GPU metrics、K8s provisioning latency 放在同一個觀察面。

### 2.3 Simulator-to-live pipeline is coherent

目前已具備：

```text
sim workload -> DSAC training -> dsac.pt -> Docker image -> Helm deploy -> live /decide -> metrics/traces
```

再加上 score parity test，至少可以保證 live Lua score 與 Python reference 不會悄悄漂移。這對後續實驗非常重要，因為你們要比較 score baseline、DSAC、DSAC residual 或 2×2 cluster 時，需要能信任 baseline 一致。

---

## 3. 主要風險與改進方向

### P1-1. 建立 2×2 cluster 實驗環境，否則 DRL 很難有表現空間

目前 1 台電腦 / 1×GPU 或近似 1×1 的 live setup，action space 太小。對 DRL 來說，很多情境其實只有「排或不排」而不是「資源分配」。這會讓 DSAC 很容易退化成 score baseline 的弱版本。

**建議目標：** 至少做 2 nodes × 2 GPUs 的實驗 profile，即使第二台機器先用 CPU-only 或 mock GPU 也可以先把 Slurm/K8s topology 與 action mask 跑通。真正有 GPU 的第二台再接上 DCGM/MPS。

**要補的內容：**

| 工作 | 目的 |
|---|---|
| `values-2x2.yaml` | 固定 2×2 cluster 實驗設定，避免手動 overlay 漂移 |
| `docs/cluster.md` 增補 join node 流程 | 第二台機器加入 k3s、label、NVIDIA runtime、MPS setup |
| simulator 2×2 eval matrix | DSAC 才有 placement / packing / fragmentation 決策空間 |
| live smoke test | submit 多個 mps:25/mps:50 job，確認 allocation / metrics / traces |

**驗收標準：** 不是「job 能跑」而已，而是 dashboard 能看到 queue、GPU allocation、MPS slot、CPU/memory、RL selected action，並且 Slurm allocation 與 RL `/decide` 回傳 placement 不矛盾。

### P1-2. 補 DRL vs baseline 的正式 evaluation matrix

目前 DSAC live 已接上，但還缺能支持結論的實驗表。建議最小矩陣如下：

| Dimension | Values |
|---|---|
| Cluster | 1×1、2×2 |
| Workload | philly_subsample、burst、mixed short/long、MPS-heavy |
| Scheduler | FCFS、Slurm multifactor、Lua score、DSAC live、DSAC shadow |
| Metrics | mean JCT、p95 JCT、GPU utilization、MPS utilization、queue wait、submit latency、abstain rate |
| Seeds | 至少 5 |

**關鍵點：** 若 DSAC 仍輸 score baseline，不要硬凹。可以把 DSAC 定位成「live-safe exploration layer」，主貢獻改成平台與可觀測性。若要追求演算法收益，下一步應做 score-residual learning，而不是讓 DSAC 從零學完整 policy。

### P1-3. 做 score-residual DSAC，而不是純 DSAC 從零學

純 DSAC 在小 cluster 會很難超過手工 score。更務實的做法：

```text
final_priority = score_priority + RL_delta
```

讓 RL 學「何時修正 heuristic」，而不是重學整個 scheduler。這也比較符合目前 live 架構，因為 Lua score 已經是穩定 fallback。

**建議設計：**

| 項目 | 建議 |
|---|---|
| Action | keep current placement/no-op，或改成 boost bucket |
| Reward | score baseline improvement：JCT delta / queue wait delta |
| Safety | RL_delta bounded，例如 `[-250, +250]` 或只允許 positive boost |
| Evaluation | 和 pure DSAC、score baseline 做 ablation |

這條路徑比「加大訓練步數」更有機會產生可解釋的改善。

### P1-4. 實機執行 submit-path chaos script 並把數字寫入 monitoring/eval

S3 的 script 已存在，但目前報告仍需要實際數據。建議在 live k3s 跑：

```bash
SAMPLES=50 bash scripts/chaos/submit-with-services-down.sh
```

至少記錄：

| Phase | 要看什麼 |
|---|---|
| baseline | 正常 submit latency p50/p95/p99 |
| rl-scheduler down | sbatch 是否仍成功、latency 是否受 150ms timeout 影響 |
| runtime-predictor down | predictor timeout 是否符合設定 |
| weight-tuner down | plugin load fallback 是否正常 |
| all optional down | 最壞情況 submit 是否仍成功 |

這會直接支撐「safe live ML scheduling」的主張。

### P1-5. Operator leader election / failover policy

operator 目前單副本，雖然有 PDB 和 liveness，但沒有 leader election。單機 thesis demo 可以接受；如果文件宣稱 production-grade，就會被問。

**最低成本改法：** 不一定要立刻 active-active。先做 active-passive leader election，只有 leader 執行 reconcile/scale；follower 保持 ready 但不操作 StatefulSet。

**驗收標準：** kill operator pod 後，新 pod 在一個 reconcile period 內恢復，且不重複 scale 或誤 drain。

---

## 4. P2 改進項

### P2-1. 外掛服務副本數與持久化

`rl-scheduler`、`runtime-predictor`、`weight-tuner` template 目前皆 `replicas: 1`。silent fallback 讓 submit 不會壞，但重啟期間會降級。

| 服務 | 現況 | 建議 |
|---|---|---|
| rl-scheduler | stateless，policy baked in image | 暴露 `replicaCount`，加 PDB |
| runtime-predictor | RWO model PVC，Deployment Recreate | 若要多副本，模型改 ReadOnlyMany 或 baked artifact |
| weight-tuner | state 在 `emptyDir` | 改 PVC 或 ConfigMap/SQLite state；多副本前需 leader election |

對目前單機環境，這不是立即阻擋；對 2×2 或長時間實驗，建議至少讓 rl-scheduler 可 2 replicas。

### P2-2. Runtime predictor CI coverage

目前 runtime predictor 是上線功能的一部分，但 CI 重點在 operator/sim/RL scheduler。建議新增輕量測試：

| Test | 目的 |
|---|---|
| feature extraction unit test | 避免欄位改名或 default drift |
| cold-start `/predict` test | 無模型時回 bootstrap floor |
| retrain smoke test | 小 trace 可產生 model artifact |

這能避免 predictor 在未來 refactor 時 silent break。

### P2-3. Fragmentation reconciler 繼續 shadow，不建議現在開 live

fragmentation decider 已有測試，但過去 eval 顯示 victim requeue 可能讓 mean JCT 變差。原因不是 checkpoint reload cost，而是 victim lost progress。

**建議：**

1. 繼續 `shadowMode=true`。
2. 加 elapsed-progress penalty：已跑很久的 job 不應被輕易 requeue。
3. 補 live shadow dashboard：每次 shadow victim decision 要能看到 target/victim/priority gap/estimated lost work。
4. 若要 live，先限制在低風險 partition 或測試帳號。

### P2-4. Checkpoint guard 支援多 pattern

目前 checkpoint guard 仍偏向單一路徑 / 單檔模型。實際 ML workload 常見：

```text
checkpoints/epoch_*.pt
last.ckpt
rank*/checkpoint.pt
```

建議把 `CHECKPOINT_PATH` 升級成 pattern list，並記錄最近 matched file 的 mtime/size 到 metrics。這能讓 scale-down guard 更符合真實訓練工作。

### P2-5. NFS / shared storage tuning

NFS 對 demo 足夠，但對 checkpoint-heavy workload 會成為瓶頸。短期至少補：

```text
rsize=1M,wsize=1M,hard,timeo=600,retrans=2
```

中期若論文要談 production，可把 shared FS 升級列為 future work：Lustre、CephFS、FSx for Lustre 或 local NVMe cache。

---

## 5. P3 清理與文件改進

| 項目 | 建議 |
|---|---|
| `services/rl_scheduler/smoke_ppo.py` | 改名或註解為 historical smoke，避免誤導目前仍用 PPO |
| `docs/note.md` | 拆成 archive / experiment log，避免主要文件入口太雜 |
| Dashboard screenshots | 把 scheduler-live / per-job-gpu 截圖放進 docs 或 thesis figures |
| `values-k3s.yaml` drift check | 加 script 比對關鍵欄位：MPS slots、GPU type、shadowMode、image tags |
| `serve_otel.py` | 可以維持獨立；若要簡化，再 inline 成 optional import |

---

## 6. 建議後續路線圖

### 最近 1 週

1. 跑一次 `scripts/chaos/submit-with-services-down.sh`，把 p50/p95/p99 latency 寫進 `docs/eval-writeup.md` 或 `docs/monitoring.md`。
2. 補 `values-2x2.yaml` 草稿與 `docs/cluster.md` 第二台機器 join 流程。
3. 清楚標註 PPO 不是目前 live 主線，避免口試時被問「到底用 PPO 還是 DSAC」。

### 接下來 2-3 週

1. 建 2×2 實驗環境。
2. 跑 evaluation matrix：score baseline vs DSAC live/shadow。
3. 做 score-residual DSAC prototype。
4. 補 runtime predictor CI。

### 論文 / 報告主軸

建議主軸改成：

```text
A safe and observable ML-assisted resource allocation platform for Slurm-on-Kubernetes
```

三個主貢獻：

1. **MPS-aware scheduling baseline + DSAC live extension**：不是只賣 DRL，而是賣 safe ML-assisted scheduling。
2. **Sim-to-live reproducibility**：sim、training、checkpoint、live service、score parity、CI 全管線。
3. **End-to-end observability**：OTel trace bridge + Grafana dashboard 能解釋 job queue / GPU allocation / MPS slot / CPU resource。

DRL 結果如果超 baseline，就是加分；如果沒有，也能作為 honest negative result，說明在小 cluster 上 heuristic 仍強，未來需 2×2 / residual RL / richer workloads。

---

## 7. 最終評估

| 面向 | 評分 | 說明 |
|---|:---:|---|
| 工程完整度 | 4/5 | Helm、CI、docs、metrics 都已成熟 |
| Live 安全性 | 4/5 | silent fallback + abstain + chaos script；仍缺實機 chaos 數據 |
| DRL 研究完成度 | 2.5/5 | DSAC 能 live，但尚未證明效能優勢 |
| 可觀測性 | 5/5 | 這是目前最強項 |
| 生產化韌性 | 3/5 | 單副本與 leader election 是主要缺口 |
| 論文敘事一致性 | 3/5 | 若從「DRL scheduler」改成「ML-assisted observable platform」會更穩 |

**結論：** 已完成的改善應從 backlog 刪掉；目前真正該投資的是 2×2 cluster、DRL/score baseline evaluation、chaos 實測數據，以及 operator/service 韌性。不要再把時間花在已完成的測試與 dashboard 上；接下來要產出的是能支撐研究結論的數據。
