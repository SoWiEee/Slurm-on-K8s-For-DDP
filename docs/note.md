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

完成 Timeline 的 Phase 3（先建立可維護、可階梯式擴充的驗證框架）：

1. 建立共享儲存（PVC）並掛載到 controller/worker。
2. 完成三層驗證：基礎互通、資料一致性、訓練語義。
3. 讓後續 PyTorch DDP 可以直接替換 Layer 3，不需重寫整套驗證流程。

---

## 開發想法

## 1) 先把風險拆層，避免一次把問題混在一起

Phase 3 最容易踩坑的是：

- Slurm 排程問題
- K8s 儲存問題
- 應用程式 checkpoint 問題

若三者同時改，故障定位會非常慢。因此本次採「由低風險到高風險」三層：

- Layer 1：只驗證 worker 間協調前提（排程/互通）。
- Layer 2：只驗證共享資料一致性。
- Layer 3：再驗證 checkpoint/resume 的語義連續性。

## 2) 先用 PVC（local-path）建立可重現最小路徑

在 Kind 單機開發環境中，PVC（local-path）部署最簡單、維護成本低。

- 優點：不用先架 NFS server 就可做共享流程驗證。
- 限制：不是完整 RWX/NFS 生產拓撲，但足夠作為 Phase 3 的 MVP。

## 3) Layer 3 先用 mock-train，保留替換點

這版刻意先不用 PyTorch 真實訓練，理由是先確認「機制」：

- checkpoint 檔案是否能跨 worker 延續。
- 重新執行後 step/optimizer/loss 是否連續。

等這個機制穩定後，再把 `mock-train.sh` 替換成 `torch.save/torch.load` 的訓練腳本，就能低風險升級到真實 DDP。

---

## 除錯方式

## A) `/shared` 在 Pod 內不存在

1. 看 PVC 是否 Bound：

```bash
kubectl -n slurm get pvc slurm-shared-pvc
```

2. 看 StatefulSet 是否掛載成功：

```bash
kubectl -n slurm get sts slurm-controller -o yaml
kubectl -n slurm get sts slurm-worker -o yaml
```

3. 進 Pod 驗證：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- ls -al /shared
kubectl -n slurm exec pod/slurm-worker-0 -- ls -al /shared
```

## B) Layer 1 `srun -N2 -n2 hostname` 無法回傳兩個 host

1. 看 node 是否可見：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- sinfo
kubectl -n slurm exec pod/slurm-controller-0 -- scontrol show nodes
```

2. 若 worker 不足 2 個，先手動拉高 replicas：

```bash
kubectl -n slurm scale statefulset/slurm-worker --replicas=2
kubectl -n slurm rollout status statefulset/slurm-worker --timeout=180s
```

## C) Layer 2 checksum/mtime 驗證失敗

1. 先看每個 pod 實際檔案：

```bash
kubectl -n slurm exec pod/slurm-worker-0 -- ls -al /shared/checkpoints
kubectl -n slurm exec pod/slurm-worker-1 -- ls -al /shared/checkpoints
```

2. 比對 checksum：

```bash
kubectl -n slurm exec pod/slurm-worker-0 -- sha256sum /shared/checkpoints/phase3_meta.txt
kubectl -n slurm exec pod/slurm-worker-1 -- sha256sum /shared/checkpoints/phase3_meta.txt
```

3. 若 mtime 沒增加，檢查寫入命令是否真的 append 成功。

## D) Layer 3 continuity 失敗

1. 直接看 log：

```bash
kubectl -n slurm exec pod/slurm-worker-1 -- cat /shared/checkpoints/mock_train.log
```

2. 常見原因：
- checkpoint 寫入不是 atomic（先寫 temp 再 mv 可降低毀損）。
- resume 讀取邏輯沒先 source state。
- 不同 worker 使用到不同路徑（需固定 `/shared/checkpoints/*`）。

---

## E) bootstrap-phase3 卡在 PVC Bound timeout

症狀：

```bash
persistentvolumeclaim/slurm-shared-pvc unchanged
error: timed out waiting for the condition on persistentvolumeclaims/slurm-shared-pvc
```

常見 root cause：

- StorageClass 使用 `WaitForFirstConsumer`（例如 Kind 常見 local-path 行為）。
- 這種模式下，PVC 不會在 `kubectl apply` 後立刻變 Bound，而是要等 Pod 真正消費 volume 才會綁定。

