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

# Development Notes (Phase 3 - Shared Storage Milestone)

## 目標

完成 Timeline 的 Phase 3（目前 milestone）：

1. 在 Kind 單機環境部署 NFS Server（WSL/VM）並整合 `nfs-subdir-external-provisioner`。
2. 建立 StorageClass + RWX PVC，作為 Slurm shared home/checkpoints。
3. 將 Controller / Worker / Login Pod 掛載共享 NFS Volume。

---

## 開發想法

## 1) 降低新手門檻：把「主機層 NFS」與「叢集層 Provisioner」拆開

Phase 3 的痛點是跨層：

- NFS Server 要在 WSL/VM（主機層）先可用。
- K8s 只負責透過 provisioner 動態建立 PV/PVC（叢集層）。

因此拆成兩支腳本：

- `phase3/scripts/setup-nfs-server.sh`：在 WSL/VM 一次性安裝/匯出 NFS。
- `phase3/scripts/bootstrap-phase3.sh`：在 kind 叢集內部署 provisioner + storage + 掛載。

這樣責任清楚，維護時不會把 host provisioning 和 cluster deployment 混在一起。

## 2) 以「patch 既有 StatefulSet」方式整合，避免破壞 Phase 1/2

`slurm-controller` / `slurm-worker` 已由 Phase 1/2 建立並可能被調整 replicas。

若 Phase 3 直接重新 apply 全量 StatefulSet 清單，容易：

- 把 replicas 覆蓋回舊值。
- 觸發不必要欄位漂移。

因此採用 `kubectl patch` 只追加：

- `volumes.shared-storage`
- `volumeMounts[/shared]`

讓變更範圍最小化，也更容易 rollback。

## 3) 先交付可驗證的共享儲存 MVP

先完成共享卷生命線：

- `StorageClass: slurm-shared-nfs`
- `PVC: slurm-shared-rwx`（RWX）
- Controller / Worker / Login 全掛載 `/shared`

並用 `verify-phase3.sh` 做「跨 Pod 寫讀」驗證，確定 checkpoint/home 的基本前提成立。

---

## 除錯方式

## Timeout 分析（你回報的 rollout 卡住）

症狀：`nfs-subdir-external-provisioner` rollout 一直停在 `0 of 1 updated replicas are available`。

最常見是 provisioner pod 啟動後，無法成功 mount 外部 NFS，導致容器無法 Ready。

高機率原因：

- `NFS_SERVER` 雖然在主機可 ping，但 Kind node/container 到該 IP 的 `2049/tcp` 被防火牆或路由擋住。
- NFS export CIDR 未包含 Kind 節點網段（WSL/Windows 環境常見）。
- `NFS_PATH` 不存在或未在 `/etc/exports` 設定。
- `/etc/exports` 已有同路徑舊規則，仍套到舊 CIDR（這次案例的高機率原因）。

為了縮短排查時間，`bootstrap-phase3.sh` 已加入 ERR trap：失敗時會自動列印 deployment/pod describe、pod logs、events、PVC/PV，若偵測到 `access denied by server while mounting` 會直接給 `/etc/exports` 修正指引。

另外 `setup-nfs-server.sh` 已改為「替換同一路徑既有 export 規則」再 `exportfs -ra`，降低舊設定殘留造成的誤判。

針對「已重跑 setup 仍 access denied」：

- 很可能 server 看到的 client source IP 並非你預期的 Kind 內網段（NAT/橋接差異）。
- 已新增 `NFS_EXPORT_ALLOW_ALL_DEBUG=true` 模式，先用 `*` 驗證路徑與服務，再回收為精準 CIDR。

## A) PVC 一直 Pending

1. 檢查 provisioner 狀態：

```bash
kubectl -n nfs-provisioner get pods
kubectl -n nfs-provisioner logs deployment/nfs-subdir-external-provisioner --tail=200
```

2. 常見 root cause：
- `NFS_SERVER` IP 錯誤（Kind node 無法連線）。
- `NFS_PATH` 在主機不存在或未 export。
- `/etc/exports` CIDR 沒放行 Kind Docker 網段。

## B) Pod 有掛 PVC 但 `/shared` 不可寫

1. 先看 mount：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- mount | grep /shared
```

2. 再用最小寫入測試：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- sh -c 'echo ok > /shared/.rw-test'
kubectl -n slurm exec pod/slurm-worker-0 -- cat /shared/.rw-test
```

3. 常見 root cause：
- NFS export 權限不足（未 `rw` 或 root squash 行為不符預期）。
- 主機路徑權限過嚴（建議先用 0777 快速驗證，再收斂）。

