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

# Development Notes (Phase 2-E)

## 目標

在**單一 Kubernetes cluster** 內引入兩個子網路，將目前 Slurm-on-K8s 架構區分成：

- **management subnet**：給 `slurm-controller`、`slurm-login`、`slurmd/slurmctld` 控制流量、SSH、健康檢查、Kubernetes API 溝通使用
- **data subnet**：給 worker 間的 DDP / MPI / checkpoint / east-west data flow 使用

## 這一輪新增的檔案

- `phase2/manifests/slurm-phaseE-topology.yaml`
  - 用 `ConfigMap/slurm-topology` 描述 dual-subnet 拓撲
- `phase2/manifests/slurm-phaseE-multus.example.yaml`
  - 提供 `slurm-mgmt-net` / `slurm-data-net` 的 Multus 範例
- `phase2/scripts/bootstrap-phase2e.sh`
  - 套用 topology，並把 login / worker / controller / operator 的 pod template 補上 network annotation
- `phase2/scripts/verify-network.sh`
  - 以可視化方式展示拓撲，並在 Multus 已安裝時驗證 runtime network attachment

## 設計判斷

### 1. 這次不把 operator 當成 data plane 成員

`slurm-elastic-operator` 的工作主要是：

- 查 Slurm queue / node state
- patch StatefulSet replicas
- 寫 log

它不是高流量資料交換元件，所以只放在 **management subnet** 即可。若硬把 operator 拉進 data subnet，只會增加複雜度，沒有明顯效益。

### 2. login 與 worker 要雙網

這次規劃裡：

- `slurm-login` 同時接 `management + data`
- `slurm-worker-*` 同時接 `management + data`
- `slurm-controller` / `slurm-elastic-operator` 僅接 `management`

這個切法的好處是：

- Slurm 控制流量維持單純
- 之後若要做 PyTorch DDP / MPI / NCCL，能逐步把高流量傳輸導向 `data subnet`
- verify 時也能清楚展示哪些元件是 control plane、哪些元件是 dual-homed compute plane

### 3. 這一版先做可落地 scaffold，不強迫 repo 立刻全面切到第二張網

原因很直接：你目前的 Phase 1 / Phase 2 主路徑已經跑通，若現在直接把 `slurmctld <-> slurmd` 全面改成第二張 NIC，風險很高。

所以這一版的策略是：

1. 先用 topology + annotations 把雙子網設計嵌進 repo
2. 讓 `verify-network.sh` 可以在**沒有 Multus** 時先做拓撲驗證
3. 若 cluster 內已安裝 Multus，再做 runtime dual-network 驗證

這樣你拿給教授看時，不會陷入「環境少一個 CNI 元件就整套不能 demo」的窘境。

## verify-network.sh 的展示方式

這支腳本故意分成兩層：

### A. Topology view

不依賴 Multus。會直接展示：

- management subnet / data subnet 名稱與用途
- controller / operator / login / worker 的邏輯歸屬
- ASCII 架構圖
- pod template 上是否已有 `k8s.v1.cni.cncf.io/networks` annotation

### B. Runtime view

若 cluster 已有 Multus 與 `network-status` annotation，則額外展示：

- `slurm-mgmt-net` / `slurm-data-net` 是否存在
- Pod 實際拿到哪些 interface
- 每張網卡對應的 IP

## 目前限制

### 1. 這不是「開箱即用的真雙網」，而是以 repo 現況為基礎的最小可用落地版

因為你目前用的是 kind，原生不會自己給第二張 Pod NIC。真正要讓 runtime 出現雙網，還需要：

- Multus CNI
- `NetworkAttachmentDefinition`
- cluster node 內對應 bridge / IPAM 可正常工作

### 2. operator 目前還沒有用 topology 自動決定某個 job 要走哪張 NIC

也就是說，Phase 2-E 目前完成的是：

- **網路拓撲建模**
- **workload placement 規劃**
- **runtime 驗證 scaffolding**

下一步若要更深入，才是：

- 將 `data interface` 寫入 worker 啟動流程
- 讓 DDP / MPI workload 明確選用 `net2`
- 將 checkpoint / shared storage traffic 與 control traffic 分離

## 建議後續里程碑

### Phase 2-E.1
- 安裝 Multus 到 dev cluster
- 套用 `slurm-phaseE-multus.example.yaml`
- 跑 `verify-network.sh` 做 live demo

### Phase 2-E.2
- 在 worker 啟動腳本中把 `net2` 暴露成可用資料網路介面
- 補一個小型 worker-to-worker `iperf3` 或 `ping` 驗證

### Phase 2-E.3
- 在 Phase 3 的 DDP / checkpoint workload 中，明確使用 data subnet
- 量測 control path 與 data path 分離後的穩定度差異


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

---

# Development Notes (Phase 2-E Proposal: Single Cluster Dual-Subnet Design)

這一節是**基於目前專案結構**，規劃「單一 Kubernetes cluster 內的兩個子網路」該怎麼落地。這裡先講結論：

- 你的題目**有可能**被要求做跨網路，但通常教授要看的不是「硬切兩個 namespace 或兩台機器」這麼表面。
- 真正有價值的是：
  - 你能不能把 **control plane traffic** 跟 **data plane traffic** 分開。
  - 你能不能描述 **不同 worker pool / 不同 workload class** 的網路需求。
  - 你能不能讓未來的 DDP / MPI / checkpoint I/O 有比較合理的網路演進空間。

如果你只是把「operator 一池、worker 一池」硬拆成兩個網路，這個切法其實很弱，因為 operator 並不是高資料流量角色，切它沒有明顯收益。

## 一、先釐清：為什麼你的題目可能會用到跨網路

### 1. Slurm + K8s 本來就有兩種流量

在你現在的架構裡，至少有兩種性質不同的流量：

1. **管理面流量**
   - `slurmctld <-> slurmd`
   - `login <-> controller`
   - operator 呼叫 `kubectl` / K8s API
   - readiness probe / service discovery / DNS

2. **資料面流量**
   - MPI / NCCL / PyTorch DDP worker-to-worker
   - checkpoint / dataset / shared storage I/O
   - 後續若接真實 training，這部分才是大流量來源

教授若要求你做跨網路，通常是在逼你思考：

- 這兩種流量能不能分離
- 分離之後有沒有比較接近真實 HPC / AI cluster
- 當網路特性不同時，scheduler / operator / workload placement 是否要跟著調整

### 2. 好處是什麼

