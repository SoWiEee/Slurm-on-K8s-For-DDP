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

# Development Notes (Phase 2-E Research: intra-node multi-job utilization)

## 問題

目前這個專案是否能在同一台 worker 上同時跑多個 job，藉此提高單機 CPU / GPU 利用率？如果不能，應該怎麼做？

## 目前 repo 的實際行為

先講結論：

- **CPU worker：可以，但不是無限制，也不是用 CPU thread 當最小單位。**
- **GPU worker：對 GPU job 來說，預設基本上不行共享同一張 GPU。**
- **目前配置比較像「CPU/核心可分配、GPU 整張卡獨占」的模式。**

### 1) CPU worker 的目前行為

目前 `slurm.conf` 使用：

- `SelectType=select/cons_tres`
- `SelectTypeParameters=CR_Core`

也就是 Slurm 不是用整台 node 當分配單位，而是用 **consumable resources** 方式配置。這代表 **同一台 node 可以同時承載多個 job**，只要它們使用的是不同的可分配 CPU 資源。官方文件對 `select/cons_tres` 的描述就是：個別 socket / core / thread 可以作為 consumable resource 被分配，而不是像 `select/linear` 一樣整台 node 一次配給一個 job。citeturn430109search4turn972856search14

但這個 repo 目前不是 `CR_CPU`，而是 `CR_Core`。這點非常重要。官方文件明確指出，`CR_Core` 的語意是：**core 是 consumable resource**；在有 SMT / hyperthreading 的節點上，thread 會被當成 CPU 計數，但 **不同 job 不會共享同一個 core 上的 threads**。分配給 job 的 CPU 數量會向上取整到完整 core。citeturn430109search1

你現在的 CPU worker 節點定義是：

- `CPUs=4`
- `Sockets=1`
- `CoresPerSocket=2`
- `ThreadsPerCore=2`

這表示它是 **2 個 core，每個 core 2 threads，共 4 CPUs**。在 `CR_Core` 下，實際上更接近「2 個可分配 core」，不是「4 個可獨立分享給 4 個 job 的 CPU thread」。因此：

- 如果每個 job 只需要 1 個 core 範圍的資源，**一台 CPU worker 預設可同時跑多個 job**。
- 但在你現在的拓撲下，**更接近最多 2 個輕量 job 同時吃滿 2 個 core**，不是 4 個 job 把 4 個 threads 完整拆開來用。
- 因為 partition 沒有設定 `OverSubscribe=FORCE`，同一個 core 不會被多個 job 重疊分配。官方文件對 `select/cons_tres` + `OverSubscribe` 的行為寫得很清楚：`OverSubscribe=NO` 時 CPU 不會被多個 job 共享；`OverSubscribe=YES` 也還要 job 明確允許 oversubscribe；`OverSubscribe=FORCE` 才會強制讓同一 CPU 被多個 job 共用。citeturn972856search0turn972856search10

### 2) 所以「一台 CPU worker total CPU=4，預設可以跑多個 job 嗎？」

**可以，但要精準描述。**

可以的原因：

- 目前不是 whole-node allocation。
- `select/cons_tres` 允許同 node 多 job，只要可分配資源還有剩。

限制在於：

- 目前是 `CR_Core`，不是 `CR_CPU`。
- 節點有 2 cores / 4 threads，所以分配粒度偏向 core。
- 預設沒有 `OverSubscribe=FORCE`，同一 core 不會被多個 job 疊上去。

所以如果你的期待是：

- 「一個 4-thread 的 CPU worker 預設能塞 4 個小 job」

那 **不一定**。以目前設定，更保守的理解是：

- 「同一台 CPU worker 可以同時跑多個 job，但分配粒度是 core，實際併發能力更像 2 個 core 的配置。」

### 3) GPU worker 的目前行為

目前 GPU node 定義長這樣：

- `Gres=gpu:a10:1` 或 `Gres=gpu:h100:1`

這代表每個 GPU worker 只有 **1 張完整 GPU** 暴露給 Slurm。現況沒有設定任何 shared GRES，例如：

- `gres/mps`
- `gres/shard`

官方 GRES 文件明確說明，GPU sharing 需要透過 **MPS** 或 **Sharding** 這類 shared GRES 機制；否則就是完整 GPU 當成一個不可分的 GRES。citeturn972856search1turn972856search15

因此對 **GPU job** 而言：

