# Development Notes

這份筆記保留原本的階段紀錄，並補上目前已完成的 Phase 2-A ~ Phase 2-D 演進，以及這一輪開發實際踩到的坑。

---

# Development Notes (Phase 1)

## 目標

完成 Timeline 的 Phase 1：

1. 建立 Slurm Controller / Worker 映像。
2. 在 Kind 部署靜態 Slurm 叢集。
3. 讓 Pod 間具備 SSH 互通與 Munge 認證。

## 開發想法

### 1) 先穩定，再擴展

Phase 1 先不做 Operator 與自動擴縮，先把最小可用系統做穩：

- 固定 Controller + Login + baseline Worker。
- 以 StatefulSet 保障 Pod 命名穩定。
- `slurm.conf` 先採顯式節點名稱，避免動態註冊把除錯成本拉高。

### 2) 設定集中管理

- Slurm 設定放在 `ConfigMap`。
- SSH 與 Munge 金鑰放在 `Secret`，由腳本自動建立。
- 啟動流程放在 container command / entrypoint，讓 bootstrap 行為可追蹤。

### 3) 針對 timeout / CrashLoop 問題的修正

根據前期錯誤紀錄，Phase 1 追加了以下修正：

- 啟動時主動建立 Munge 所需目錄並修正權限。
- `munged` 啟動後立即檢查程序是否存在。
- `SlurmctldHost` 改成 `主機名(FQDN)` 形式。
- 為 controller / worker 增加 readiness / liveness probes。
- bootstrap 失敗時會自動蒐集 `describe` / `logs` / `get all`。

## 重要 root cause 紀錄

### A. `bash\r` / CRLF 問題

現象：

- `/usr/bin/env: 'bash\r': No such file or directory`
- 容器 entrypoint 直接失敗

原因：

- 腳本以 CRLF 被複製進 image。

修正：

- 轉為 LF。
- 重新 build image。

### B. Secret volume 唯讀，不能直接 chmod

現象：

- `chmod: changing permissions of '/etc/munge/munge.key': Read-only file system`

原因：

- K8s Secret mount 是唯讀。

修正：

- 改掛到 `/slurm-secrets/munge.key`。
- 啟動時複製到 `/etc/munge/munge.key` 後再 `chown/chmod`。

### C. `SlurmctldHost` / DNS 解析錯誤

現象：

- `This host ... not a valid controller`
- `NO NETWORK ADDRESS FOUND`

修正：

- 在 `slurm.conf` 明確設定 `NodeAddr` / `NodeHostname`。
- controller 改用 `slurm-controller-0(slurm-controller-0....svc.cluster.local)`。

### D. `create-secrets.sh` 造成 bootstrap 中途停住

這一輪開發的關鍵修正之一。

現象：

- `bootstrap-dev.sh` 看似跑到 create-secrets，但後面完全沒 apply manifests。
- namespace 已建立，但 `slurm` 底下沒有任何 workload。

原因：

- `create-secrets.sh` 的 inline python 區段有問題，腳本被 `set -euo pipefail` 中斷。
- 上層 bootstrap 沒有走到 `applying phase1 manifests`。

修正：

- 修正 `create-secrets.sh`，確認 exit code 正常。
- bootstrap 才能順利接續 Phase 1 與 Phase 2 部署。

---

# Development Notes (Phase 2)

## 目標

完成 Timeline 的 Phase 2：

1. 開發 Python Operator，實作 `Pending Job -> Scale Up`。
2. 實作 `Idle Node -> Scale Down`。
3. 讓整個 Phase 1 + Phase 2 能以 `bootstrap-dev.sh` / `verify-dev.sh` 穩定驗證。

## 開發想法

### 1) 先做可用 MVP，再往策略抽象化

Phase 2 一開始先用單一 Python polling loop 完成 MVP：

- 讀 Slurm queue。
- 判斷 pending / running / busy。
- patch 對應 StatefulSet replicas。

等核心路徑穩定後，再往 Phase 2-A ~ 2-D 演進。

### 2) 先把「可觀測性」做起來

因為這類專案的 bug 很多都不是「功能根本沒寫」，而是：

- Slurm 狀態在某個時間點暫時不一致。
- DNS 尚未收斂。
- Pod 剛起來但 Slurm 尚未 reconfigure。
- queue / node / partition 查詢偶發 timeout。

所以這一階段重點之一，是讓 operator 與 verify 能提供足夠 debug 訊息，而不是只在失敗時吐一句 exit 1。

---

# Development Notes (Phase 2-A)

## 目標

把原本單體式 operator loop 做等價重構，整理成之後可延伸的架構。

## 已完成內容

- 將 operator 拆成 collector / policy / actuator。
- 以 dataclass 描述 `ClusterState` / `ScalingDecision`。
- 保持與原本 Phase 2 核心策略等價。