如果做對，好處有三個：

#### A. 降低 control plane 被 data plane 干擾

訓練流量大時，若全部都走同一張 Pod 網路，容易讓：

- Slurm query timeout
- node registration 變慢
- `sinfo` / `squeue` / `scontrol` 抖動

你前面已經碰過很多 timeout。雖然主因不只網路，但把資料面與管理面切開，確實能降低這種風險。

#### B. 更接近真實叢集設計

真實 HPC / AI cluster 很常是：

- 管理網路一套
- 高速資料網路另一套

你現在是 Kind 單機，當然不會真的變快很多，但**架構概念**會更完整。

#### C. 讓後續 Phase 3 / DDP / fault tolerance 有研究價值

你之後若要做：

- DDP 多節點訓練
- checkpoint-aware autoscaling
- worker class 的網路感知調度

那麼「不同網路對不同 workload 的影響」本身就能變成可寫進報告的內容。

## 二、基於目前專案，最合理的切法是什麼

## 結論先講

**最合理的切法不是 `worker 一池 / operator 一池`，也不是 `CPU pool 一網 / GPU pool 一網`。**

基於你現在的結構，最合理的是：

### 方案：

- **Net-A：管理子網路（management subnet）**
  - 所有 Pod 都保留既有 K8s 預設 Pod 網路
  - 給 controller、login、operator、worker 全部使用
  - 負責 Slurm control traffic、K8s API、DNS、probe、一般 service discovery

- **Net-B：資料子網路（data subnet）**
  - 只額外掛到需要高資料流量的 Pod
  - 第一階段先掛到 `slurm-login`、`slurm-worker-cpu`、`slurm-worker-gpu-a10`、`slurm-worker-gpu-h100`
  - controller / operator **不要**先掛，避免把 control plane 複雜化
  - 未來 DDP / MPI / NCCL / checkpoint 可優先綁這張網卡

這樣才符合你的專案現況。

### 為什麼這樣切比較合理

因為你現在真正需要解決的是：

- Slurm control plane 要穩
- worker-to-worker 的資料面未來要能擴展

所以控制面要盡量保守，資料面再額外拉出來。

如果你把 operator 拉去另一個網路，收益很小，複雜度卻上升。

如果你把 CPU / GPU 直接硬拆成兩張不同網路，也太早了。因為現在 CPU / GPU 的差異主要是 **resource / constraint / gres**，不是網路型態。

## 三、在單一 cluster 內，兩個子網路應該怎麼落地

你現在是 Kind，代表：

- 預設 CNI 會給你一套 Pod 網路
- 你若要第二張網路，通常要靠 **Multus CNI**
- IP 分配可以搭配 **Whereabouts**

### 建議組合

1. **Primary network**
   - 保留 Kind/K8s 預設 Pod network
   - 不要動 controller、operator 的主通訊路徑

2. **Secondary network**
   - 導入 `Multus + Whereabouts`
   - 用 `NetworkAttachmentDefinition` 提供額外介面
   - worker / login Pod 透過 annotation 掛第二張 NIC

### 推薦的 CNI 型態

若你只是要在**單機 Kind**裡做概念驗證：

- 可先用 `bridge` + `whereabouts`
- 因為最容易在本地驗證

若你未來真的要更接近實機 L2 行為：

- 可再評估 `macvlan` 或 `ipvlan`
- 但在 Docker Desktop / Kind / Windows 這組環境下，除錯成本會顯著上升

### 實際上可定義成：

- `slurm-mgmt`：其實沿用預設 pod network，不額外建 NAD
- `slurm-data`：額外的 secondary subnet，例如 `192.168.20.0/24`

也就是說，你口頭上是兩個子網路，但實作上通常會是：

- 一個是預設 cluster pod CIDR
- 一個是 Multus 附加網段

這是最務實的做法。

## 四、基於你現在結構，哪些元件該掛哪個網路

## 1. `slurm-controller`

建議：

- **只保留 management subnet**
- 不要先掛 data subnet

理由：

- 它是控制面核心
- 你現在已經有 `slurmctld` query timeout、reconfigure、FQDN resolve 等問題
- 先別把 controller 的網路模型弄更複雜

## 2. `slurm-elastic-operator`

建議：

- **只保留 management subnet**

理由：

- 它只需要 K8s API + Slurm query
- 不是資料面角色
- 把它丟進第二網路沒有實際收益

## 3. `slurm-login`

建議：

- **management subnet + data subnet 雙介面**

理由：

- 它是提交工作的入口
- 後續你若要在 login container 內測試 MPI/DDP，會需要直接觸碰資料面
- 它也可當作除錯入口，檢查 worker 間資料網路是否互通

## 4. `slurm-worker-cpu`

建議：

- **management subnet + data subnet 雙介面**

理由：

- 現在 CPU pool 是 baseline pool
- Phase 2 驗證、DDP CPU 原型、MPI smoke test 都可能先落在這裡
- 不該只有 control network

## 5. `slurm-worker-gpu-a10` / `slurm-worker-gpu-h100`

建議：

- **management subnet + data subnet 雙介面**

理由：

- 真正高資料流量 workload 最後多半會落在這裡
- 之後若要區分 NCCL/訓練流量走哪張卡，這些 pool 是主要受益者

## 五、你問的「要不要切成兩個網路，各自放不同元件」的更精確回答

### 不建議的切法

#### 切法 A：worker 一池、operator 一池

這很像做了切分，但其實沒有抓到重點。

問題：

- operator 流量很小
- worker 才是真正資料面主角
- 這種切法無法支撐你後續 DDP / MPI 的論述

#### 切法 B：CPU 一網、GPU 一網

這也太快。

問題：

- 你現在 CPU / GPU 差異是 compute class，不是 network class
- 除非你後續真的要模擬不同 fabric，例如：
  - CPU pool 走一般乙太網
  - GPU pool 走高速 fabric
- 否則這樣切只是增加驗證成本

### 比較好的切法

#### 切法 C：control plane / data plane

這才是目前最值得做的切法。

也就是：

- controller / operator 只走管理網
- login / workers 除了管理網，再多一張資料網

這個切法有明確理由，也容易寫進設計文件與口試說明。

## 六、基於目前 repo，可怎麼接到現有結構

你現在 repo 裡已經有這些很適合承接網路拓撲的地方：

- `phase1/manifests/worker-pools.json`
- `phase2/manifests/slurm-phaseA-topology.yaml`
- `phase2/manifests/slurm-phaseB-topology.yaml`
- `phase2/operator/main.py`

