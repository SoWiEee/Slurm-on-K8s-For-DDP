# Development Notes (Phase 1)

## 目標

完成 Timeline 的 Phase 1：

1. 建立 Slurm Controller / Worker 映像。
2. 在 Kind 部署靜態 Slurm 叢集。
3. 讓 Pod 間具備 SSH 互通與 Munge 認證。

---

## 開發想法

## 1) 先穩定，再擴展

Phase 1 先不做 Operator 與自動擴縮，先把最小可用系統（MVP）做穩：

- 固定 1 個 Controller + 2 個 Worker。
- 以 StatefulSet 保障 Pod 命名穩定（例如 `slurm-worker-0`）。
- `slurm.conf` 先採固定節點名稱，避免動態註冊提升除錯成本。

## 2) 設定集中管理

- Slurm 設定放在 `ConfigMap`（`phase1/manifests/slurm-static.yaml`）。
- SSH 與 Munge 金鑰放在 `Secret`，並以腳本產生，避免把敏感資料提交到 Git。
- 啟動流程放在容器 `entrypoint.sh`，讓行為可讀、可追蹤。

## 3) 針對 timeout 問題的修正策略

根據使用者回報（StatefulSet rollout timeout + Pod Error），Phase 1 追加了以下強化：

- `entrypoint.sh` 先建立並修正 munge 所需目錄權限（`/run/munge`、`/var/lib/munge`、`/var/log/munge`）。
- 啟動 `munged` 後主動檢查程序是否存在，不再「失敗但繼續跑」。
- `SlurmctldHost` 改為 `主機名(FQDN)` 形式，避免 controller 身分比對與 worker DNS 解析衝突。
- StatefulSet 增加 readiness/liveness probe，讓 rollout 判斷更準確。
- bootstrap 失敗時自動收集 `get pods` / `describe` / `logs`，減少手動排查時間。

---

## 除錯方式

## A) Pod 卡在 CrashLoopBackOff 或 Error

1. 看 Pod 事件：

```bash
kubectl -n slurm describe pod <pod-name>
```

2. 看容器日誌：

```bash
kubectl -n slurm logs <pod-name>
```

常見原因：
- `munge.key` 沒掛載成功。
- `munge.key` 權限不正確（必須 `0400` 且 owner `munge`）。
- `munged` 需要的目錄權限不正確。

## B) `sinfo` 看不到 worker

1. 到 controller 內查節點：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- scontrol show nodes
```

2. 到 worker 看 slurmd log：

```bash
kubectl -n slurm logs pod/slurm-worker-0
```

常見原因：
- `SlurmctldHost` 寫錯。
- DNS service 名稱與 StatefulSet serviceName 不一致。

## C) SSH 不通

1. 從 controller 手動 ssh：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- \
  bash -lc 'ssh -o StrictHostKeyChecking=no slurm-worker-0.slurm-worker hostname'
```

2. 若失敗，檢查：
- `id_ed25519` / `id_ed25519.pub` 是否掛載。
- `authorized_keys` 是否在 entrypoint 中正確生成。
- `sshd` 是否有正常啟動。

## D) Munge 驗證