## C) Login Pod 不存在或未 Ready

1. 檢查 deployment：

```bash
kubectl -n slurm get deployment slurm-login
kubectl -n slurm describe deployment slurm-login
```

2. 若 `ImagePullBackOff`，先確認本地有 `slurm-worker:phase1` 且 phase1 已完成。

## D) 一鍵驗證

```bash
bash phase3/scripts/verify-phase3.sh
```

這支腳本會檢查：StorageClass、PVC Bound、三種 Pod 掛載、以及跨 Pod 寫讀一致性。

---

## 下一步（Phase 3 後續）

- 將 PyTorch DDP CPU 測試工作負載導入 `/shared/checkpoints`。
- 建立 checkpoint heartbeat + resume + `--requeue` 範例工作。
- 在 operator 加入 checkpoint-aware scale-down guard 與量測指標。

---

## Slurm-on-K8s（往 Slinky operator 方向）的分層架構筆記

這一段是為了把「目前做法」與「未來想靠近 Slinky operator 的做法」放在同一張心智模型裡，之後新增功能（fair-share/QOS、MPI、DDP、彈性節點、治理）比較不會散掉。

### 一、目前設計（Current）：Slurm in-cluster + 以 StatefulSet 當 Slurm nodes + 自製 elastic operator

核心想法：
- Slurm 還是以傳統模式運作（`slurmctld` + `slurmd`），只是把 controller/login/worker 全部容器化，跑在同一個 K8s namespace。
- Slurm 的「node」對應到 K8s 的 `StatefulSet` Pod（`slurm-worker-{i}`），並用 headless service 提供穩定 DNS。
- 彈性擴縮用一個簡單的 controller（`slurm-elastic-operator`）去觀察 queue 狀態，調整 `slurm-worker` 的 replicas。

K8s 資源清單（以 namespace=slurm 為例）：
- 工作負載（Workloads）
  - `StatefulSet/slurm-controller`：`slurmctld` + `munged`
  - `Deployment/slurm-login`：使用者入口（sbatch/srun/squeue 等）
  - `StatefulSet/slurm-worker`：`slurmd` + `munged`（每個 Pod=一個 Slurm node）
  - `Deployment/slurm-elastic-operator`：依 queue 狀態調整 `slurm-worker` replicas（上限/下限、cooldown、step）
- 網路（Networking）
  - `Service/slurm-controller`（headless）：讓 controller Pod 有穩定 DNS（例：`slurm-controller-0.slurm-controller.slurm.svc.cluster.local`）
  - `Service/slurm-worker`（headless）：讓每個 worker 有穩定 DNS（例：`slurm-worker-2.slurm-worker.slurm.svc.cluster.local`）
  - （可選）`Service/slurm-login`：若你要把 login 暴露給叢集外使用者
- 設定/密鑰（Config & Secrets）
  - `ConfigMap/slurm-config`：`slurm.conf`（NodeName/NodeAddr/Partition 等）
  - `Secret/slurm-munge-key`：`munge.key`（controller/login/worker 必須一致）
  - `Secret/slurm-ssh-key`：若你需要 ssh/workflow（非必要但常見）
- 儲存（Storage）
  - `PVC/PV`（或 hostPath）：提供 `/shared`（讓 login 產生 sbatch、job out/err、worker 執行結果可共享）

ASCII 架構圖（目前）：

```
+------------------------------ Kubernetes Cluster ------------------------------+
|                                                                               |
|  [Namespace: slurm]                                                           |
|                                                                               |
|  +--------------------+        headless svc        +--------------------+     |
|  | slurm-controller-0 |<-------------------------->|  slurm-controller  |     |
|  |  - slurmctld       |                           |  (ClusterIP None)  |     |
|  |  - munged          |                           +--------------------+     |
|  +---------^----------+                                                     
|            | scontrol/squeue/sbatch RPC                                     
|            |                                                                
|  +---------+----------+        shared PVC         +--------------------+     |
|  |  slurm-login (dep) |<------------------------->|   /shared (PVC)    |     |
|  |  - sbatch/srun     |                           +--------------------+     |
|  +---------^----------+                                                     
|            | submits jobs                                                    
|            v                                                                
|  +--------------------+        headless svc        +--------------------+     |
|  | slurm-worker (sts) |<-------------------------->|   slurm-worker     |     |
|  |  - slurm-worker-0  |                           |  (ClusterIP None)  |     |
|  |  - slurm-worker-1  |                           +--------------------+     |
|  |  - slurm-worker-2  |                                                     
|  |  - slurm-worker-3  |                                                     
|  |  each: slurmd+munge|                                                     
|  +--------------------+                                                     
|                                                                               |
|  +---------------------------+                                               |
|  | slurm-elastic-operator    |  watches queue -> scale sts replicas          |
|  +---------------------------+                                               |
|                                                                               |
+-------------------------------------------------------------------------------+
```