這代表你其實已經有「拓撲配置」這個概念，只是目前偏向：

- worker class
- node set
- autoscaling policy

下一步可以把 network 屬性補進同一條鏈。

### 建議新增的 topology 欄位

可在 `workerClasses` 或 `nodeSets` 裡加入：

```json
{
  "name": "gpu-a10-workers",
  "workerClass": "gpu-a10",
  "partition": "debug",
  "statefulset": "slurm-worker-gpu-a10",
  "serviceName": "slurm-worker-gpu-a10",
  "networkAttachments": ["slurm-data"],
  "networkRole": "data-plane"
}
```

對 CPU pool 也加：

```json
{
  "name": "cpu-workers",
  "workerClass": "cpu-standard",
  "partition": "debug",
  "statefulset": "slurm-worker-cpu",
  "serviceName": "slurm-worker-cpu",
  "networkAttachments": ["slurm-data"],
  "networkRole": "data-plane"
}
```

而 controller / operator 則標成：

```json
{
  "networkRole": "control-plane",
  "networkAttachments": []
}
```

### 後續 manifest 生成器應做的事

你現在 `phase1/scripts/render-slurm-static.py` 已經會從 `worker-pools.json` 生成 StatefulSet。

所以之後可擴成：

1. 若 pool 有 `networkAttachments`
2. 就在 Pod template metadata 加上 Multus annotation
3. 例如：

```yaml
metadata:
  annotations:
    k8s.v1.cni.cncf.io/networks: slurm-data
```

這樣可以把「拓撲配置 -> manifest 生成」串成一條完整 pipeline。

## 七、推薦你分成兩個落地階段

## Phase 2-E.1：先做結構正確，但不追求性能提升

目標：

- 保留既有 Phase 1 / Phase 2 能運作
- 額外讓 login / worker 掛第二張網卡
- 驗證第二張網卡存在、能互 ping、能做基本通訊

你應該先做到：

1. 安裝 Multus
2. 建立 `slurm-data` 的 NAD
3. 只改 login / worker manifests
4. verify 增加：
   - Pod 內 `ip addr` 可看到第二張介面
   - login 與 worker 能經資料網互通

### 這一階段不要做的事

- 不要一開始就改 controller 通訊走第二網
- 不要一開始就想讓 Slurm NodeAddr 改綁第二網
- 不要一開始就混入 NCCL / MPI / shared storage 調校

不然你會一次炸三個層面，根本無法 debug。

## Phase 2-E.2：再做 workload-aware network usage

第二階段才考慮：

- `NodeAddr` 是否切到 data subnet
- MPI/DDP 是否顯式綁第二張 NIC
- checkpoint I/O 是否透過 data plane 減少 management 干擾
- autoscaling policy 是否考慮 network class

這階段才比較像研究題目。

## 八、可參考的開源專案 / 元件

以下是你該看的，不是因為它們和你專題一模一樣，而是因為它們各自解決你會碰到的某一塊問題。

### 1. Multus CNI

用途：

- 在 K8s Pod 上掛多張網卡
- 這是你做單 cluster 雙子網最核心的元件

你若不導入 Multus，基本上很難把現在的 Pod 做成「主網 + 資料網」雙介面模型。

### 2. Whereabouts

用途：

- 給 secondary network 做 IPAM
- 很適合 Multus 附掛網段

因為你需要讓 worker / login 的第二張網卡在同一個附加網段拿到可管理 IP。

### 3. NVIDIA k8s-device-plugin

用途：

- 若未來 GPU pool 要更像真實環境，這是 GPU 資源宣告基礎

你現在是以 Slurm feature / gres 模擬 GPU pool，這在專案初期是合理的。但若教授往「真實 GPU 調度」追問，這條線你得知道。

### 4. Volcano / kube-batch

用途：

- K8s 上的 batch / gang scheduling 參考實作

不是要你改用它，而是你可以借它們理解：

- HPC/AI workload 在 K8s 上常怎麼描述拓撲、queue、資源群組
- 網路 / 資源池 / job class 怎麼在控制器層被表達

### 5. Kubeflow Training Operator / MPI Operator

用途：

- 看分散式訓練 workload 在 K8s 內怎麼處理 worker 通訊與 Pod 角色

你現在不是要直接搬它，但它能讓你理解：

- 為什麼 login / launcher / worker 的網路角色不同
- 為什麼 data plane 和 control plane 分離有價值

## 九、你現在最該避免的錯誤假設

### 假設 1：跨網路一定要拆成兩群元件

不一定。

更合理的是：

- 同一批 worker 同時有兩張網卡
- 一張負責管理，一張負責資料

### 假設 2：做跨網路一定會讓效能大幅變好

在 Kind + Docker Desktop + 單機下，**不會有明顯真實效能提升**。

這件事你要誠實。這比較像：

- 架構驗證
- 拓撲建模
- 為未來實體叢集或多節點實驗打底

### 假設 3：先把 Slurm 通訊全面切到第二網路比較厲害

這通常是更容易炸。

你現在最需要的是**穩定可驗證**，不是一次把所有通訊改掉。

## 十、我對你目前專案的具體建議

若你要把「單一 cluster 內兩個子網路」變成下一個可交付里程碑，我建議這樣切：

### 建議的里程碑名稱

- **Phase 2-E: Dual-Network Topology in Single Cluster**

### 里程碑內容

1. 導入 Multus 與 secondary subnet
2. login / worker 掛第二張 data-plane NIC
3. controller / operator 保持只走 management network
4. topology 檔補上 network metadata
5. verify 增加雙網卡存在性與資料網路互通檢查
6. 保持目前 CPU / GPU autoscaling 邏輯不變

### 驗收條件

- `bootstrap-dev.sh` 仍可完成部署
- `verify-dev.sh` 原有 CPU / GPU 路徑不被破壞
- login / worker 內可看到第二張介面
- 指定 Pod 能透過 data subnet 互通
- 文件能說清楚 control-plane / data-plane 分工

## 十一、最後結論

基於你現在的 repo 結構，**最值得做的單一 cluster 兩子網方案**是：

- **既有 Pod network 當 management subnet**
- **Multus 附加 network 當 data subnet**
- **controller / operator 留在 management only**
- **login + all worker pools 掛 management + data 雙網卡**

這個方案有幾個優點：