- 目前 **預設不能** 在同一張 GPU 上跑多個 job。
- 一個要求 `--gres=gpu:a10:1` 的 job 會吃掉該 node 上唯一的那張 GPU。
- 第二個 GPU job 會等，除非它排到別台有空 GPU 的 node。

至於 CPU 資源面，GPU worker 仍然有 4 CPUs，理論上 Slurm 的 CPU allocation 邏輯仍可以讓多個 job 共享同一台 GPU node 的 CPU cores。可是真正的瓶頸通常是 GPU GRES，而不是 CPU。也就是說：

- **GPU node 的 CPU 可以被切分**。
- **GPU 本身目前不能共享。**

## 如果要提高 CPU worker 單機利用率，現在有哪些做法？

### 方法 A：把 `CR_Core` 改成 `CR_CPU`

這是最直接、最貼近你問題的做法。

如果你想讓 4-thread 的 CPU worker 更接近「4 個最小 CPU 單位可分配」，應考慮改成：

- `SelectTypeParameters=CR_CPU`

官方文件指出，在 `select/cons_tres` 下可以用 CPU 作為 consumable resource。`CR_CPU` / `CR_CPU_Memory` 會以 CPU 作為配置單位，而不是 core。citeturn972856search0turn430109search4

這樣的效果是：

- 更細粒度地塞多個小 job 到同一台 worker
- 更容易提高 CPU thread 層級的利用率

風險與代價：

- SMT siblings 會被不同 job 分到同一 physical core，互相干擾更明顯
- 對 DDP / MPI / latency-sensitive workload 可能更差
- 記憶體配置最好同步改成 `CR_CPU_Memory`，不然只細切 CPU 而不細切記憶體，容易出現 CPU 看似還有、實際 memory pressure 很高的問題。官方也建議在這種情況下設定 `DefMemPerCPU`。citeturn430109search1

### 方法 B：保留 `CR_Core`，但讓使用者明確提交小資源 job

如果你不想改 Slurm 分配粒度，另一條路是：

- 保持 `CR_Core`
- 要求使用者提交更小的 CPU 需求，例如 `--cpus-per-task=1`
- 搭配合理的 `--ntasks` / `--ntasks-per-node`

這樣仍可以在單 node 上共存多個 job，只是上限仍受 core 粒度限制。

優點：

- 改動小
- 比較不容易把 SMT 壓榨到不可控

缺點：

- 單機利用率上限沒有 `CR_CPU` 高

### 方法 C：啟用 `OverSubscribe`

官方文件明確說，`OverSubscribe=FORCE[:count]` 可以讓同一 CPU 被多個 job 共用。citeturn972856search0turn972856search10

這可以進一步提高表面利用率，但我不建議把它當第一選項。原因很直接：

- 這不是切得更細，而是直接讓多個 job 疊在同一資源上
- 對 batch 吞吐量有時有幫助，但對效能穩定性通常有害
- 對 DDP / MPI / 訓練工作負載通常不友善

如果你的目標是「多個輕量 CPU serial jobs 塞同一台 worker」，可以研究；如果你的目標是「穩定的 AI / HPC 執行」，這通常不是最先該開的開關。

## 如果要提高 GPU worker 單機利用率，有哪些開源做法？

### 方法 1：Slurm 的 shared GRES，使用 `gres/mps`

Slurm 官方提供 `gres/mps`，對 NVIDIA CUDA MPS 做 GPU sharing。MPS 會透過控制 daemon 來管理 shared access。`gres.conf` 文件也說明了 MPS 的 Count 通常是 100 或其倍數，用來表達共享配額。citeturn972856search15turn430109search6

適合情境：

- 同質、可合作共享 GPU 的 CUDA 工作負載
- 你想維持 Slurm 為單一權威排程器

限制：

- 需要額外配置 MPS daemon 與 shared GRES
- 隔離性沒有 MIG 強
- 對混雜 workload 風險較高

### 方法 2：Slurm 的 `gres/shard`

Slurm 官方文件把 sharding 定義成一種 generic GPU sharing 機制，允許多個 job 共用同一 GPU，但不會真正 fence 掉彼此進程，官方也明說它比較適合同質 workflow。citeturn972856search1turn972856search9

適合情境：

- 想在 Slurm 內做較輕量的 GPU sharing 概念驗證
- 同質工作、彼此可接受干擾

限制：