## 為什麼要做

因為如果直接在原本 polling loop 上硬塞：

- multi-pool
- checkpoint guard
- 不同 constraint / gres 規則
- 後續 DDP policy

最後只會變成一大坨 if-else，無法維護。

## 結果

這一步本身不追求新功能，重點是把後面 Phase 2-B / 2-C / 2-D 的地基打好。

---

# Development Notes (Phase 2-B)

## 目標

加入結構化日誌，讓 autoscaling 行為可追蹤、可分析、可做報告。

## 已完成內容

- 改用 JSON line logger。
- 補齊：
  - `startup`
  - `loop_observation`
  - `scale_action`
  - `scale_skipped`
  - `error`

## 實際價值

這一輪你提供的大量 operator log，其實就是 Phase 2-B 最有價值的成果之一。沒有這層，你很難確認：

- 它到底在看哪個 pool。
- 當下判斷是 `pending_jobs` 還是 `no_pending_jobs`。
- 為什麼 keep 不 scale。
- 是 CPU pool 還是 GPU pool 在迴圈中被判斷。

## 之後可做的分析

- scale-up latency
- scale-down latency
- 抖動次數
- pending job 對各 pool 的命中率

---

# Development Notes (Phase 2-C)

## 目標

把單一 worker pool 擴展成 multi-pool / partition-aware autoscaling。

## 已完成內容

- 引入 `PARTITIONS_JSON`。
- 可同時描述：
  - `slurm-worker-cpu`
  - `slurm-worker-gpu-a10`
  - `slurm-worker-gpu-h100`
- 每個 pool 有獨立的 min/max/cooldown/step。
- 每個 pool 可用 `match_features` / `match_gres` 指派工作。
- 保留 `fallback` 機制，讓 CPU pool 可接 baseline 工作。

## 這一輪實際踩到的坑

### A. Deployment 明明是新版 manifest，實際跑的卻是舊 operator env

現象：

- `kubectl get deploy ... -o yaml` 裡只有 `WORKER_STATEFULSET=slurm-worker-cpu`
- 看不到 `PARTITIONS_JSON`
- operator log 也只在處理 CPU pool

原因：

- live deployment 沒被真正替換成功，或 apply 後 env 仍沿用舊值。

修正：

- `bootstrap-dev.sh` 內加入 `force_replace_operator_deployment`。
- 加上 `operator_force_env`，直接用 `kubectl set env` 把 runtime env 強制覆蓋。
- 再用 `validate_live_operator_config` 驗證 live deployment 裡真的有 `PARTITIONS_JSON`。

### B. `duplicate partition in config: debug`

現象：

- operator 啟動時直接 `ValueError: duplicate partition in config: debug`

原因：

- 原本 validation 把「partition 名稱重複」當成非法，但現在多 pool 共享同一個 Slurm partition 是設計需求，不是錯誤。

修正：

- validation 不能用 partition name 當唯一鍵。
- 要接受「同一 partition 對應多個 worker pool」。

### C. 為何 live `slurm.conf` 一直解析不存在的 worker

現象：

- controller log 不斷嘗試解析：
  - `slurm-worker-cpu-1`
  - `slurm-worker-cpu-2`
  - `slurm-worker-gpu-a10-0`
  - `slurm-worker-gpu-h100-0`
- 但這些 Pod 當下根本沒存在。

原因：

- `slurm.conf` 內預先宣告了 max node set。
- Slurm 在 reconfigure / node query 時會嘗試解析所有已宣告節點。
- 在 K8s 中這不代表一定是致命錯誤，但會讓 query 偶發 timeout、state 顯示 dirty。

結論：

- 這不是單純 verify 腳本錯，而是 multi-pool 靜態節點宣告與動態 Pod 存在性之間的張力。
- verify 需要避免在最脆弱的時間點瘋狂打 `sinfo -N -l` / `scontrol show node`。

---

# Development Notes (Phase 2-D)

## 目標

加入 checkpoint-aware scale-down guard，避免正在跑的工作因過早縮容而丟失恢復點。

## 已完成內容

- 支援 `CHECKPOINT_GUARD_ENABLED`。
- 支援 `CHECKPOINT_PATH`。
- 支援 `MAX_CHECKPOINT_AGE_SECONDS`。
- 當 queue 清空但仍有 running jobs 時：
  - checkpoint 狀態未知，可阻擋縮容。
  - checkpoint 過舊，可阻擋縮容。

## 實際觀察

你貼出的 log 裡出現過：

- `checkpoint_unknown_block_scale_down`

這代表 guard 已經真的進入決策路徑，不只是參數存在而已。

---

# verify-dev.sh 演進與真實踩坑

這一輪真正花很多時間的，不只 operator 本體，還有 verify 腳本。

## 問題 1：早期 verify 假設太樂觀

現象：