- 不會直接破壞你現在已經跑通的 Phase 1 / Phase 2 主路徑
- 能合理回答「為什麼要跨網路」
- 能和你現有的 topology / worker pool / autoscaling 結構接起來
- 能為之後的 DDP、checkpoint、shared storage、network-aware scheduling 留出空間

如果你下一步真的要做，我建議先把它當成 **Phase 2-E**，而不是急著塞進 Phase 3。因為它本質上還是在補強「elastic multi-pool control/data topology」，還沒進到 shared storage / workload recovery 的核心。


# Development Notes (Phase 2-E MVP)

## 目標

把原本只有 topology / annotation scaffold 的雙子網設計，補成真正可執行的 MVP。

## 已完成內容

- 將 `slurm-ddp-runtime` ConfigMap 納入基礎 manifest，讓 login / worker pod 啟動時就會安裝 `/opt/slurm-runtime/ddp-env.sh`。
- `ddp-env.sh` 會把 `NCCL_SOCKET_IFNAME`、`GLOO_SOCKET_IFNAME`、`SLURM_DATA_IFACE` 綁到 `net2`，讓 DDP collective traffic 可明確走 secondary NIC。
- `phase2/scripts/bootstrap-phase2e.sh` 改成預設要求 Multus runtime，並在有 CRD 時套用正式 `NetworkAttachmentDefinition`。
- `phase2/manifests/slurm-phaseE-runtime.yaml` 最終改成只建立 `slurm-data-net`，不再建立 `slurm-mgmt-net`。MVP 保留 `kindnet` 當 primary management network。
- `slurm-data-net` 在 Kind dev cluster 採 `ptp + host-local IPAM`，因為實測環境有 `ptp` plugin，沒有 `bridge` plugin。前期使用 `bridge` 的版本會直接在 CNI 階段失敗。
- `phase2/scripts/verify-network.sh` 從單純展示 topology，升級為檢查 annotation、`network-status`、container 內 `net2`、secondary IPv4、runtime helper 是否存在，以及 `ddp-env.sh` 內的 DDP/NCCL/Gloo 綁定輸出。
- login 對 worker 的 data-plane SSH probe 被降級成 warning-only，因為它不是這個 MVP 的必要條件，失敗常常反映的是 SSH 授權模型，而不是 secondary NIC 本身失效。

## 設計邏輯

### 1. control plane 與 data plane 分流，但不強行搬動 Slurm control path

- `slurm-controller`、`slurm-elastic-operator` 只留在 `kindnet`。
- `slurm-login`、worker pools 走 `kindnet + net2`。
- `slurm.conf` 的 `NodeAddr` 暫時不搬到 data subnet。

原因很直接。這個專案目前仍依賴 StatefulSet FQDN、headless service、以及 static node 宣告維持穩定。如果在 MVP 階段就把 `NodeAddr` 全部搬去 secondary network，會同時引入 DNS、service discovery、scale-to-zero、以及 control/data path 混雜的問題，風險不成比例。

### 2. Kind 環境先求可執行，再談漂亮拓撲

原本設計過 `slurm-mgmt-net + slurm-data-net` 兩張 Multus 附加網路，但在實際 dev cluster 中踩到兩個硬問題：

- Kind node 內沒有 `bridge` plugin，`bridge` 型別的 NAD 會直接在 CNI sandbox 建立時失敗。
- 反覆的 failed sandbox 會把 primary `kindnet` IP pool 吃乾，讓整個 cluster 連 baseline dev workflow 都起不來。

所以最後收斂成：

- primary `kindnet` 當 management network
- Multus 只額外掛一張 `slurm-data-net`
- `slurm-data-net` 採 `ptp`

這不是最漂亮的 network fabric，但它能在當前 dev 環境把 secondary NIC 與 DDP 綁定邏輯做出來。

### 3. 驗證標準回到真正 relevant 的部分

MVP 的成功條件不是「login 一定要能 SSH 到 worker 的 data IP」，而是：

1. login / worker pod 都能 Running。
2. login / worker 的 pod template annotation 已正確掛上 `slurm-data-net`。
3. `network-status` 中可看到 `net2` 與 `192.168.20.x`。
4. container 內 `/proc/net/dev` 可看到 `net2`。
5. `/opt/slurm-runtime/ddp-env.sh` 存在。
6. source `ddp-env.sh` 後可看到 `NCCL_SOCKET_IFNAME=net2`、`GLOO_SOCKET_IFNAME=net2`、`SLURM_DATA_IFACE=net2`。

SSH probe 只保留成 warning-only，因為它驗到的是「SSH 授權是否正確覆蓋到 data-plane IP」，不是「secondary NIC 是否存在」。把它當 hard fail 會讓驗證結果被非核心因素污染。

## 這版刻意沒做的事

- 沒有把 `slurm.conf` 的 `NodeAddr` 改到 data subnet。MVP 仍維持 Slurm control traffic 走 management network。
- 沒有建立完整的 data-plane DNS / service discovery。`MASTER_ADDR` 仍建議由提交腳本明確指定。
- 沒有宣稱 `ptp` 版本已提供完整 shared east-west data fabric。它目前證明的是 secondary NIC 與 DDP 綁定能力，不是最終拓撲的終局。

## 為什麼這樣切

因為這樣才能先把「跨網路能力」收斂成一個可驗證、可維護、且不會把整個 baseline cluster 一起拖垮的 MVP。先讓 control plane 穩定、secondary NIC 可用、DDP env 可綁定，再談更完整的 data-plane rendezvous、worker-to-worker 連通性模型、或更高階的 secondary CNI 選型。


---

# Development Notes (Phase 2-E follow-up: job packing / utilization on kind)

## 問題

這一輪確認的重點是：

1. 目前這個專案，能不能在同一台 `cpu-worker` 上同時跑多個 job，藉此提高 CPU 利用率。
2. `gpu-worker` 目前能不能做到類似的共享。
3. 若不能，實務上有哪些開源方案或可行方法。
4. 以上判斷都要放在 **目前是 kind（Kubernetes in Docker）模擬環境** 這個前提下理解。

## 先講結論

### CPU worker

**可以，但要分成「排程層可不可以」與「隔離層有沒有做紮實」兩件事來看。**

目前 repo 的 `slurm.conf` 採用：

- `SelectType=select/cons_tres`
- `SelectTypeParameters=CR_Core`
- 每個 CPU worker 宣告 `CPUs=4`

