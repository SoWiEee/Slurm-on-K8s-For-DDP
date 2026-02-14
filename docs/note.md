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