已知特性與限制（建議你未來寫到 README/roadmap 的）
- 你的彈性擴縮目前是「K8s replicas」尺度，不是 Slurm 的原生 elastic（例如 cloud burst）那種語意。
- `slurm.conf` 需要預先宣告 `NodeName=slurm-worker-[0-3]` 才能做到 max=4 的動態擴到 4；如果沒宣告，`scontrol update` 會報 `Invalid node name`。
- `NO NETWORK ADDRESS` 往往是 slurmctld 在解析 NodeAddr / NodeHostname 或 DNS 尚未 ready 時就做了 node state 更新，所以你後來加入 DNS gate/重試邏輯是必要的「K8s 化」措施。

### 二、往 Slinky operator 靠近時，你會「多」哪些層？

Slinky 的核心方向不是只有 autoscale，而是把 Slurm cluster 當成一個被 K8s controller 管理的「抽象資源」。
你可以把它想成：把目前散在 YAML + scripts 的決策，移到 operator 裡用 reconciliation loop 做成可重入、可觀測、可治理。

建議分層如下（對應你專案的 future work）：

1) L0 基礎設施層（K8s Infra）
- CNI/DNS/StorageClass、GPU device plugin、NodePool（spot/on-demand）
- 這層的 KPI 是：Pod 啟動穩定性、DNS/網路收斂、PVC 性能、節點供給速度。

2) L1 Slurm 控制面層（Slurm Control Plane）
- `slurmctld` 高可用（optional）、state save location、升級/滾動策略
- `munge` key rotation/一致性保證
- （進階）slurmdbd + accounting（用於 fair-share/報表/計費）

3) L2 Slurm 資源抽象層（Operator CRDs / Desired State）
- 引入 CRD 來描述叢集期望狀態（名稱示例）：
  - `SlurmCluster`：cluster 版本、image、munge/ssh secret reference、shared storage、partition policy
  - `SlurmNodeSet`：worker 族群（cpu/gpu/feature/taint/toleration）、min/max、template
  - `SlurmLoginSet`：login 入口（可能多副本）
- operator 負責把 CRD reconcile 成：StatefulSet/Deployment/Service/ConfigMap/Secret/PVC。

4) L3 彈性與排程治理層（Elasticity + Governance）
- Autoscale 觸發來源不只看「有沒有 pending」，還要能讀：
  - pending reason（資源不足、constraint、QOS、reservation）
  - job 的資源向量（CPU/Mem/GPU/Nodes/Time）
  - 叢集成本策略（上限 4 workers、cooldown、步進）
- Scale-down 要做 drain：
  - 對應 Slurm 的語意是 `DRAIN`/`DOWN`/`RESUME` + 確認無 job step
  - 對應 K8s 的語意是優雅縮 Pod + 避免破壞正在跑的 job

5) L4 使用者介面層（User UX / HPC Features）
- 保持 `sbatch/srun/squeue/sacct` 的工作流（這正是 Slinky 願景的重點）
- 強化 HPC 特性：MPI/多節點啟動一致性（gang-like）、job array、dependency、QOS/fair-share

### 三、Future work（建議照這個順序做，風險最低）

(1) 把「目前動態 worker」從 script 移到 operator 內，並把策略參數化
- 把 `MAX_REPLICAS/MIN_REPLICAS/POLL_INTERVAL/COOLDOWN` 變成 CRD spec 或 ConfigMap。
- 讓 operator 的決策可觀測：events/metrics（例如每次 scale 的原因、pending job 數量）。

(2) 將 Slurm node lifecycle 做成「強一致」的 reconcile
- 加入：DNS gate、`scontrol reconfigure` 重試、`scontrol update State=RESUME/DRAIN` 的狀態機。
- 目標是把 `NO NETWORK ADDRESS` 類問題變成「operator 會自動收斂」而不是靠人工 rerun。

(3) 引入 CRD（最小集）把 cluster spec 固化
- 先做一個最小 `SlurmNodeSet`：只管 worker 的 min/max + pod template。
- 做到後，你的 YAML 就能從「手寫 slurm.conf + sts」變成「宣告式 spec」。

(4) 進階：Accounting/fair-share（slurmdbd）與多 partition policy
- 這是讓「HPC 排程比純 K8s 更有利」真正發揮的關鍵，尤其是多人共享 GPU 時。