這代表 Slurm 會把 CPU 當成 consumable resource 來分配。只要多個 job 的 CPU 請求總和沒有超過該 node 的可用 CPU，**Slurm 是可以把多個 job 放到同一台 worker 上的**。Slurm 官方文件明確說明，使用 consumable resource（cons_tres）時，CPU 會配置給 job；不同 job 是否能共用同一顆 CPU，則取決於 OverSubscribe 設定。預設 `OverSubscribe=NO` 時，不會讓兩個 job 共用同一顆 CPU，但同一台 node 上仍可同時承載多個 job，只要它們使用的是不同 CPU 資源。citeturn832572search6turn998774search18turn832572search15

換句話說，**在目前這份設定下，同一台 `cpu-worker` 跑多個 job 是可能的，而且這其實就是提高單機 CPU 利用率的預設方向**。例如：

- job A 請求 1 CPU
- job B 請求 1 CPU
- job C 請求 2 CPU

在 `CPUs=4` 的 worker 上，這三個 job 可以同時被排進去，總計用滿 4 CPU。這不需要 `OverSubscribe`。`OverSubscribe` 只在你想讓多個 job **共用同一批 CPU** 時才需要。citeturn998774search18turn832572search15

### 但目前 repo 的限制很大

雖然 Slurm 排程層面允許 packing，但目前 repo 還沒有把 CPU/memory 隔離做完整。從現有 `slurm.conf` 可見：

- `TaskPlugin=task/none`
- `ProctrackType=proctrack/linuxproc`

這表示目前沒有啟用 Slurm 常見的 cgroup/task 隔離路徑。結果是：

- Slurm **會記帳與配置** CPU 數量
- 但它**不一定會強制把 job 嚴格限制在那幾顆 CPU 上**
- 多個 job 都跑在同一個 worker pod 裡時，Linux 行程層面可能彼此搶 CPU，而不是像正式 HPC 節點那樣有明確 cpuset/cgroup 約束

所以答案不能講太漂亮。**目前 CPU worker 的「多 job 共存」在排程語意上是可行的，但在 kind 模擬環境下，資源隔離與效能可預測性偏弱。**

### GPU worker

**目前不應假設可以安全地在同一張 GPU 上同時跑多個 GPU job。**

原因有兩層。

第一層是 Slurm / K8s 的資源模型。repo 目前把 GPU worker 宣告成：

- `Gres=gpu:a10:1` 或 `Gres=gpu:h100:1`

這是典型的「一張卡就是一個 consumable GPU resource」配置。若 job 請求 `--gres=gpu:a10:1`，那張 GPU 在 Slurm 看來就會被整張配置給那個 job。這種配置預設不是拿來做多 job sharing 的。Slurm 文件也指出，像 GPU 這類 GRES/TRES 會被當成可分配資源記帳與分配。citeturn998774search7turn832572search18

第二層是 Kubernetes / 裝置插件語意。NVIDIA 的 k8s device plugin 預設是把 GPU 以 extended resource 方式暴露給容器，一般語意是一個請求拿到一個 GPU 資源單位。若沒有另外啟用 time-slicing、MIG 或其他 sharing 機制，就不應把「多個 GPU job 在一張卡上共享」當成預設可行行為。citeturn998774search4turn832572search2turn998774search6

因此，**目前這個 repo 的 GPU worker 比較接近「每張 worker pod 對應一張獨占 GPU」的模型，不是 GPU sharing 模型。**

## 那目前預設行為到底會怎樣

### CPU worker 的預設行為

若 job 沒有把整台 node 吃滿，**同一台 CPU worker 可以被排入多個 job**。但前提是 job 本身要正確申請 CPU，例如：

- `--cpus-per-task=1`
- `--ntasks=1`
- 或總 CPU 需求沒有超過 node 的 `CPUs=4`

若你提交的 job script 沒有清楚聲明 CPU 需求，或應用程式自己在容器裡開太多 threads，最後實際上可能會出現：

- Slurm 認為只分了 1 CPU
- 應用程式卻在 worker pod 裡吃超過 1 CPU

這是目前 repo 因為沒有 cgroup/task plugin 而留下的風險。

### GPU worker 的預設行為

**目前比較接近不能共享。**

只要 job 申請了 `gpu:a10:1` 或 `gpu:h100:1`，那個 GPU 資源就會被當成完整的一份配置掉。要讓多個 job 共用同一張 GPU，需要額外導入 sharing 機制，現在 repo 沒有做。

## 可行的開源方法 / 專案

### 1) CPU packing：Slurm 既有能力就能做到，但要補隔離

若目標是讓同一台 CPU worker 更有效率地跑多個 job，最直接的方向不是找外部專案，而是把現有 Slurm 設定補完整：

- 保留 `select/cons_tres`
- 讓 job 正確申請 CPU
- 補 `task/cgroup` / cgroup-based 限制
- 視需求加入 CPU binding / cpuset

Slurm 的 CPU management 文件就是沿著這條路設計的。citeturn998774search10turn832572search6

在你目前的 kind 模擬環境裡，這條路可做，但要注意：

- worker 本身只是 pod，底下是 Docker container
- kind node 也只是 Docker container
- 你做的 CPU 限制與 binding 會疊在 K8s / container runtime 的抽象之上

所以它可以用來驗證設計與控制流，但**不適合把效能數字當成正式結論**。

### 2) GPU sharing：NVIDIA time-slicing

如果你要讓多個 workload 共用同一張 GPU，最成熟的開源路線之一，是 NVIDIA GPU Operator / NVIDIA device plugin 的 **time-slicing**。官方文件明確說明 time-slicing 可以把單張 GPU 切成多個可分配的 sharing replicas，讓多個 pod 共用同一張實體 GPU。citeturn832572search2turn998774search11

優點：

- 開源且主流
- 與 Kubernetes 生態整合度高
- 對不支援 MIG 的 GPU 也可用

缺點：

- 沒有像 MIG 那樣的記憶體與 fault isolation
- 每個 pod 可以開很多 GPU process，行為不如整張獨占那麼可預測。NVIDIA 官方也特別提醒 time-slicing 是共享，不是硬切分。citeturn998774search2

### 3) GPU sharing：MIG

若硬體是 A100 / H100 這類支援 MIG 的 GPU，可以考慮 **MIG**。MIG 會把一張卡切成多個硬體隔離的 instance，Kubernetes 與 NVIDIA device plugin 也支援把 MIG instance 當成可分配資源。citeturn998774search6turn832572search14turn832572search20

優點：

- 隔離較好
- 資源切分較穩定