修正策略：

1. `bootstrap-phase3.sh` 先 patch StatefulSet + rollout。
2. rollout 後再檢查 PVC Bound。
3. 對 controller/worker 增加 `wait Ready` + `/shared` 可寫入檢查（`touch/rm`）+ retry，避免瞬間抖動導致假失敗。
4. 若失敗，自動 dump `pvc/pv/storageclass/sts/pods/events`，並額外輸出兩個 Pod 內 `/shared` 與 mount 訊息。

---

## F) verify-phase3 Layer 1 出現 `Unable to resolve slurm-worker-2...`

症狀（節錄）：

```bash
srun: error: slurm_set_addr: Unable to resolve "slurm-worker-2.slurm-worker.slurm.svc.cluster.local"
[L1] expected 2 hosts from srun, got 1
```

常見 root cause：

- `slurm.conf` 預先定義了 `slurm-worker-[0-2]`。
- 但 Phase 3 驗證當下 worker replicas 只有 2，`worker-2` Pod 不存在。
- 若 `srun` 未鎖定 node list，可能挑到不存在/未就緒節點，導致 getaddrinfo 失敗。

修正策略：

1. Layer 1 先確保 `slurm-worker-0/1` Ready。
2. 在 controller 內先做兩個 FQDN 的 `getent hosts` 檢查。
3. `srun` 改為 `--nodelist=slurm-worker-0,slurm-worker-1`。
4. 加入 `scontrol update ... state=resume` 與 retry，降低暫時性 `NOT_RESPONDING` 影響。

---

## G) verify-phase3 timeout 時自動 dump 資訊

現在 `verify-phase3.sh` 已加上 `trap on_error`，只要任一步驟失敗（例如 `kubectl wait ... timeout`），會自動輸出：

- `pods` / `statefulsets` / `endpoints`
- `events`（最近 120 筆）
- controller 內 `sinfo` + `scontrol show nodes`
- controller 對 `worker-0/1/2` 的 `getent hosts`
- `worker-0/1/2` 的 `describe` 與 logs tail

這樣在「當下失敗現場」就能看到：

1. Pod readiness 是否達標。
2. Slurm node 是否 `NOT_RESPONDING` / `COMPLETING`。
3. DNS 解析是否一致。
4. worker 端是否有 slurmd/ssh/munge 啟動異常。

---

## H) verify-phase3 等待 `slurm-worker-1` timeout（operator 競態）

症狀（節錄）：

```bash
error: timed out waiting for the condition on pods/slurm-worker-1
```

搭配 diagnostics 可看到：

- `statefulset/slurm-worker` 最後常回到 `READY 1/1`。
- 只有 `slurm-worker-0` 存在。
- 但驗證腳本正在等 `slurm-worker-1` Ready。

常見 root cause：

- Phase 2 的 `slurm-elastic-operator` 在無 pending job 時會主動 scale-down worker。
- verify 腳本把 worker 拉到 2 的同時，operator 可能又縮回 1，造成等待 timeout。

修正策略：

1. verify 開始時暫時把 operator scale 到 0（預設開啟）。
2. verify 結束（成功或失敗）都自動還原 operator replicas。
3. 保留 `DISABLE_OPERATOR_DURING_VERIFY=false` 作為除錯開關（若要觀察 operator 互動）。

補強（本次）：

- 新增 `wait_worker_replicas_ready` 守門邏輯：在等待期間持續檢查 `statefulset/slurm-worker` 的 `spec.replicas` 與 `readyReplicas`。
- 若發現 `spec.replicas` 被外部改動（例如被其他控制器改回 1），會自動 re-scale 回目標值再繼續等。
- diagnostics 追加 `kubectl logs --previous`（worker-1/2），提高對反覆重啟場景的可見性。
- 移除錯誤路徑中的重複 restore 呼叫，避免出現雙重 `restoring operator replicas ...` 訊息。

---

## 下一步（銜接 PyTorch Training）

1. 新增訓練 image（含 Python + PyTorch + torch.distributed）。
2. 用 `torch.save/torch.load` 取代 `mock-train.sh` state file。
3. 增加 chaos case：訓練中刪除一個 worker pod，驗證 Slurm 重排程後可 resume。
4. 在驗證中納入 loss 曲線連續性門檻（例如斷點前後 loss 不應異常跳升）。