(5) 進階：與 K8s batch 生態共存/整合
- 選項 A：保留 Slurm 做 HPC queue，K8s 做 services（你現在的方向）。
- 選項 B：評估與 Kueue/Volcano 整合（讓 Slurm 與 K8s native batch 有一致的 quota/policy），但這通常是後期工作。


### 四、目前 K8s 資源對應表（方便之後做成 Slinky-like CRD）

（以 `namespace=slurm` 為主；名稱以你目前專案慣例為例）

Control plane / Access
- `StatefulSet/slurm-controller`：`slurmctld` + `munged`（含 readiness/liveness）
- `Service/slurm-controller`（Headless, `clusterIP: None`）：提供 `slurm-controller-0.slurm-controller...` 的穩定 DNS
- `Deployment/slurm-login`：使用者入口（`sbatch/srun/squeue`），通常會 mount shared PVC
- （可選）`Service/slurm-login`：若要提供 cluster 外部入口（NodePort/Ingress/SSH bastion）

Compute plane
- `StatefulSet/slurm-worker`：每個 Pod = 一個 Slurm node（`slurmd` + `munged`）
- `Service/slurm-worker`（Headless）：提供 `slurm-worker-{i}.slurm-worker...` 的穩定 DNS

Config / Secrets / Storage
- `ConfigMap/slurm-config`：`slurm.conf`（NodeName/Partition/SlurmctldHost 等）
- `Secret/slurm-munge-key`：`munge.key`（必須全域一致）
- `Secret/slurm-ssh-key`：login/controller/worker 之間若需要 ssh/scp 的 key（可逐步減少依賴）
- `PVC/<shared>` + `PV/<shared>`（或 hostPath/CSI）：提供 `/shared` 作為 job output 與 smoke 驗證路徑

Elasticity / Governance
- `Deployment/slurm-elastic-operator`：watch `squeue`/pending job 狀態，調整 `StatefulSet/slurm-worker` replicas
- （建議新增）`ConfigMap/slurm-elastic-config`：把 min/max/cooldown/poll interval 參數化
- （建議新增）`ServiceAccount/Role/RoleBinding`：最小權限（只允許讀 pod/sts、patch sts replicas、exec login/controller）

觀測與除錯（可選）
- `ServiceMonitor/PodMonitor`（Prometheus operator）：抓 slurm exporter/metrics
- `NetworkPolicy`：讓 slurmd/slurmctld/munged 所需 port 可互通（並限制其他流量）

### 五、ASCII 架構圖（Current vs. Future）

Current（你現在的設計：slurmd pods + 自製 elastic operator）：

    [Users]
      |
      |  sbatch/srun/squeue
      v
    +---------------------+             +-------------------------------+
    |  slurm-login (Dep)  |  SSH/CLI    |  slurm-controller-0 (STS)     |
    |  sbatch client      +------------>+  slurmctld + munged           |
    |  /shared (PVC)      |             |  /etc/slurm (CM)              |
    +----------+----------+             +---------------+---------------+
               |                                        |
               | shared output                           | slurmctld RPC
               v                                        v
         +-----------+                     +----------------------------+
         |  /shared  |<------------------->|  slurm-worker-{i} (STS)    |
         | PVC / PV  |   job I/O, logs     |  slurmd + munged           |
         +-----------+                     |  /etc/slurm (CM)           |
                                           +----------------------------+

    +-----------------------------------------------+
    | slurm-elastic-operator (Dep)                  |
    | - watches pending jobs (via login/controller) |
    | - scales slurm-worker StatefulSet replicas    |
    +-----------------------------------------------+

Future（靠近 Slinky operator：宣告式 CRD + reconcile + 更完整的治理）：

    [Users] -> [slurm-login] -> [slurmctld]
                         ^         |
                         |         v
                   +-----+-------------------+
                   | Slurm Operator (CRD)    |
                   | - SlurmCluster          |
                   | - SlurmNodeSet(min/max) |
                   | - Partitions/QOS        |
                   | - DNS gate + node FSM   |
                   +-----+-------------------+
                         |
                         v
              K8s objects (STS/Dep/Svc/CM/Secret/PVC)
                         |
                         v
                  slurm-worker pods (elastic)

註：如果未來要更進一步走到「Slurm job -> K8s Pod」的 slurm-bridge 路線，架構會在 slurmctld 與 compute plane 之間多一層 bridge/launcher（把 job step 映射成 Pod/Job），那會牽涉到更大幅度的設計變更（但對 Kubernetes 原生 batch 生態整合也更深）。