缺點：

- 依賴特定 GPU 型號
- 操作與資源型號管理更複雜

### 4) 研究型方案：MPS / 其他 GPU sharing 專案

除了 NVIDIA 官方路線，也有一些研究或社群方案，例如：

- CUDA MPS
- `nvshare` 這類研究型 GPU sharing 專案

這些方案有探索價值，但若你的目標是讓 repo 演進成較可信的工程原型，**優先順序仍然應該是 NVIDIA 官方 device plugin / GPU Operator 的 sharing 能力**，因為整合 Kubernetes 的摩擦最小。`nvshare` 這類專案比較像研究參考，不是你現在這份專案最應先整合的基線。citeturn998774search15turn998774search12

## 針對這份 repo 的建議

### 短期

1. **CPU worker**
   - 保持一個 worker pod 可承載多個 Slurm job 的方向。
   - 明確要求 job script 寫 CPU 需求。
   - 補一組 smoke test，驗證在 `CPUs=4` 的單 worker 上，四個各請求 1 CPU 的 job 可以同時被排進去。

2. **GPU worker**
   - 先把目前模型定義清楚為「單卡獨占」。
   - 不要在文件裡暗示現在已經支援 GPU sharing。

### 中期

1. CPU 端補強
   - 研究把 `TaskPlugin` 從 `task/none` 改成 cgroup 相關路徑。
   - 補 CPU binding / cpuset 驗證。
   - 在 kind 中做功能驗證，在實機節點再做效能驗證。

2. GPU 端共享
   - 若只是模擬控制流程，可先在文件設計層寫 time-slicing / MIG integration plan。
   - 若真要做功能原型，優先選 NVIDIA GPU Operator time-slicing。

## 最後的判斷

- **CPU worker：目前在排程語意上可以做到多 job packing。**
- **CPU worker：目前在隔離與可預測性上還不夠完整。**
- **GPU worker：目前不應視為可共享；預設應視為單 job 獨占 GPU。**
- **在 kind 裡，這些驗證應被視為控制流與資源語意驗證，不應過度解讀成真實裸機效能結論。**

---

## Phase 3 — 共享儲存與 sbatch 輸出調查（2026-03-28）

### 背景問題

確認 Phase 1/2 完成後，使用者是否可以從 Login Pod 提交 `sbatch` 並取回輸出檔案（`*.out`、`*.err`）？

### Phase 1/2 的限制分析

`slurm-login.yaml`（Phase 1/2 版本）的 volumeMounts 只有：

```
/etc/slurm           ← ConfigMap（唯讀）
/slurm-secrets       ← Secret（唯讀）
/opt/slurm-runtime-src ← ConfigMap（唯讀）
```

Worker pods 同樣**沒有任何共享可寫 Volume**。

Slurm 預設將 `slurm-<JOBID>.out` 寫到 `$SLURM_SUBMIT_DIR`，但 job 是在 worker pod 上執行的，worker 的本機 ephemeral 檔案系統與 login pod 完全隔離。因此：

- `srun`（互動式）：stdout 透過 Slurm I/O forwarding 回傳，**可用**。
- `sbatch`：job 成功提交與執行，但輸出檔案寫在 worker 的本機磁碟，login pod **看不到**。

verify-dev.sh 的 smoke test 刻意使用 `sleep N` job，迴避了這個問題。

### Phase 3 實作現況

Phase 3 **已完成實作**，並非只有設計：

| 檔案 | 內容 |
|------|------|
| `phase3/scripts/setup-nfs-server.sh` | 在 Windows 11 主機上建立 NFS Server（`/srv/nfs/k8s`） |
| `phase3/manifests/nfs-subdir-provisioner.tmpl.yaml` | NFS subdir external provisioner Deployment（需替換 `__NFS_SERVER__` / `__NFS_PATH__`） |
| `phase3/manifests/shared-storage.yaml` | StorageClass `slurm-shared-nfs` + PVC `slurm-shared-rwx`（20Gi RWX） |
| `phase3/scripts/bootstrap-phase3.sh` | 部署 provisioner → 建立 PVC → patch controller/worker/login 加入 `/shared` mount |
| `phase3/scripts/verify-phase3.sh` | 驗證 PVC Bound + `/shared` 掛載在所有 pod 上 |
| `phase3/scripts/verify-phase3-e2e.sh` | **完整 e2e 測試**：login 提交 `sbatch -o /shared/out-%j.txt`，等待完成，從 login 讀回輸出驗證 |

Phase 3 部署後，`/shared` 以 ReadWriteMany 方式同時掛載到：
- `slurm-controller-0`
- `slurm-worker-0`（以及所有副本）
- `slurm-login`

使用者只需在 job script 加入：

```bash
#SBATCH --output=/shared/out-%j.txt
#SBATCH --error=/shared/err-%j.txt
```

job 完成後即可在 login pod 的 `/shared/` 直接讀取輸出。

### 已知的 Phase 2 + Phase 3 整合缺口

`bootstrap-phase3.sh` 目前只 patch Phase 1 的單一 worker StatefulSet：

```bash
ensure_mount statefulset slurm-controller  slurm-controller  ...
ensure_mount statefulset slurm-worker      slurm-worker      ...   ← Phase 1 名稱
```

Phase 2 引入多節點池後，worker StatefulSet 名稱已改為：
- `slurm-worker-cpu`
- `slurm-worker-gpu-a10`
- `slurm-worker-gpu-h100`

所以 **Phase 3 bootstrap 跑完後，GPU worker pods 不會掛載 `/shared`**，提交到 GPU pool 的 job 無法寫輸出到共享路徑。

另外，`phase3/manifests/shared-storage.yaml` 重新定義了 `slurm-login` Deployment，但遺漏了 Phase 2-E 加入的 `slurm-ddp-runtime` volume mount。若先部署 Phase 2-E 再部署 Phase 3，login pod 會失去 DDP runtime 環境變數。

### Phase 3 後續計畫

#### 修正：bootstrap-phase3.sh 補齊多節點池

在 `ensure_mount` 呼叫後補上 Phase 2 的三個 pool：

```bash
ensure_mount statefulset slurm-worker-cpu     slurm-worker  shared-storage slurm-shared-rwx /shared
ensure_mount statefulset slurm-worker-gpu-a10 slurm-worker  shared-storage slurm-shared-rwx /shared
ensure_mount statefulset slurm-worker-gpu-h100 slurm-worker shared-storage slurm-shared-rwx /shared
```