- 缺少強隔離
- 不太適合多租戶環境

### 方法 3：NVIDIA MIG

對支援 MIG 的 GPU，NVIDIA 官方 GPU Operator 與 MIG 文件都支援把一張 GPU 切成多個隔離的 instance。MIG 提供比 time-slicing / MPS 更強的隔離。citeturn366067search11turn366067search18

適合情境：

- A100 / H100 這類支援 MIG 的卡
- 想做比較嚴格的 GPU 分區與隔離

限制：

- 只支援特定 GPU
- 需要切 MIG profile
- 對 repo 現況改動較大

### 方法 4：Kubernetes / NVIDIA device plugin 的 GPU time-slicing 或 MPS

如果未來你想讓「GPU sharing」落在 Kubernetes 層而不是 Slurm 層，NVIDIA GPU Operator 與 k8s-device-plugin 都支援 **time-slicing**，也支援 MPS。官方文件指出 time-slicing 可以把一張 GPU 複製成多個可分配的 replicas，但沒有 memory/fault isolation；MPS 則是更明確的資源分割與控制。citeturn366067search2turn366067search5

優點：

- 與 K8s 生態更整合
- 若未來 worker pool 更偏原生 K8s 資源管理，擴充性較好

缺點：

- 會產生「K8s 與 Slurm 誰才是 GPU sharing 真正權威」的協調問題
- 你目前這個 repo 還是以 Slurm 為主要 batch scheduler，直接把 sharing 下放到 K8s，架構邏輯會變複雜

## 有哪些開源專案可參考？

### 1) Slurm 官方機制本身

最值得優先參考的其實不是外部專案，而是 Slurm 官方的：

- `select/cons_tres`
- `OverSubscribe`
- `gres/mps`
- `gres/shard`

原因是你這個專案本來就以 Slurm 為主要排程器，直接沿官方資源模型擴展，改動最小，也最一致。citeturn972856search0turn972856search1turn430109search4

### 2) NVIDIA k8s-device-plugin / GPU Operator

如果你要研究 K8s 層 GPU sharing，這兩個是最成熟、最該看的開源路線：

- NVIDIA `k8s-device-plugin`
- NVIDIA `gpu-operator`

它們已經支援 time-slicing、MPS、MIG 等能力。citeturn366067search2turn366067search5turn366067search11

### 3) Volcano

如果未來你想走更「K8s-native batch scheduling」方向，而不是 Slurm-first，Volcano 是常見開源選項。不過它比較像 batch scheduler / queueing / gang scheduling 的替代路線，不是直接幫 Slurm worker 做 CPU/GPU intra-node sharing 的解法。citeturn972856search2

## 建議路線

### 短期

如果你的目標是 **提高 CPU worker 單機利用率**，最務實的順序是：

1. 先在文件裡承認目前 `CR_Core` 的現實含義：可多 job 同 node，但粒度是 core。  
2. 做一個小實驗，把 `SelectTypeParameters=CR_CPU_Memory` 跑一版對照。  
3. 觀察同一台 4-thread worker 能否穩定塞更多 serial jobs。  
4. 同時補 `DefMemPerCPU`，不要只切 CPU 不管記憶體。  

### 中期

如果你的目標是 **提高 GPU worker 利用率**，最合理的路線是二選一：

- 想保持 Slurm-first：研究 `gres/mps` 或 `gres/shard`
- 想走 K8s-native GPU sharing：研究 NVIDIA device plugin / GPU Operator 的 time-slicing 或 MIG

### 不建議先做的事

- 一開始就開 `OverSubscribe=FORCE`
- 一開始就把 CPU / GPU sharing 都丟給 K8s 層，卻還維持 Slurm 當主排程器
- 在沒有 accounting / isolation 策略前，就宣稱 GPU worker 可安全多租戶共享

## 最終判斷

### CPU worker

- **目前就可以同 node 跑多個 job。**
- 但因為是 `CR_Core`，實際粒度偏向 core，不是完整 thread-level packing。
- 所以你要的「提高每台 worker 硬體資源利用率」有一部分已經存在，但還沒做到最激進。

### GPU worker

- **對 GPU job 而言，目前預設不行共享同一張 GPU。**
- 若要做到，需要顯式引入 GPU sharing 機制，例如 Slurm `gres/mps`、`gres/shard`、NVIDIA time-slicing 或 MIG。 