可在 controller 產生 token 再在 worker 解碼（進階）：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- munge -n
kubectl -n slurm exec pod/slurm-worker-0 -- unmunge
```

若解碼失敗，通常是 `munge.key` 不一致或權限不正確。

---


## E) 本次真實 root cause（依錯誤訊息）

你提供的訊息其實有兩個獨立問題：

1. `Exit Code: 127` + `/usr/bin/env: 'bash\r': No such file or directory`
   - 代表 entrypoint 以 CRLF 換行被複製進 image，容器內解析 shebang 失敗。
   - 解法：用 `.gitattributes` 強制 LF，並建議重置 working tree 後重建 image。

2. `error mounting ... /etc/slurm/slurm.conf ... no such file or directory`
   - 來自 `subPath` 掛單一檔案在某些環境容易碰到初始化邊緣錯誤。
   - 解法：把 ConfigMap 整個掛到 `/etc/slurm`，避免 subPath mount path 問題。


3. `No resources found in slurm namespace` + `namespaces "slurm" not found`
   - 常見於 kubectl context 指到錯誤叢集（不是 `kind-slurm-lab`）。
   - 解法：bootstrap / verify 都顯式切換到 `KUBE_CONTEXT`（預設 `kind-slurm-lab`），並在失敗輸出 current-context。


4. `chmod: changing permissions of '/etc/munge/munge.key': Read-only file system`
   - Kubernetes Secret volume 是唯讀，直接改 mount 檔案權限會失敗，導致 entrypoint 結束（Exit 1）。
   - 解法：Secret 改掛到 `/slurm-secrets/munge.key`，啟動時複製到 `/etc/munge/munge.key` 後再 `chown/chmod`。
   - 同時移除 munge/ssh 的 `subPath` 檔案掛載，改為目錄掛載降低 runtime 邊緣錯誤。
   - `bootstrap` 新增 `FORCE_RECREATE=true` 可刪除舊 StatefulSet/Pod，避免沿用舊 revision。


5. `munged: Error: PRNG seed dir is insecure: invalid ownership of "/var/lib/munge"` 或 `Socket is inaccessible: execute permissions for all required on "/run/munge"`
   - `munged` 會檢查安全權限；只要目錄 owner/mode 不符合就會直接退出。
   - 解法：entrypoint 顯式修正 `/etc/munge`、`/var/lib/munge`、`/var/log/munge` 為 `munge:munge` + `0700`（含遞迴）。
   - `/run/munge` 必須給 `0711`，否則會出現 socket path execute 權限錯誤。
   - 並改用 `munge` 使用者啟動 `munged`。

6. `This host (...) not a valid controller` + `Unable to resolve "slurm-controller-0.slurm-controller"`
   - `SlurmctldHost` 若不是本機 hostname（controller pod 內多半是 `slurm-controller-0`）會被 slurmctld 拒絕。
   - 同時 worker 解析 controller 建議使用完整 FQDN，避免 namespace 搜尋路徑差異。
   - 解法：`SlurmctldHost=slurm-controller-0(slurm-controller-0.slurm-controller.slurm.svc.cluster.local)`，讓 controller 主機名比對與 worker DNS 解析同時成立。

7. `Reason=NO NETWORK ADDRESS FOUND`（節點可用但顯示原因）
   - 在 K8s 環境中，Slurm 有時無法從 `NodeName` 自動推導穩定位址。
   - 解法：在每個 `NodeName` 額外指定 `NodeAddr`（FQDN）與 `NodeHostname`（短名），減少位址判定歧義。

## 後續銜接（Phase 2 前）

- 把 worker 改成 Deployment + 動態 replicas。
- 將節點數量與 partition 設定改為可程式化更新。
- 導入 Operator（Kopf）觀察 Pending jobs 並觸發 scale up/down。

# Development Notes (Phase 2)

## 目標

完成 Timeline 的 Phase 2：

1. 開發 Python Operator，實作 `Pending Job -> Scale Up`。
2. 實作 `Idle Node -> Scale Down`。

---

## 開發想法

## 1) 階梯式開發（先可用，再精緻）

Phase 2 先走「低耦合、可觀測」路線：

- 先用單一 Python 控制迴圈（polling）完成 MVP。
- 透過 `kubectl exec` 讀取 controller 內的 Slurm 狀態（不先引入過多 framework）。
- 只 patch 一個目標（`slurm-worker` StatefulSet replicas），避免一次改太多面向。

這樣可以快速驗證核心路徑：

`Pending Jobs -> replicas +1`，`No Pending + Busy 可縮 -> replicas -1`。

## 2) 可維護性設計

為了後續擴充（例如改成 Kopf / CRD）可平滑銜接，本次程式結構拆成清楚函式：

- `get_pending_jobs`：只負責 queue 計數。
- `get_busy_nodes`：只負責節點忙碌狀態計算。
- `desired_replicas`：只負責擴縮決策。
- `patch_replicas`：只負責寫回 K8s。

搭配 `Config` dataclass + env vars，讓策略可由 manifest 調整，不需改程式碼。

## 3) 防抖動策略

如果任務剛結束就立即縮容，容易在短工作負載下反覆彈跳。

因此加入：

- `SCALE_DOWN_COOLDOWN_SECONDS`：scale-up 後短時間內先不縮。
- `SCALE_UP_STEP` / `SCALE_DOWN_STEP`：讓擴縮速度可控。
- `MIN_REPLICAS` / `MAX_REPLICAS`：避免超出 slurm.conf 已定義節點範圍。

---

## 除錯方式

## A) Operator 沒有擴容

1. 看 Operator log：

```bash
kubectl -n slurm logs deployment/slurm-elastic-operator -f
```

2. 檢查 RBAC 是否允許 `pods/exec` 與 patch statefulset：

```bash
kubectl -n slurm auth can-i create pods/exec --as=system:serviceaccount:slurm:slurm-elastic-operator
kubectl -n slurm auth can-i patch statefulsets --as=system:serviceaccount:slurm:slurm-elastic-operator
```

3. 手動在 controller 驗證 pending：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- squeue -t PENDING
```