（需先確認每個 StatefulSet 的 container name）

#### 修正：shared-storage.yaml 保留 DDP runtime volume

`slurm-login` Deployment 的 volumes 應補回：

```yaml
- name: slurm-ddp-runtime
  configMap:
    name: slurm-ddp-runtime
    defaultMode: 0755
```

以及對應 `volumeMounts`。

或者改為：`bootstrap-phase3.sh` 不重新 apply 完整 Deployment YAML，改用 `ensure_mount` 的 JSON patch 方式把 shared-storage volume 加進現有的 login deployment，避免覆蓋 Phase 2 已有的設定。

#### verify-phase3-e2e.sh 補齊多節點池驗證

目前 e2e 測試使用 `WORKER_STS=slurm-worker`（預設），需要加入對 `slurm-worker-cpu` 的顯式支援。在 Phase 2+3 整合後，驗證腳本應傳入：

```bash
WORKER_STS=slurm-worker-cpu bash phase3/scripts/verify-phase3-e2e.sh
```

#### 部署順序建議

在 Phase 2 + Phase 3 同時啟用時，建議部署順序為：

```
1. bash scripts/bootstrap-dev.sh          # Phase 1 + Phase 2
2. sudo bash phase3/scripts/setup-nfs-server.sh  # 主機端 NFS（一次性）
3. NFS_SERVER=<ip> bash phase3/scripts/bootstrap-phase3.sh
4. bash phase3/scripts/verify-phase3.sh
5. WORKER_STS=slurm-worker-cpu bash phase3/scripts/verify-phase3-e2e.sh
```

---

## CPU / GPU 資源分配與多 Job 共用調查（2026-03-28）

### 背景問題

1. Job 是否能指定要用多少 CPU 和 GPU？
2. 一台 worker 的 CPU cores 是否能同時給兩個不同的 job 使用？
3. GPU 是否能被多個 job 共用？

### 相關設定（slurm-static.yaml / slurm.conf）

```
SelectType=select/cons_tres
SelectTypeParameters=CR_Core
TaskPlugin=task/none
GresTypes=gpu
```

每台 worker 節點宣告：
```
CPUs=4  Sockets=1  CoresPerSocket=2  ThreadsPerCore=2  RealMemory=3500
```

GPU worker 額外宣告（gres.conf）：
```
Gres=gpu:a10:1   # A10 pool，每台 1 張
Gres=gpu:h100:1  # H100 pool，每台 1 張
File=/dev/null   # Kind 環境模擬，無真實硬體
```

### Job 指定資源的方式

標準 Slurm 旗標均支援：

```bash
# CPU
#SBATCH --cpus-per-task=2   # 每個 task 要 2 cores
#SBATCH --ntasks=4          # 4 個 task（共 8 cores）

# GPU
#SBATCH --gres=gpu:a10:1    # 要 1 張 A10
#SBATCH --constraint=gpu-a10
```

### CPU 多 Job 共用分析

`select/cons_tres` + `SelectTypeParameters=CR_Core` 啟用 **Consumable Resources 模式**，Slurm 以 core 為單位追蹤每台節點的資源消耗。每台 CPU worker 有 4 個可分配 CPU slot，可同時排入多個 job：

| Job A | Job B | 同一 worker 可行？ |
|-------|-------|------------------|
| `--cpus-per-task=2` | `--cpus-per-task=2` | ✅ 各佔 2 cores，合計 4 |
| `--cpus-per-task=3` | `--cpus-per-task=2` | ❌ 超過 4 cores，排到不同 worker |
| `--cpus-per-task=4` | 任意 | ❌ 整台 worker 被佔滿 |

**結論：排程語意層面可以做到 CPU packing（多 job 共用同一 worker）。**

### GPU 多 Job 共用分析

每台 GPU worker 只宣告 `Gres=gpu:a10:1`（1 張）。GRES 是整數消耗，無分數分配。

- Job A 請求 `--gres=gpu:a10:1` → 佔用整張 GPU
- Job B 同樣請求 → 必須等 A 結束，或排到另一台 worker

**結論：GPU 不支援多 Job 共用，每台 worker 同時只能跑一個 GPU job。**

### 重要限制：TaskPlugin=task/none

`TaskPlugin=task/none` 代表 Slurm 只在**排程計算層面**追蹤 core 數量，但不執行任何 CPU binding 或 cgroup 隔離。兩個 job 被排到同一 worker 後，OS 層面的 process 可跑在任意 CPU 上，沒有強制 pinning。

在真實 HPC 環境通常改為 `TaskPlugin=task/cgroup` 來強制隔離。但在本 Kind/Docker 環境中，cgroup 設定會疊加在 K8s / container runtime 的抽象之上，只適合驗證排程控制流，不適合測試效能隔離。

### 調查結論

| 問題 | 結果 |
|------|------|
| Job 能指定 CPU 數量？ | ✅ `--cpus-per-task`、`--ntasks` |
| Job 能指定 GPU 數量？ | ✅ `--gres=gpu:a10:1` |
| 同一 worker CPU 能讓兩個 job 共用？ | ✅ 排程層面可以（`CR_Core` consumable） |
| 是否有 CPU 實體隔離？ | ❌ `TaskPlugin=task/none`，無 binding/cgroup |
| 同一 worker GPU 能讓兩個 job 共用？ | ❌ 每台只有 1 GPU，整數消耗 |
| GPU GRES 是真實硬體？ | ❌ `File=/dev/null`，Kind 環境純排程模擬 |

---

## Operator 與部署流程改進（2026-03-28）

本輪針對程式碼品質與可靠性進行三項改進：

### 1. 消除 N+1 kubectl exec（`phase2/operator/main.py`）

**問題：** 每次 poll 週期，`collect_partition_state()` 對每個 pool 分兩次呼叫 `_jobs_by_pool()`（PENDING / RUNNING），每次先用 `squeue -o %i` 取 job ID 清單，再對每個 job 呼叫一次 `scontrol show job -o <id>`。若有 N 個 pending + M 個 running job，三個 pool 共會產生：

```
6 squeue 呼叫 + (N+M) × scontrol 呼叫
```

**修正：**

- 新增 `_jobs_by_pool_and_state(partition)`：一次 `squeue -h -t PENDING,RUNNING -o '%i|%T|%N|%f|%b'`，在 Python 端解析並按 pool 分類。
- 新增 `collect_all_partition_states()`：將相同 partition 的多個 pool（三個 pool 都用 `debug`）合併為一次 squeue 呼叫。
- `OperatorApp.run()` 改為呼叫 `collect_all_partition_states()` 取代逐個 `collect_partition_state()`。