- `sinfo` / `squeue` / `scontrol show node` 偶發 timeout。
- verify 一遇到就整支退出。

原因：

- Slurm 在 controller 剛 reconfigure、worker 剛註冊、DNS 尚未完全穩定時，client query 會短暫 flaky。

修正方向：

- 增加 warm-up。
- 對 query 做 retry。
- inventory 檢查改得更保守，避免一開始就對全節點做 heavy query。

## 問題 2：verify 把 debug 信息打太多，反而干擾 Slurm

現象：

- `scontrol show node` / `sinfo -N -l` 在不對的時間點會放大 controller 端 timeout 問題。

修正方向：

- baseline path 優先驗證「目前存在且 ready 的 worker」。
- multi-pool 驗證改成用 job 行為驗證，而不是靠重查所有節點靜態資訊。

## 問題 3：GPU smoke job 有時 scale 了，但 job 看起來 `<gone>`

原因通常有兩種：

1. Slurm query timeout，job 其實還在。
2. job 很快完成 / 被清掉，verify 追蹤方式太脆弱。

修正方向：

- 用更穩定的 job state 觀察。
- 驗證重點放在：
  - operator 是否把 GPU pool 從 0 拉到 1
  - job 最終是否曾落在 `slurm-worker-gpu-a10-0`
  - 工作結束後 pool 是否縮回 0

## 問題 4：`cpu smoke job did not leave queue after cancel`

現象：

- job 已取消，但 queue 狀態短時間仍殘留，verify 太快判定失敗。

修正方向：

- cancel 後加入收斂等待。
- 對 queue 消失做 retry，不立刻當成錯誤。

---

# 目前可接受的系統現象

以下現象目前可以接受，不應直接視為功能失敗：

1. controller log 中偶爾出現對不存在 FQDN 的解析錯誤。
   - 因為 `slurm.conf` 有宣告 max node set。
   - 在動態 Pod 尚未存在時，controller 可能暫時解析不到。

2. `squeue` / `sinfo` 偶發 timeout。
   - 在 reconfigure 或 node registration 時可能發生。
   - verify 已盡量降低對這些瞬時抖動的敏感度。

3. GPU pool 預設為 0 replicas。
   - 只在有對應 constraint / gres 的工作時才拉起。
   - 驗證時這是預期行為。

---

# 目前通過的 acceptance path

根據目前開發結果，以下主路徑已可視為通過：

## CPU path

- controller / login / baseline cpu worker 啟動成功
- `scontrol ping` 成功
- `srun` 成功
- `sbatch` CPU smoke job 成功
- CPU scale-up 成功
- CPU scale-down 成功

## GPU path

- GPU job 送出後，operator 會把 `slurm-worker-gpu-a10` 從 0 拉到 1
- job 可落到 `slurm-worker-gpu-a10-0`
- 工作完成後 pool 可縮回 0

## 結論

這代表目前的 **Phase 2 核心、Phase 2-A、Phase 2-B、Phase 2-C、Phase 2-D** 已有可驗證的工作路徑。

---

# 後續建議

## 1. 先不要再把 verify 變成大型 debug 掃描器

verify 的責任是驗證主路徑，不是取代人工 debug。

太重的查詢會：

- 放大 Slurm 暫時不穩的窗口
- 製造新的 timeout
- 讓驗證腳本自己成為干擾源

## 2. 下一步該進到真正的 Phase 3 工作

也就是把目前 Phase 2-D 的 checkpoint-aware 邏輯，接上真正 workload：

- PyTorch DDP CPU workload
- `/shared/checkpoints`
- resume
- `--requeue`
- 故障恢復量測

## 3. 如果要再優化 Slurm/K8s 邊界

可以考慮：

- 降低 `slurm.conf` 預宣告節點數，減少不存在 FQDN 帶來的 query timeout
- 或在未來導入更接近 operator-managed node lifecycle 的做法，讓 Slurm node 宣告與 K8s live pod 更同步

---

# Development Notes (Phase 3 - Shared Storage Milestone)

這部分保留原始規劃方向，因為目前 Shared Storage / DDP / requeue / 恢復量測仍是接下來的工作重點。

## 目標

1. 在 Kind 單機環境部署 NFS Server 並整合 `nfs-subdir-external-provisioner`。
2. 建立 StorageClass + RWX PVC。
3. 將 Controller / Worker / Login 掛載共享儲存。
4. 將 Phase 2-D 的 checkpoint-aware guard 與真實 workload 串起來。

## 備註

目前文件層次已調整為：

- Phase 2
  - Phase 2-A
  - Phase 2-B
  - Phase 2-C
  - Phase 2-D
- Phase 3
  - Shared Storage + 應用整合 + 容錯

這樣比較符合目前實際開發順序，也避免把已完成的 operator 演進錯掛到 Shared Storage 底下。