## B) 有擴容但沒有縮容

1. 先確認 cooldown 是否尚未結束（預設 60 秒）。
2. 確認 job 是否真的已清空：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- squeue
```

3. 檢查 busy 節點是否仍 > min_replicas，避免縮到執行中任務。

## C) verify-phase2 失敗

1. 先看 operator log：

```bash
kubectl -n slurm logs deployment/slurm-elastic-operator --tail=200
```

2. 再看 worker replicas 變化：

```bash
kubectl -n slurm get statefulset slurm-worker -w
```

3. 若 job 提交失敗，進 controller 直接測 `sbatch`：

```bash
kubectl -n slurm exec -it pod/slurm-controller-0 -- bash
sbatch --help
```

---

## 後續銜接（Phase 3 前）

- 把目前 polling loop 抽象成策略介面，準備承接 DDP 訓練事件與 checkpoint-aware 決策。
- 加入「每個 partition 的獨立擴縮」而不只單一 worker pool。
- 將操作事件（scale up/down）輸出為結構化日誌，方便量化評估與報告撰寫。

## D) bootstrap-phase2 出現 StatefulSet invalid/forbidden update

錯誤訊息像是：

- `spec.selector: Required value`
- `spec.template.spec.containers: Required value`
- `updates to statefulset spec ... are forbidden`

這通常是因為用 `kubectl apply` 套了一份「只有 replicas」的 StatefulSet YAML。
StatefulSet 不是 Strategic Merge Patch；apply 會把它當成完整目標物件做驗證與更新，
因此會命中 selector/template 必填與 immutable 欄位限制。

修正策略：

1. 將 Phase 2 manifest 移除 partial StatefulSet 定義。
2. 改由 `bootstrap-phase2.sh` 對既有 `slurm-worker` 執行：

```bash
kubectl -n slurm scale statefulset/slurm-worker --replicas=1
```

這樣只更新 replicas，不會觸發不允許的 spec 欄位更新。

## Phase 3 前的設計規劃（先記錄想法）

以下是我對你列的三個方向的實作策略，目標是：

- 盡量不破壞現有可運作的 Phase 2（可階梯式落地）
- 每一步都可獨立驗證
- 保持程式可讀、可維護、可觀測

---

### 1) 把 polling loop 抽象成策略介面（承接 DDP / checkpoint-aware 決策）

#### 現況

目前 `main.py` 是單一 loop：

1. 讀 pending jobs
2. 讀 busy nodes
3. 算 desired replicas
4. patch replicas

這在 Phase 2 可用，但若要加上「DDP checkpoint-aware」判斷，決策會快速膨脹。

#### 拆分目標

把邏輯拆成三層：

1. **State Collector（資料蒐集層）**
   - 專職蒐集狀態，不做決策。
   - 例：`pending_jobs`, `running_jobs`, `busy_nodes`, `idle_nodes`, `checkpoint_age_seconds`。

2. **Policy（決策策略層）**
   - 輸入目前 state + config，輸出 `ScalingDecision`。
   - 可插拔：
     - `BasicQueuePolicy`（現有 Phase 2 行為）
     - `CheckpointAwarePolicy`（新增 checkpoint 保護邏輯）

3. **Actuator（執行層）**
   - 僅負責 patch/scale 與重試。
   - 不知道為何縮放，只執行 `target_replicas`。

#### 先期可行的 checkpoint-aware 規則（MVP）

先不碰應用程式內部細節，採「保守保護」：

- 若偵測目前有 DDP job 且距離最近 checkpoint 太久（或狀態未知），禁止 scale-down。
- 若 checkpoint 在安全窗口內，才允許 scale-down。

這可避免「即將可保存時被縮掉」導致訓練回復成本過高。

#### 介面草案（概念）

- `ClusterState`（dataclass）
- `ScalingDecision`（target replicas + reason + policy name）
- `Policy.evaluate(state, cfg) -> ScalingDecision`

這樣後續就能平滑接上 Phase 3 應用事件，不需重寫 control loop。

---

### 2) 加入「每個 partition 的獨立擴縮」

#### 現況限制

現在只有單一 `slurm-worker` pool + 單一 `debug` partition；target replicas 是全域值。

#### 目標架構

改成「Partition 為單位」管理：

- 每個 partition 對應一個 worker StatefulSet（或 Deployment）。
- Operator 讀 partition 維度的 queue 壓力，分別計算 target replicas。

#### 資料模型（建議）

可先用一份靜態 mapping（env/json）開始：

- `partition_name`
- `worker_statefulset`
- `min/max replicas`
- `scale_up/down step`
- `cooldown`

例：

- `debug -> slurm-worker-debug`
- `gpu -> slurm-worker-gpu`

#### 漸進落地順序

1. 先保留單 partition，但把程式改成「列表迴圈」（即便列表只有一個）。
2. 再新增第二個 partition 做 smoke test。
3. 最後才把 Slurm 設定與 node naming 進一步自動化。

這樣可以把風險切小，避免一次改太多造成不可逆故障。

---

### 3) 結構化日誌（支援量化評估與報告）

#### 目標

每次迴圈和每次縮放都留下 machine-readable 記錄，方便後續算 KPI：

- 反應時間（job pending 到 scale-up 的秒數）
- 資源利用率近似（busy/total）
- scale actions 次數與抖動頻率

#### 實作方式

1. 日誌統一 JSON line（一行一事件）。
2. 固定欄位：
   - `ts`, `level`, `event_type`
   - `policy`, `partition`, `current_replicas`, `target_replicas`
   - `pending_jobs`, `busy_nodes`, `cooldown_remaining`
   - `decision_reason`
3. 事件分類：
   - `loop_observation`
   - `scale_action`
   - `scale_skipped`
   - `error`

#### 報告銜接

後續可直接用 `jq/python` 聚合：

- 平均 scale-up latency
- P95 scale-down latency
- 單位時間縮放次數（看是否抖動）

這會讓 Phase 4 的「評估與優化」有可重現數據，而不只文字描述。

---

## 建議的實作里程碑（小步快跑）

### Milestone A（低風險）

- 抽出 `ClusterState` / `ScalingDecision` / `Policy`。
- 保持行為與現況一致（等價重構）。

### Milestone B（可觀測性）

- 全面改為 JSON structured logs。
- 提供簡單 log parser 腳本輸出 baseline 指標。

### Milestone C（partition-aware）

- 改為 partition 清單迴圈。
- 先在 `debug` + `debug2`（模擬）驗證雙池獨立擴縮。

### Milestone D（checkpoint-aware）

- 先加入保守規則（checkpoint 狀態未知時不縮）。
- 再逐步與 DDP 訓練 wrapper 的 checkpoint metadata 對接。

---

## 風險與避險

1. **策略誤判造成縮容過頭**
   - 避險：加入 per-partition `min_replicas` 與 cooldown，並對關鍵任務加 scale-down guard。

2. **partition mapping 與 slurm.conf 不一致**
   - 避險：啟動時做 config validation，對不存在 partition/statefulset 直接告警並跳過。

3. **log 太多造成噪音**
   - 避險：區分 observation/action log level，並支援抽樣或週期性摘要。

---

## 我建議你下一步

如果你同意，我下一個 PR 可以先做 **Milestone A + B**（等價重構 + 結構化日誌）：

- 幾乎不改功能行為，風險最低。
- 但會把架構打好，Phase 3/4 都會明顯更好推進。

## Milestone A + B 實作落地（已完成）

本次已先完成低風險重構與可觀測性強化：

1. **等價重構（Milestone A）**
   - 將 operator 程式拆為 `ClusterStateCollector`、`BasicQueuePolicy`、`StatefulSetActuator`。
   - 以 `ClusterState` / `ScalingDecision` dataclass 作為層間資料契約。
   - 保持與 Phase 2 既有規則等價（pending 觸發 scale-up、無 pending + busy floor + cooldown 控制 scale-down）。

2. **結構化日誌（Milestone B）**
   - 新增 JSON line logger（`JsonLogger`）。
   - 事件型別：`startup`、`loop_observation`、`scale_action`、`scale_skipped`、`error`。
   - 欄位包含 policy/state/decision/cooldown，方便後續用 `jq` 或 Python 做 KPI 聚合。

後續可直接在此基礎上往 Milestone C（partition-aware）與 Milestone D（checkpoint-aware）前進。

## Milestone C + D 實作落地（已完成）

本次完成兩個方向：

### C) Partition-aware（每個 partition 獨立擴縮）

- 新增 `PartitionConfig` 與 `PartitionState`，讓每個 partition 各自計算 target replicas。
- 透過 `PARTITIONS_JSON` 支援多 partition 設定；若未提供則 fallback 到單 partition（`SLURM_PARTITION` + `WORKER_STATEFULSET`）。
- control loop 以 partition 為迴圈單位，scale action 會指向對應 worker StatefulSet。

### D) Checkpoint-aware（checkpoint 保護式縮容）

- 新增 `running_jobs` 與 checkpoint age 判斷。
- 當「有 running job 且準備 scale-down」時：
  - checkpoint 狀態未知（檔案不存在/未設定）=> 阻擋縮容。
  - checkpoint age 超過門檻（`MAX_CHECKPOINT_AGE_SECONDS`）=> 阻擋縮容。
- 只有 checkpoint 在安全窗口時才允許縮容，降低 DDP 任務恢復成本。

### 觀測面

- 保持 Milestone B 的結構化日誌，並補充 partition/checkpoint 相關欄位，方便後續比較不同 partition 的行為與 checkpoint 保護命中率。

# Development Notes (Phase 3)

## 目標

完成 Timeline 的 Phase 3（第一版，採階梯式開發）：

1. 先打通共享儲存（/shared）。
2. 再驗證跨節點（MPI-like）工作負載。
3. 最後導入 PyTorch/checkpoint 工作流（含 fallback 路徑）。

---

## 開發想法

## 1) 階梯式（Storage -> MPI -> Training）

Phase 3 最大風險是一次引入太多變因（PVC、MPI、PyTorch 套件、DDP 啟動參數）。
因此拆成三層：

- **Layer A: Shared Storage**
  - 只驗證 `/shared` 讀寫是否可行。
  - 若 A 不穩，B/C 一定不穩。

- **Layer B: MPI-like smoke**
  - 先不綁特定 MPI 發行版，直接用 Slurm `srun --nodes=2 --ntasks=2` 驗證跨節點。
  - 目標是「先確認調度與跨節點執行路徑」。

- **Layer C: PyTorch/checkpoint**
  - 若 image 內有 torch：走 `python -m torch.distributed.run`。
  - 若尚未安裝 torch：fallback 到純 Python checkpoint demo。
  - 這樣可在不阻塞主流程的前提下，先驗證 checkpoint 資料流與檔案一致性。

## 2) 可維護性設計

- 用 `phase3/manifests/slurm-phase3-shared.yaml` 集中管理「共享儲存 + sbatch 模板」。
- 用 `bootstrap-phase3.sh` 負責 infra 變更（PVC + StatefulSet 掛載 patch）。
- 用 `verify-phase3.sh` 負責工作流驗證（三個 job 依序執行）。

這種拆分讓未來要升級 PyTorch image、改 checkpoint 規則、加入 chaos test 時，不會和基礎 infra 攪在一起。

## 3) 為何用 patch 掛載 `/shared`

避免直接重寫 Phase 1 manifest，降低對既有可運作路徑的破壞：

- `bootstrap-phase3.sh` 會先檢查 volume/volumeMount 是否存在。
- 不存在才 patch（具 idempotent 行為）。
- patch 後等待 rollout，失敗時可直接從 StatefulSet event 排查。

---

## 除錯方式

## A) PVC 一直 Pending

先看 StorageClass：

```bash
kubectl get storageclass
```

再看 PVC 事件：

```bash
kubectl -n slurm describe pvc slurm-shared
```

若 `local-path` 不存在，表示 kind/local-path provisioner 未正常啟用。

## B) `/shared` 在 Pod 內看不到

1. 看 StatefulSet spec 是否已包含 `shared-storage`：

```bash
kubectl -n slurm get statefulset slurm-worker -o yaml
kubectl -n slurm get statefulset slurm-controller -o yaml
```

2. 看 rollout 是否完成：

```bash
kubectl -n slurm rollout status statefulset/slurm-worker
kubectl -n slurm rollout status statefulset/slurm-controller
```

3. 進 Pod 驗證：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- ls -al /shared
```