每個 poll 週期的 exec 呼叫數對比：

| | 修改前 | 修改後 |
|--|--|--|
| squeue | 6 | 1 |
| scontrol show job | N+M（隨 job 數線性增長） | 0 |
| sinfo | 3 | 3 |
| statefulset get | 3 | 3 |
| **合計** | **12 + N + M** | **7（固定）** |

移除的方法：`_job_ids()`、`_jobs_by_pool()`、`get_pending_jobs()`、`get_running_jobs()`。

### 2. Operator Liveness Probe（`phase2/operator/main.py` + `phase2/manifests/slurm-phase2-operator.yaml`）

**問題：** Operator Deployment 沒有 liveness probe，若 polling loop 卡死（如 kubectl exec 無限 hang），K8s 不會自動重啟。

**修正：**

- `main.py`：每次 poll loop 全部完成後執行 `pathlib.Path("/tmp/operator-alive").touch()`（heartbeat）。
- `slurm-phase2-operator.yaml`：加入 `livenessProbe`，檢查 heartbeat file 是否在 120 秒內有更新（= 8 個 poll 週期的緩衝）。
  ```yaml
  livenessProbe:
    exec:
      command: ["/bin/sh", "-c", "test -f /tmp/operator-alive && test $(( $(date +%s) - $(stat -c %Y /tmp/operator-alive) )) -lt 120"]
    initialDelaySeconds: 60
    periodSeconds: 30
    failureThreshold: 3
  ```

### 3. Phase 3 NFS 補丁持久化（跨越多個檔案）

**問題：** 舊做法是由 `bootstrap-phase3.sh` 的 `ensure_mount()` 對已運行的 StatefulSet 做 JSON patch 加上 `/shared` volumeMount。若之後重新執行 `bootstrap-dev.sh`，它會重新 render `slurm-static.yaml`（不含 NFS），再 `kubectl apply` 蓋掉這些 patch，NFS mount 就消失。

**修正：**

**`phase1/scripts/render-slurm-static.py`：**
- 新增 `--with-shared-storage` flag（`argparse`）。
- 啟用時，controller 與所有 worker pool StatefulSet 的 template 中都會注入：
  - `volumeMounts: - name: shared-storage mountPath: /shared`
  - `volumes: - name: shared-storage persistentVolumeClaim: claimName: slurm-shared-rwx`
- 生成的 `slurm-static.yaml` 本身就包含 NFS volume，後續任何 `kubectl apply` 都保持一致。

**`scripts/bootstrap-dev.sh`：**
- render 前先檢查 PVC `slurm-shared-rwx` 是否存在：
  ```bash
  if kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx >/dev/null 2>&1; then
    render_flags+=(--with-shared-storage)
  fi
  ```
- 若 Phase 3 已部署，自動帶入 flag，確保 re-run 也不會遺失 NFS。

**`phase3/scripts/bootstrap-phase3.sh`：**
- 移除 `ensure_mount()` 函式與所有 `patch` 呼叫（共約 50 行）。
- 改為 PVC Bound 後直接執行：
  ```bash
  python3 phase1/scripts/render-slurm-static.py --with-shared-storage
  kubectl apply -f phase1/manifests/slurm-static.yaml
  ```
- `slurm-static.yaml` 磁碟上的版本更新為含 NFS 的版本，後續所有 bootstrap 都以此為準。

**持久化的完整流程：**

```
首次 Phase 3 bootstrap
  → render --with-shared-storage → 更新 slurm-static.yaml
  → kubectl apply → StatefulSets 含 /shared

重新執行 bootstrap-dev.sh（e.g. 更新 operator 後）
  → 偵測到 slurm-shared-rwx PVC 存在
  → render --with-shared-storage → 仍含 /shared
  → kubectl apply → NFS mount 不遺失
```

---

## Phase 3 實際部署踩坑紀錄（2026-03-29 on Windows 11 + WSL2 + Kind）

### 坑 1：NFS_SERVER 誤填網段而非 IP

**現象：** `bootstrap-phase3.sh` 傳入 `NFS_SERVER=172.16.0.0/12`，provisioner pod 卡在 `ContainerCreating`，timeout 300s 失敗。

**原因：** `172.16.0.0/12` 是 CIDR 網段，不是 IP 位址；provisioner Deployment 把它直接填入 `NFS_SERVER` env var，kubelet 嘗試掛載 `172.16.0.0/12:/srv/nfs/k8s` 當然失敗。

**修正：** 在 WSL2 執行 `ip addr show eth0 | grep 'inet '` 取得實際 IP（例如 `172.26.7.207`），傳入 `NFS_SERVER=172.26.7.207`。

### 坑 2：NFS exports 的 `secure` 選項拒絕 Kubernetes 掛載

**現象：** 用正確 IP 重試後，provisioner pod event 顯示：
```
MountVolume.SetUp failed for volume "nfs-client-root":
mount.nfs: access denied by server while mounting 172.26.7.207:/srv/nfs/k8s
```

**原因：** Linux NFS 預設選項 `secure`，要求客戶端從特權端口（< 1024）發起連線。Kubernetes kubelet 在容器環境（Kind/Docker）中執行 NFS mount 時使用非特權端口，因此被 NFS server 拒絕。

**修正：** 在 WSL2 的 `/etc/exports` 中加上 `insecure`：
```
/srv/nfs/k8s 172.16.0.0/12(rw,sync,no_subtree_check,no_root_squash,insecure)
```
然後重新載入：`sudo exportfs -arv`

### 確認 NFS 連通性（不依賴 `nc`）

Kind 容器內通常沒有 `nc`，改用 bash `/dev/tcp` 測試：
```bash
docker exec slurm-lab-control-plane bash -c \
  "timeout 3 bash -c 'echo >/dev/tcp/172.26.7.207/2049' && echo OK || echo FAIL"
```

### 完整 Phase 3 部署指令（WSL2 環境）

```bash
# 1. 在 WSL2 取得 IP
WSL2_IP=$(ip addr show eth0 | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)

# 2. 確認 /etc/exports 有 insecure 選項，重載
sudo exportfs -arv

# 3. 部署
NFS_SERVER=${WSL2_IP} NFS_PATH=/srv/nfs/k8s bash phase3/scripts/bootstrap-phase3.sh
```