## C) MPI-like job 卡 Pending

1. 檢查是否有足夠 worker：

```bash
kubectl -n slurm get statefulset slurm-worker
kubectl -n slurm exec pod/slurm-controller-0 -- squeue
```

2. 檢查 partition 與節點狀態：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- sinfo -R
kubectl -n slurm exec pod/slurm-controller-0 -- scontrol show nodes
```

## D) PyTorch job 失敗

1. 先判斷是 torch 缺失還是分散式參數問題：

```bash
kubectl -n slurm exec pod/slurm-worker-0 -- python3 -c 'import torch'
```

2. 若無 torch，先接受 fallback 路徑，確認 checkpoint 檔案仍有落盤：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- ls -al /shared/phase3
```

3. 若有 torch 但失敗，再查看 job 輸出與 controller log：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- sacct -j <job_id> --format=JobID,State,ExitCode
kubectl -n slurm logs statefulset/slurm-controller --tail=200
```

---

## 下一步（Phase 3.1）

- 加入 chaos 測試腳本：訓練中隨機刪除 worker Pod。
- 與 Phase 2 checkpoint guard 事件對齊，記錄「阻擋縮容」與「恢復成功」指標。
- 將 phase3 verify 輸出整理成可直接放報告的 KPI 表格。


## 本次修正（第二輪）

你回報的兩個實際問題已納入修正：

1. `rg: command not found`
   - root cause：`bootstrap-phase3.sh` / `verify-phase3.sh` 依賴 ripgrep。
   - 修正：改用系統預設可用的 `grep`，移除 Phase 3 腳本對 `rg` 的執行時依賴。

2. `PVC wait timeout` 後腳本仍繼續 patch
   - root cause：原本 `kubectl wait pvc ... || true` 會吞掉錯誤。
   - 修正：改成 timeout 直接 fail-fast，並自動輸出 `kubectl get/describe pvc slurm-shared`，避免後續 rollout 產生二次錯誤訊號。

這次調整重點是「錯誤可觀測且可立即中止」，讓問題定位更直覺。


## 本次修正（第三輪：StorageClass 相容性）

你最新回報的核心錯誤是：

- `storageclass.storage.k8s.io "local-path" not found`

### root cause

Phase 3 先前把 PVC `storageClassName` 寫死成 `local-path`，但你的叢集目前沒有這個 StorageClass，導致 `slurm-shared` 永遠 Pending。

### 修正

1. `bootstrap-phase3.sh` 改為自動選擇 StorageClass：
   - 優先 `PHASE3_STORAGE_CLASS`（若有指定）
   - 否則 `local-path`（若存在）
   - 否則 default StorageClass
   - 否則第一個可用 StorageClass

2. 當現有 `slurm-shared` 是 Pending 且 StorageClass 不符時，自動刪除並重建 PVC。

3. 若現有 `slurm-shared` 已 Bound 且 StorageClass 不符，為避免資料風險，腳本會停止並提示手動處理。

4. `phase3/manifests/slurm-phase3-shared.yaml` 改為僅管理 job ConfigMap；PVC 由 bootstrap 動態建立，避免不同叢集環境下被硬編值卡住。


### 補充：你這次看到 `WaitForFirstConsumer` 的真正意義

你貼的事件：

- `Normal  WaitForFirstConsumer ... waiting for first consumer to be created before binding`

這不是 provisioner 壞掉，而是 `standard` StorageClass 設定為 `volumeBindingMode=WaitForFirstConsumer` 的預期行為。

舊版 bootstrap 在「patch 掛載之前」就先等 PVC Bound，會形成等待順序錯誤：

1. 先等 Bound（但 class 要等 consumer）
2. consumer 又要等後續 patch 才會出現

=> timeout。

本次把流程改成：

1. 建立 PVC
2. 若是 `WaitForFirstConsumer`，先不 pre-wait
3. 先 patch controller/worker 掛載 `/shared`
4. 再做 post-mount wait PVC Bound

這樣才能符合 WFFC 的生命週期。


## 本次修正（第四輪：verify 對 Slurm 短暫 timeout 的韌性）

你這次回報 `verify-phase3.sh` 的錯誤重點：

- `slurm_load_jobs error: Socket timed out on send/recv operation`
- `sbatch: error: Batch job submission failed: Socket timed out on send/recv operation`

### root cause（觀察）

Phase 3 bootstrap 後，controller/worker 正在剛完成 rollout 或剛發生節點調度變動時，
Slurm CLI（`squeue`/`sbatch`）偶發短暫 timeout。這通常是瞬時控制面可用性抖動，不一定代表永久故障。

此外，`sacct` 在未啟用 accounting storage 的環境會失敗，若腳本把它當硬失敗，會干擾真正驗證結果。

### 修正

1. `verify-phase3.sh` 新增 `slurm_exec_retry()`：
   - 對 `Socket timed out` / `Unable to contact slurm controller` 等可重試錯誤做重試。
   - 預設重試 `8` 次、間隔 `5` 秒（可由 env 覆寫）。

2. `submit_job`、`wait_job_done`、`sinfo` preflight 都改用 retry 路徑。

3. job 狀態輸出改成：
   - 預設使用 `scontrol show job`（不依賴 accounting）
   - 若需要 `sacct`，可用 `ENABLE_SACCT_STATUS=true` 額外輸出

這樣可避免把「短暫 timeout」或「sacct 不可用」誤判成 Phase 3 功能失敗。


### 補強：避免 verify 對 `CG` 狀態過早 timeout

你提供的 `squeue` 顯示：

- `ST=CG`（Completing）

這代表 job 已進入收尾階段，不應與一般 Pending/Running 用同一個 hard timeout 直接判失敗。

因此 `wait_job_done` 補上：

- `JOB_TIMEOUT_SECONDS`：一般等待上限
- `JOB_COMPLETING_GRACE_SECONDS`：若進入 `COMPLETING`，再給額外緩衝

同時把 job 結果輸出改為預設 `scontrol show job`，避免 accounting 未啟用時的 `sacct` 噪音影響判讀。
