# Development Notes

這份筆記保留原本的階段紀錄，以及這一輪開發實際踩到的坑。

---

# Phase 1

- 建立 Slurm Controller / Worker 映像。
- 在 Kind 部署靜態 Slurm 叢集。
- 讓 Pod 間具備 SSH 互通與 Munge 認證。

## Debug Record

### 問題 1：Secret volume 唯讀，不能直接 chmod

觀察到：
```
chmod: changing permissions of '/etc/munge/munge.key': Read-only file system
```
原因在於 K8s Secret mount 是唯讀。

修正方式：

- 改掛到 `/slurm-secrets/munge.key`。
- 啟動時複製到 `/etc/munge/munge.key` 後再 `chown/chmod`。

### 問題 2：`SlurmctldHost` / DNS 解析錯誤

觀察到：
```
This host ... not a valid controller
NO NETWORK ADDRESS FOUND
```

修正方式：

- 在 `slurm.conf` 明確設定 `NodeAddr` / `NodeHostname`。
- controller 改用 `slurm-controller-0(slurm-controller-0....svc.cluster.local)`。

---

# Phase 2

- 開發 Python Operator，實作 `Pending Job -> Scale Up`。
- 實作 `Idle Node -> Scale Down`。
- 讓整個 Phase 1 + Phase 2 能以 `bootstrap.sh` / `verify.sh` 穩定驗證。
- (2-B) 加入結構化日誌，讓 autoscaling 行為可追蹤、可分析、可做報告。
- (2-C) 把單一 worker pool 擴展成 multi-pool / partition-aware autoscaling。
- (2-D) 加入 checkpoint-aware scale-down guard，避免正在跑的工作因過早縮容而丟失恢復點。
- (2-E) 在單一叢集引入兩個子網路，分成 management subnet 和 data subnet。
  - Slurm 控制流量維持單純
  - 之後若要做 PyTorch DDP / MPI / NCCL，能逐步把高流量傳輸導向 `data subnet`
  - verify 時也能清楚展示哪些元件是 control plane、哪些元件是 dual-homed compute plane

## Debug Record

### 問題 1：`duplicate partition in config: debug`

觀察到 operator 啟動時直接 `ValueError: duplicate partition in config: debug`

原因是：原本 validation 把「partition 名稱重複」當成非法，但現在多 pool 共享同一個 Slurm partition 是設計需求，不是錯誤。

修正方法：

- validation 不能用 partition name 當唯一鍵。
- 要接受「同一 partition 對應多個 worker pool」。

# Phase 3

- 在 Kind 單機環境部署 NFS Server 並整合 `nfs-subdir-external-provisioner`。
- 建立 StorageClass + RWX PVC。
- 將 Controller / Worker / Login 掛載共享儲存。
- 將 Phase 2-D 的 checkpoint-aware guard 與真實 workload 串起來。

## Debug Record

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

verify.sh 的 smoke test 刻意使用 `sleep N` job，迴避了這個問題。

### Phase 3 實作現況

Phase 3 **已完成實作**，並非只有設計：

| 檔案 | 內容 |
|------|------|
| `scripts/setup-nfs-server.sh` | 在 Windows 11 主機上建立 NFS Server（`/srv/nfs/k8s`） |
| `manifests/storage/nfs-subdir-provisioner.tmpl.yaml` | NFS subdir external provisioner Deployment（需替換 `__NFS_SERVER__` / `__NFS_PATH__`） |
| `manifests/storage/shared-storage.yaml` | StorageClass `slurm-shared-nfs` + PVC `slurm-shared-rwx`（20Gi RWX） |
| `scripts/bootstrap-storage.sh` | 部署 provisioner → 建立 PVC → patch controller/worker/login 加入 `/shared` mount |
| `scripts/verify-storage.sh` | 驗證 PVC Bound + `/shared` 掛載在所有 pod 上 |
| `scripts/verify-storage-e2e.sh` | **完整 e2e 測試**：operator scale-up → login 提交多節點 `sbatch` → 等待 job COMPLETING → 從 login 讀回 `/shared/` 輸出驗證 |

Phase 3 部署後，`/shared` 以 ReadWriteMany 方式同時掛載到：
- `slurm-controller-0`
- `slurm-worker-cpu-*`（以及所有副本）
- `slurm-login`

使用者只需在 job script 加入：

```bash
#SBATCH --output=/shared/out-%j.txt
#SBATCH --error=/shared/err-%j.txt
```

job 完成後即可在 login pod 的 `/shared/` 直接讀取輸出（因共享 NFS，任何 pod 均可讀）。

**E2E 測試說明：** `verify-storage-e2e.sh` 驗證的是多節點場景下的完整生命週期：
1. 暫停 operator → 提交帶 `--hold` 的觸發 job（確保 cpu-1 不立即被搶）
2. 恢復 operator → scale-up 到 2 台 worker
3. 等待 cpu-1 變 `idle`（含 fix_node_addr 修正 IP cache + SIGHUP fallback）
4. 取消 trigger job → 提交真實 2 節點 smoke job（operator 繼續暫停，避免縮容干擾）
5. 等待 job 完成，從 login pod 讀取 `/shared/` 輸出驗證每台 worker 的 hostname

### 部署順序建議

在 Phase 2 + Phase 3 同時啟用時，建議部署順序為：

```
1. bash scripts/bootstrap.sh              # Phase 1 + Phase 2
2. sudo bash scripts/setup-nfs-server.sh  # 主機端 NFS（一次性，在 WSL2 執行）
3. NFS_SERVER=<wsl2-ip> bash scripts/bootstrap-storage.sh
4. bash scripts/verify-storage.sh
5. bash scripts/verify-storage-e2e.sh    # WORKER_STS 預設 slurm-worker-cpu
```

---

# Phase 4 (可觀測性)

## Prometheus + Grafana 監控（已完成）

詳細規格見 `docs/monitoring.md`。核心是三層 metrics：

| 來源 | 取得方式 | 關鍵指標 |
|------|---------|---------|
| slurm-exporter | scrape slurmrestd（REST API） | queue_pending, nodes_idle, nodes_alloc |
| kube-state-metrics | K8s 原生 | StatefulSet replicas, Pod ready |
| operator 自定義 | prometheus_client HTTP server（port 8000） | scale_up/down_total, guard_blocks, poll_duration |

Phase 4 已部署 Prometheus + Grafana + slurm-exporter + kube-state-metrics。Grafana 提供三個看板：
- **Bridge Overview**：視覺化 Slurm queue depth 與 K8s StatefulSet replicas 的聯動關係
- **Slurm Cluster State**：node states 圓餅圖、各 partition queue depth 時序
- **K8s Operator**：scale event timeline、poll duration histogram、guard block 計數

**部署：** `bash scripts/bootstrap-monitoring.sh`  
**驗證：** `bash scripts/verify-monitoring.sh`

**參考來源：** `github.com/SlinkyProject/slurm-exporter`（REST API 驅動）、`github.com/vpenso/prometheus-slurm-exporter`（exec 驅動，較易入門）

---

## 相關開源專案對照

| 改進項目 | 主要參考來源 | 備註 |
|---------|------------|------|
| REST API | SlinkyProject/slurm-exporter | SchedMD 官方，生產驗證 |
| SIGTERM handler | pytorch/elastic | torchelastic 官方模式 |
| Pre-check | Character.ai Slonk | blog.character.ai/slonk |
| Scale-down 安全性 | nebius/soperator | Go+Kubebuilder，架構參考 |
| sbatch + torchrun 整合 | aws/aws-parallelcluster | 業界驗證的環境變數設定 |
| Prometheus monitoring | vpenso/prometheus-slurm-exporter | exec 版本，入門用 |
| Prometheus monitoring | SlinkyProject/slurm-exporter | REST API 版本，演進目標 |

## Debug Record

### A. verify-monitoring.sh 在無 wget/curl 的 image 裡 exec 失敗

**症狀：**

verify script 對 kube-state-metrics 和 slurm-exporter 的 metrics 檢查全部 FAIL，但 Prometheus 上這兩個 target 都是 UP。

**根因（三個獨立問題）：**

1. **distroless image 無 shell/wget/curl**：`check_metrics_endpoint` 用 `kubectl exec pod -- wget ...` 的方式，但 kube-state-metrics 使用的 image 沒有這些工具，exec 直接失敗。

2. **python:3.11-slim 無 wget/curl**：slurm-exporter image 同樣無 curl/wget。

3. **Windows 環境無 `python3` 指令**：Prometheus target 解析原本用 `python3 -c "..."` 做 JSON 解析，Windows 下指令應為 `py`，導致腳本報錯誤而非解析失敗，造成誤判。

**修正：**

改用 **port-forward + host-side curl** 的方式取代 exec：
- 每個 metrics 檢查改為：啟動 `kubectl port-forward`，等待 3 秒，用 host 上的 `curl` 抓 `/metrics`，直接 pipe 給 `grep`，完成後 kill port-forward。
- Prometheus targets 解析從 `python3` JSON 解析改為 `grep -A5 ... | grep '"health":"up"'`，無需任何 Python。
- 同時修正 kube-state-metrics 的 metric 名稱：實際名稱為 `kube_statefulset_status_replicas`（非 `kube_statefulset_replicas`）。

---

# Phase 5 技術規劃

Phase 5 的目標是讓這套系統從「可運作的基礎設施原型」演進成「使用者能直接提交各種 AI 批次工作的運算平台」。

目前以**單一使用者**情境為主，系統核心優先做到：**部署可重複 → job 生命週期可視化 → 工作負載開箱即用 → 真實 SSH 登入**。多租戶（Fair-Share 帳號配額）留待後期疊加，不影響前四項。

開發順序：**Helm（5-A）→ OpenTelemetry（5-B）→ 工作負載模板（5-C）→ SSH Login（5-D）**

---

## 5-A：Helm Chart 封裝

> **修訂版 3（2026-04-28，Stage D 中）：** 嘗試把 `gpu-operator` 加成 chart dependency 後發現它把所有 DaemonSet hardcode 在 `Release.Namespace`（沒有 namespaceOverride 機制），且需要該 namespace PSS=`privileged` 才能 mount hostPath（`/dev/nvidia*`、`/run/nvidia/mps`、driver libs）。我們的 slurm namespace 走 PSS=`baseline`（NetworkPolicy + secret projection 都依賴此），兩者放同一個 namespace 不乾淨——dropping 到 privileged 會放鬆 slurm pod 的整體安全姿態。
>
> **改採分離安裝**：`gpu-operator` 不再是 subchart，由 `scripts/install-gpu-operator.sh` 獨立 `helm install` 到自己的 `gpu-operator` namespace（PSS=privileged）。本 chart 只負責放 `device-plugin-config` ConfigMap 進該 namespace + cluster-wide 的 node-labeler Job。`Chart.yaml` 移除 dependencies block；`charts/`、`Chart.lock`、`*.tgz` 都不進 git。部署流程從「一條 helm install」變成「一條 setup-linux-gpu.sh + 一條 install-gpu-operator.sh + 一條 helm install slurm-platform」。
>
> **修訂版 2（2026-04-28）：** Linux+k3s+RTX4070 路徑驗證後（commit `3eec54f`），確認 `nvidia-device-plugin` 內建 `sharing.mps` 在 v0.15–v0.17.x 全系列因 upstream `cmd.Exec("nvidia-cuda-mps-control", "-d")` daemonize spawn race 而無法啟動（見 [`docs/migration.md`](migration.md)）。本版改為以 GPU Operator 為 GPU 子系統的目標方案——它把 MPS daemon 拆成獨立 `mps-control-daemon` DaemonSet 用前景模式跑，繞過 spawn race。〔修訂版 3 把它從 dependency 改成獨立安裝。〕
>
> **修訂版 1（2026-04-27）：** 本節原稿寫於 N1 / N7 修復前，`mps.enabled` flag、`partition: debug`、rtx4080 `devicePath: /dev/nvidia1` 已隨 `mps-migration` 分支上線而過時。先對齊：sharing.mps（N1 / N10）、三 partition 拆分（N7）、`/dev/nvidia0` 一律（N2）、`AccountingStorageTRES`（N6）、namespace PSS=baseline（N9）、k3s `ctr images import`（N4）、NetworkPolicy 6443（N5）。

### 問題
目前部署流程是「依序執行多支 bootstrap 腳本，每支腳本依賴前一支的副作用」：
- 環境差異（k3s vs. Kind、`REAL_GPU=true/false`）靠環境變數控制，容易漏設。
- `worker-pools.json` 改完還需要手動跑 `render-core.py`，manifest 與設定雙重維護。
- 無法用 ArgoCD / Flux 做 GitOps、無版本 rollback、無 dry-run diff。
- N1 之後還多了「需要記得對 GPU 節點打 `nvidia.com/device-plugin.config` label」這一步驟，若忘記則 MPS 默默失效。
- 自寫的 `manifests/gpu/nvidia-device-plugin.yaml` 內建 MPS 已知壞掉（migration 階段 fallback 到 time-slicing），長期需要換 GPU Operator 才能拿回 `--gres=mps:N` SM 配額；Phase 5-A 是合併兩件事的最佳時機。

### 設計方向

**Monolithic chart for slurm 子系統 + NVIDIA GPU Operator 獨立安裝 + slurm.conf 拆兩個 ConfigMap。**

- 主體不拆 subchart；monitoring / storage / gpu 用 `enabled` flag 控制。
- `render-core.py` 廢棄，`slurm.conf` / `gres.conf` 改由 `_helpers.tpl` 從 `values.yaml` 的 `pools` 列表產生。
- **GPU 子系統用 NVIDIA GPU Operator，但獨立安裝（不是 chart dependency）**——見上面修訂版 3 banner。本 chart 只在 `gpu-operator` namespace 放 device-plugin-config ConfigMap + 一個 cluster-wide 的 node-labeler Job 把 GPU 節點標上 `nvidia.com/device-plugin.config=<key>`。GPU Operator 由 `scripts/install-gpu-operator.sh` 用 `helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace --set driver.enabled=false --set toolkit.enabled=false` 裝進自己的 PSS=privileged namespace。Operator 內建的 `mps-control-daemon` DaemonSet 解決 v0.15–v0.17.x device-plugin 內建 MPS 的 spawn race。
- **GPU Operator 的 driver / toolkit 子模組關掉**：host 已用 `apt install nvidia-driver-535` + `nvidia-container-toolkit` 裝好，重複裝會撞。Operator 只負責 device-plugin、MPS daemon、DCGM exporter（可選）、gpu-feature-discovery、node-feature-discovery。
- 自寫的 `manifests/gpu/nvidia-device-plugin.yaml` + `manifests/gpu/mps-daemonset.yaml` 廢棄。
- `slurm.conf` ConfigMap 拆成 **`slurm-config-static`**（ClusterName / Auth / Plugin / AccountingStorageTRES，幾乎不變）+ **`slurm-config-nodes`**（NodeName / PartitionName，每次 pool 變動都重產）。worker 只 mount 後者 → 改一個 pool 的 `maxReplicas` 不會 rolling restart 全部 worker。
- secret（munge.key / slurm-jwt-key）**不由 chart 產生**，install 前要先跑 `scripts/create-secrets.sh`（chart 用 `helm.sh/hook-pre-install` 檢查存在性即可）。

### Chart 目錄結構

```
chart/
  Chart.yaml                ← appVersion = Slurm 版本（如 23.11.7）；無 dependencies
  values.yaml               ← 預設值（Kind 開發環境基準）
  values-dev.yaml           ← Kind override（無 GPU，File=/dev/null）
  values-k3s.yaml           ← k3s override（real GPU、namespace baseline label）
  templates/
    _helpers.tpl            ← label 函數 + slurmConf / gresConf / partitionsJson 產生函數
    namespace.yaml          ← slurm Namespace，含 pod-security.kubernetes.io/enforce=baseline label
    configmap-static.yaml   ← slurm-config-static（ClusterName / Auth / Plugin / AccountingStorageTRES）
    configmap-nodes.yaml    ← slurm-config-nodes（NodeName / PartitionName / gres.conf）
    controller.yaml         ← controller StatefulSet + PDB
    workers.yaml            ← range pools → 每 pool 的 StatefulSet + PDB
    operator.yaml           ← operator SA + Role + Binding + Deployment + PDB + Service
    services.yaml           ← controller / restapi / 每 pool 的 worker service
    pvc.yaml                ← ctld-state PVC
    login.yaml              ← login Deployment + Service + PDB + slurm-ddp-runtime ConfigMap
    network-policy.yaml     ← 11 條 NetworkPolicy（operator → K8s API egress 443+6443 等）
    gpu/
      gpu-operator-namespace.yaml  ← {{- if .Values.gpu.enabled }} 建 gpu-operator namespace（PSS=privileged）
      device-plugin-config.yaml    ← {{- if .Values.gpu.enabled }} ConfigMap 進 gpu-operator namespace
                                   ← GPU Operator 的 device-plugin 透過 nvidia.com/device-plugin.config
                                   ←   label 從這個 ConfigMap 讀取 sharing 設定
      node-labeler-job.yaml        ← {{- if .Values.gpu.autoLabel }} Job 自動對符合條件的節點打 label
                                   ←   含 ServiceAccount + ClusterRole + ClusterRoleBinding 給 Job 用
    monitoring/             ← {{- if .Values.monitoring.enabled }} Prometheus + Grafana + slurm-exporter
    storage.yaml            ← {{- if .Values.storage.enabled }} NFS subdir provisioner
    tests/
      test-scontrol-ping.yaml    ← helm test 用，跑 scontrol ping + sinfo
      test-mps-job.yaml          ← gpu.enabled=true 時跑 --gres=mps:25 sbatch
```

注意：GPU Operator 自己（DaemonSets、CRDs、`mps-control-daemon` / `device-plugin` / `dcgm-exporter` / `gfd` / `nfd`）由 `scripts/install-gpu-operator.sh` 獨立 `helm install` 進 `gpu-operator` namespace，不在本 chart 內。本 chart 的 `gpu/` 子目錄只有 ConfigMap（給它 device-plugin 讀的 sharing 設定）+ namespace stub + labeler Job。

### Chart.yaml（不含 dependencies）

```yaml
apiVersion: v2
name: slurm-platform
appVersion: "23.11.7"        # Slurm 版本，升 Slurm 透過 helm upgrade 觸發 rolling restart
version: 0.1.0
# 沒有 dependencies。GPU Operator 由 scripts/install-gpu-operator.sh 獨立安裝。
```

GPU Operator 預設會把 driver / toolkit / DCGM exporter 一起裝，跟 host 已裝的 `nvidia-driver-535` + `nvidia-container-toolkit` 衝突——`install-gpu-operator.sh` 用 `--set driver.enabled=false --set toolkit.enabled=false` 把這兩個子模組關掉。

### values.yaml 結構（對齊 mps-migration）

`pools` 用有序 **list**（而非 map），保證 `slurm.conf` 的 `NodeName` 順序與 `PartitionName Nodes=` 順序一致。

```yaml
cluster:
  name: slurm-lab
  namespace: slurm
  runtime: kind              # kind | k3s（影響 TaskPlugin、ProctrackType、device file 路徑）
  podSecurity: baseline      # baseline | restricted | privileged（labels namespace）

image:
  controller: slurm-controller:latest
  worker: slurm-worker:latest
  operator: slurm-elastic-operator:latest
  pullPolicy: IfNotPresent

# 每個 pool 同時帶自己的 partition 名稱（N7：partition split）
partitions:
  - name: cpu
    default: true
    maxTime: INFINITE
  - name: gpu-rtx4070
    default: false
    maxTime: 24:00:00
  - name: gpu-rtx4080
    default: false
    maxTime: 24:00:00

pools:
  - id: cpu
    statefulset: slurm-worker-cpu
    partition: cpu                       # ← 對應 partitions[].name（N7）
    minReplicas: 1
    maxReplicas: 4
    scaleCooldownSeconds: 60
    cpus: 4
    realMemory: 3500
    sockets: 1
    coresPerSocket: 2
    threadsPerCore: 2
    maxNodes: 4
    features: [cpu]
    gres: []
    fallback: true

  - id: gpu-rtx4070
    statefulset: slurm-worker-gpu-rtx4070
    partition: gpu-rtx4070               # ← N7
    minReplicas: 0
    maxReplicas: 2
    scaleCooldownSeconds: 60
    cpus: 4
    realMemory: 3500
    sockets: 1
    coresPerSocket: 2
    threadsPerCore: 2
    maxNodes: 4                          # = sharing.mps replicas，避免 Pending（N3）
    features: [gpu, gpu-rtx4070]
    gres:
      - name: gpu
        type: rtx4070
        count: 1
      - name: mps
        count: 100
    matchGres: [gpu:rtx4070, mps]
    devicePluginConfig: rtx4070-mps      # ← 對應 gpu.deviceConfigs.* key（chart 自動打 node label）

  - id: gpu-rtx4080
    statefulset: slurm-worker-gpu-rtx4080
    partition: gpu-rtx4080               # ← N7
    minReplicas: 0
    maxReplicas: 2
    scaleCooldownSeconds: 60
    cpus: 4
    realMemory: 3500
    sockets: 1
    coresPerSocket: 2
    threadsPerCore: 2
    maxNodes: 1
    features: [gpu, gpu-rtx4080]
    gres:
      - name: gpu
        type: rtx4080
        count: 1
    matchGres: [gpu:rtx4080]
    devicePluginConfig: rtx4080-exclusive

# 注意：不再有頂層 mps.enabled 旗標。MPS 由 device-plugin sharing.mps 提供（N1）；
# 是否啟用對某個 pool 而言，僅由它的 gres 是否含 mps + devicePluginConfig 是否
# 指到 *-mps 決定。worker pod 不需要 hostIPC 或 /tmp/nvidia-mps mount。

gpu:
  enabled: false                         # true 時 gpu-operator dependency 啟用
  autoLabel: true                        # true 時 chart post-install Job 自動對節點打 device-plugin.config label
  # 我們自己的 ConfigMap，由 templates/gpu/device-plugin-config.yaml 渲染。
  # GPU Operator 會掛載這個 ConfigMap 到 device-plugin pod，並依
  # nvidia.com/device-plugin.config label 選用對應 key。
  deviceConfigs:
    default:
      version: v1
    rtx4070-mps:
      version: v1
      sharing:
        mps:
          resources:
            - name: nvidia.com/gpu
              replicas: 4
    rtx4080-exclusive:
      version: v1                        # 獨佔，無 sharing
  # 對映「節點 selector → 要套哪個 config key」。autoLabel Job 用這個。
  nodeAssignments:
    - selector:
        # gpu-host-class 是使用者自己事先打的 label，例如 'rtx4070'
        gpu-host-class: rtx4070
      config: rtx4070-mps
    - selector:
        gpu-host-class: rtx4080
      config: rtx4080-exclusive

# GPU Operator subchart override（key 必須與 Chart.yaml dependency 的 name 一致）。
# 只在 gpu.enabled=true 時生效。host 已自裝 nvidia-driver-535 + nvidia-container-toolkit，
# 所以關掉 driver / toolkit；只讓 Operator 接管 device-plugin、MPS daemon、（可選）DCGM。
gpu-operator:
  driver:
    enabled: false                       # 用 host apt 裝的 nvidia-driver-535
  toolkit:
    enabled: false                       # 用 host apt 裝的 nvidia-container-toolkit
  devicePlugin:
    enabled: true
    config:
      # 指向我們自己的 ConfigMap（templates/gpu/device-plugin-config.yaml）
      name: slurm-on-k8s-device-plugin-config
      default: default
  mps:
    root: /run/nvidia/mps                # 與 host hostPath 對齊
  dcgmExporter:
    enabled: false                       # 等 5-B 觀測性再開
  gfd:                                   # gpu-feature-discovery
    enabled: true                        # 自動補 nvidia.com/gpu.product 等 label
  nodeStatusExporter:
    enabled: false
  migManager:
    enabled: false                       # RTX 4070/4080 不支援 MIG
  validator:
    plugin:
      env:
        - name: WITH_WORKLOAD            # 把 validator 的 cuda-vector-add workload 開起來
          value: "true"

slurm:
  # 這些以前散在 render-core.py header 裡，現在抽出來給 values override：
  authType: auth/munge
  credType: cred/munge
  selectType: select/cons_tres
  taskPlugin:
    kind: task/cgroup                    # k3s 預設；values-dev.yaml 覆寫成 task/none
  proctrack: proctrack/cgroup            # N11：與 task/cgroup 一致
  accounting:
    enabled: true                        # 開 slurmdbd
    storageTres: [gres/gpu, gres/mps]    # N6
  jwt:
    lifespanSeconds: 86400               # 1 day（v3 1-B 建議；舊值 10 年）
  fairshare:
    enabled: false                       # P1 之後再啟用

operator:
  pollIntervalSeconds: 15
  scaleDownCooldownSeconds: 60
  drainTimeoutSeconds: 1800              # N8
  checkpointGuard:
    enabled: true
    maxAgeSeconds: 600
    graceSeconds: 300

networkPolicy:
  enabled: true
  apiServerPorts: [443, 6443]            # N5：同時允許兩個 port

monitoring:
  enabled: true
  grafana:
    adminPassword: admin
  alertmanager:
    slack:
      webhookUrl: ""

storage:
  enabled: false                         # true → NFS subdir provisioner
  nfsServer: ""
  nfsPath: /shared

# 預先建立的 secret 名稱（chart 不產生，只引用）
secrets:
  munge: slurm-munge
  jwt: slurm-jwt
  ssh: slurm-ssh
```

### `_helpers.tpl`：slurm.conf / gres.conf 產生邏輯

取代 `render-core.py` 的核心邏輯，寫成三個 named template。注意 `File=/dev/nvidia0` 對所有 GPU pool 都成立（device-plugin 把分配到的 GPU 一律以 `/dev/nvidia0` 暴露給 pod）。

```
{{- define "slurm.slurmConfStatic" -}}
ClusterName={{ .Values.cluster.name }}
SlurmctldHost=slurm-controller-0(slurm-controller-0.slurm-controller.{{ .Values.cluster.namespace }}.svc.cluster.local)
AuthType={{ .Values.slurm.authType }}
CredType={{ .Values.slurm.credType }}
SelectType={{ .Values.slurm.selectType }}
{{- if eq .Values.cluster.runtime "k3s" }}
TaskPlugin={{ .Values.slurm.taskPlugin.kind }}
ProctrackType={{ .Values.slurm.proctrack }}
CgroupPlugin=cgroup/v2
{{- else }}
TaskPlugin=task/none
ProctrackType=proctrack/linuxproc
{{- end }}
{{- $gresTypes := list -}}
{{- range .Values.pools }}{{- range .gres }}{{- $gresTypes = append $gresTypes .name }}{{- end }}{{- end }}
{{- if $gresTypes }}
GresTypes={{ $gresTypes | uniq | join "," }}
{{- if .Values.slurm.accounting.storageTres }}
AccountingStorageTRES={{ .Values.slurm.accounting.storageTres | join "," }}
{{- end }}
{{- end }}
Include /etc/slurm/slurm.nodes.conf       # ← configmap-nodes 提供
{{- end }}

{{- define "slurm.slurmConfNodes" -}}
{{- range .Values.pools }}
{{- $pool := . }}
{{- range $i := until (int $pool.maxNodes) }}
NodeName={{ $pool.statefulset }}-{{ $i }} CPUs={{ $pool.cpus }} RealMemory={{ $pool.realMemory }} Sockets={{ $pool.sockets }} CoresPerSocket={{ $pool.coresPerSocket }} ThreadsPerCore={{ $pool.threadsPerCore }} Feature={{ $pool.features | join "," }}{{- if $pool.gres }} Gres={{- $first := true -}}{{- range $pool.gres -}}{{- if not $first }},{{ end }}{{ .name }}{{- if .type }}:{{ .type }}{{- end }}:{{ .count }}{{- $first = false -}}{{- end }}{{- end }} State=CLOUD
{{- end }}
{{- end }}
{{- range .Values.partitions }}
{{- $part := . }}
{{- $nodes := list -}}
{{- range $.Values.pools -}}{{- if eq .partition $part.name -}}{{- range $i := until (int .maxNodes) -}}{{- $nodes = append $nodes (printf "%s-%d" .statefulset $i) -}}{{- end -}}{{- end -}}{{- end }}
PartitionName={{ $part.name }} Nodes={{ $nodes | join "," }} Default={{ if $part.default }}YES{{ else }}NO{{ end }} MaxTime={{ $part.maxTime }} State=UP
{{- end }}
{{- end }}

{{- define "slurm.gresConf" -}}
{{- range .Values.pools }}
{{- $pool := . }}
{{- range $i := until (int $pool.maxNodes) }}
{{- range $pool.gres }}
{{- if eq .name "mps" }}
NodeName={{ $pool.statefulset }}-{{ $i }} Name=mps Count={{ .count }}
{{- else }}
NodeName={{ $pool.statefulset }}-{{ $i }} Name={{ .name }} Type={{ .type }} Count={{ .count }} File={{ if eq $.Values.cluster.runtime "k3s" }}/dev/nvidia0{{ else }}/dev/null{{ end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
```

### `workers.yaml`：pool StatefulSet 迴圈（無 hostIPC）

```yaml
{{- range .Values.pools }}
{{- $pool := . }}
{{- $isGpu := gt (len $pool.gres) 0 }}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ $pool.statefulset }}
spec:
  replicas: {{ $pool.minReplicas }}
  ...
    spec:
      # 注意：不需要 hostIPC，sharing.mps 由 device-plugin DaemonSet 處理
      containers:
        - name: slurm-worker
          {{- if and $isGpu (eq $.Values.cluster.runtime "k3s") }}
          resources:
            limits:
              nvidia.com/gpu: "1"           # 拿一個 sharing.mps 切片
          {{- end }}
          # 兩個 ConfigMap 分別 mount，pool 變動只影響 nodes ConfigMap
          volumeMounts:
            - name: slurm-config-static
              mountPath: /etc/slurm/slurm.conf
              subPath: slurm.conf
            - name: slurm-config-nodes
              mountPath: /etc/slurm/slurm.nodes.conf
              subPath: slurm.nodes.conf
            - name: slurm-config-nodes
              mountPath: /etc/slurm/gres.conf
              subPath: gres.conf
{{- end }}
```

### `operator.yaml`：`PARTITIONS_JSON` 從 values 產生

```yaml
- name: PARTITIONS_JSON
  value: |
    [
    {{- range $i, $pool := .Values.pools }}
    {{- if $i }},{{ end }}
    {"partition":"{{ $pool.partition }}",
     "worker_statefulset":"{{ $pool.statefulset }}",
     "min_replicas":{{ $pool.minReplicas }},
     "max_replicas":{{ $pool.maxReplicas }},
     "scale_up_step":1,"scale_down_step":1,
     "scale_down_cooldown":{{ $pool.scaleCooldownSeconds }},
     "drain_timeout_seconds":{{ $.Values.operator.drainTimeoutSeconds }},
     "match_features":{{ $pool.features | toJson }},
     {{- if $pool.matchGres }}"match_gres":{{ $pool.matchGres | toJson }},{{ end }}
     "fallback":{{ default false $pool.fallback }}}
    {{- end }}
    ]
```

### GPU 節點自動 labeling（取代手動 `kubectl label node`）

`templates/gpu/node-labeler-job.yaml` 在 `helm install` / `helm upgrade` 時跑一次性 Job：

```yaml
{{- if and .Values.gpu.enabled .Values.gpu.autoLabel }}
apiVersion: batch/v1
kind: Job
metadata:
  name: gpu-node-labeler
  annotations:
    "helm.sh/hook": post-install,post-upgrade
    "helm.sh/hook-delete-policy": before-hook-creation,hook-succeeded
spec:
  template:
    spec:
      serviceAccountName: gpu-node-labeler   # 對應 ClusterRole patch nodes
      restartPolicy: OnFailure
      containers:
        - name: labeler
          image: bitnami/kubectl:latest
          command:
            - sh
            - -c
            - |
              {{- range .Values.gpu.nodeAssignments }}
              kubectl label nodes -l {{- range $k, $v := .selector }} {{ $k }}={{ $v }}{{- end }} \
                  nvidia.com/device-plugin.config={{ .config }} --overwrite
              {{- end }}
{{- end }}
```

使用者只需要事先對節點打一個語義化 label（如 `gpu-host-class=rtx4070`），chart 就會自動把對應 device-plugin config 的 label 套上去。

### values overlay 策略（移除 `mps.enabled`）

| 檔案 | 用途 | 關鍵差異 |
|------|------|---------|
| `values.yaml` | 基準（Kind 開發） | `runtime: kind`、`gpu.enabled: false`、`slurm.taskPlugin.kind: task/none`、`storage/monitoring: enabled` 看情況 |
| `values-k3s.yaml` | Linux + 真實 GPU + MPS | `runtime: k3s`、`gpu.enabled: true`、`gpu.autoLabel: true` |
| `values-dev.yaml` | CI / 無 GPU 環境 | `gpu.enabled: false`、`monitoring.enabled: false`、`storage.enabled: false`、`pools` 只留 cpu |

### Helm test

`templates/tests/test-scontrol-ping.yaml`：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: "{{ .Release.Name }}-test-scontrol"
  annotations:
    "helm.sh/hook": test
spec:
  restartPolicy: Never
  containers:
    - name: test
      image: {{ .Values.image.controller }}
      command: [sh, -c, "scontrol ping && sinfo -h"]
```

`gpu.enabled=true` 時加 `test-mps-job.yaml`，跑 `--gres=mps:25` sbatch 並確認 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25` 出現在 job env（呼應 `verify-gpu.sh` step 6）。

### 漸進實作順序（6 個 PR，避免 big-bang 重寫）

| 階段 | 內容 | 完成標準 | 風險 |
|:---:|---|---|:---:|
| **A** | `chart/` scaffold：`Chart.yaml`、`values.yaml`、`_helpers.tpl` 把 `render-core.py::build_slurm_conf` 翻成 Go template；其他 templates 為空 | `helm template chart/ -f values-k3s.yaml \| diff -u manifests/core/slurm-static.yaml`，差異收斂到只剩格式空白 | 中 |
| **B** | 加 `templates/configmap-static.yaml` + `configmap-nodes.yaml` + `controller.yaml` + `workers.yaml` + `pvc.yaml` + `namespace.yaml`（PSS baseline label） | `helm install` 後 `slurmctld` 起得來、`sinfo` 看到所有 pool 的 node | 低 |
| **C** | 加 `operator.yaml` + `network-policy.yaml`（443+6443）+ `login.yaml` | operator 擴／縮 worker 正常、`scale_action` metric 有資料、`scontrol ping` from login pod 成功 | 低 |
| **D** | 加 `gpu/` 子目錄（`gpu-operator-namespace.yaml` + `device-plugin-config.yaml` ConfigMap + `node-labeler-job.yaml`）；新增 `scripts/install-gpu-operator.sh` 把 NVIDIA GPU Operator 獨立裝到 `gpu-operator` namespace（`--set driver.enabled=false --set toolkit.enabled=false`）；移除自寫 `manifests/gpu/nvidia-device-plugin.yaml` | `verify-gpu.sh` 全綠（**含 step 6 MPS**——這是 Phase 5-A 的核心 milestone，因為 GPU Operator 才能讓 `--gres=mps:N` 成立）| 中-高 |
| **E** | 把 monitoring / storage 收進 chart `templates/monitoring/` `storage.yaml`，`enabled` flag 控制 | `helm install --set monitoring.enabled=true` 一次帶起 Prometheus + Grafana + slurm-exporter | 低 |
| **F** | 砍掉 `render-core.py`、`scripts/bootstrap*.sh` 大部分內容（保留 `setup-linux-gpu.sh` 與 `create-secrets.sh`）、`manifests/core/slurm-static.yaml`、`worker-pools.json`；README / migration.md 改寫為 `helm install` | bootstrap 時間 < 5 分鐘；`docs/migration.md` 從多步腳本改為 4 行 helm 指令 | 高 |

> 階段 A–C 可在 Windows + Kind 上做完並驗證；階段 D 需要 Linux + 真實 GPU 才能整合測試。階段 E 與目前 `bootstrap-monitoring.sh` 行為對齊，最低風險。階段 F 是 cutoff，需要 README、CI、文件大量同步更新。

### 廢棄的檔案（Phase F 後）

| 檔案 | 取代者 |
|------|--------|
| `manifests/core/worker-pools.json` | `chart/values.yaml::pools` |
| `scripts/render-core.py` | `chart/templates/_helpers.tpl` |
| `scripts/bootstrap.sh` 大部分 | `helm install` |
| `scripts/bootstrap-gpu.sh` | `scripts/install-gpu-operator.sh` + chart 內 node-labeler Job |
| `scripts/bootstrap-monitoring.sh` | `helm install --set monitoring.enabled=true` |
| `manifests/core/slurm-static.yaml` | `helm template chart/` 動態產生 |
| `manifests/gpu/nvidia-device-plugin.yaml` | install-gpu-operator.sh + chart 的 device-plugin-config ConfigMap（GPU Operator 內建 device-plugin DaemonSet）|
| `manifests/gpu/mps-daemonset.yaml`（已是 stub） | 直接刪除（GPU Operator 內建 `mps-control-daemon` DaemonSet）|

保留：`scripts/setup-linux-gpu.sh`（host 層 NVIDIA toolkit + k3s 安裝，本來就不該進 chart）、`scripts/create-secrets.sh`（chart 之外的 prerequisite）、`scripts/verify*.sh`（Helm test 之外的 e2e 驗證）。

### 不在 5-A 範圍但需要決定的事

1. **secret 怎麼生**：起步用 `scripts/create-secrets.sh` 預先建立；長期可改 External Secrets / Sealed Secrets。
2. **CI 怎麼跑**：`helm lint` + `helm template -f values-dev.yaml \| kubectl apply --dry-run=server -f -` + `helm-unittest` 一支 GitHub Actions workflow。
3. **GitOps 接入點**：等 chart 進 OCI registry 或 GitHub Pages 後，ArgoCD `Application` 指向那個 repo + values overlay，不需要動 chart 本體。

---

## 5-B：OpenTelemetry 分散式追蹤

### 為什麼 metrics 不夠
Prometheus 告訴你「現在 p95 provisioning latency 是 45 秒」，但不告訴你：
- 這 45 秒是花在 K8s 排程（pending pod）、image pull、還是 Slurm node registration？
- 是某個特定 job 特別慢，還是系統性問題？

OpenTelemetry trace 回答的是「**這一次** job J42 為什麼比較慢」。

### Trace 結構設計

```
TraceID: job-{SLURM_JOB_ID}
│
├── [Span] job_submit
│     attributes: job_id, partition, gres, requested_cpus
│
├── [Span] queue_wait  (start=submit_time, end=scale_up_decision_time)
│     attributes: pending_jobs_at_submit, pool
│
├── [Span] scale_up_decision  (Operator)
│     attributes: from_replicas, to_replicas, reason
│
├── [Span] k8s_provisioning  (start=patch_time, end=pods_ready_time)
│     attributes: pool, target_replicas
│     → 已有資料來源：slurm_operator_provisioning_latency_seconds histogram
│
├── [Span] slurm_node_registration
│     attributes: node_name, registered_at
│
├── [Span] job_running  (start=start_time, end=end_time)
│     attributes: nodes, cpus, gres
│
└── [Span] checkpoint_write  (fine-tuning job 專用，SIGTERM handler 觸發)
      attributes: checkpoint_path, file_size_bytes
```

### 實作路徑
1. **Operator 加 OTel SDK**：`opentelemetry-sdk` + `opentelemetry-exporter-otlp`，在 `scale_action`、`loop_observation` 決策處建立 span。
2. **Exporter 加 Span**：每次 slurmrestd REST 呼叫包在 span 裡，紀錄 HTTP latency。
3. **OTel Collector**：monitoring namespace 部署 `otel/opentelemetry-collector`，接收 OTLP，轉發到 Grafana Tempo。
4. **Grafana Tempo 整合**：加 Tempo datasource + exemplar 連結，讓 Prometheus histogram spike 可以直接跳到對應 trace。

### Exemplar 連結（差異化觀測點）
在 Provisioning Latency p95 圖上，spike 對應的那個 TraceID 可以直接點進去看整條鏈。這是目前所有 Slurm-on-K8s 開源方案（SUNK、Slinky、Slonk）都沒有做到的端到端觀測視角。

---

## 5-C：工作負載模板

### 目標
NFS `/shared/templates/` 預放 5 支對應平台典型使用情境的 sbatch 模板，使用者登入後 cp 過去修改參數即可提交，不需要自己研究 GRES 語法。

| 模板 | GRES 設定 | 展示的系統能力 |
|------|----------|--------------|
| `01_preprocess.sh` | `--cpus-per-task=8` | CPU pool autoscale，與 GPU job 完全並行 |
| `02_batch_infer.sh` | `--gres=mps:25` | MPS 多工，4 個推論 job 共用同一張 GPU |
| `03_hpo_array.sh` | `--array=1-8 --gres=mps:25` | Job array + MPS，8 組超參數實驗並行 |
| `04_finetune_lora.sh` | `--gres=gpu:rtx4080:1` | 整卡獨佔 + checkpoint guard 縮容保護 |
| `05_ddp_2gpu.sh` | `--nodes=2 --gres=gpu:1` | 跨 worker pod 的 2-GPU DDP 訓練 |

### 實作路徑
- 新增 `templates/` 目錄，每支腳本含詳細行內註解（GRES 含義、資源選擇理由）。
- `bootstrap-lmod.sh` 結尾加一步：`cp -r templates/ /shared/templates/`。
- Login pod `Dockerfile` 加入 `/etc/motd`，顯示平台簡介與模板路徑。

---

## 5-D：SSH Login

### 問題
目前進入 login node 需要 `kubectl exec -it deploy/slurm-login -- bash`，使用者必須安裝 kubectl 並持有 kubeconfig，這不是「共用 AI 計算平台」應有的使用體驗。

### 目標
```
ssh -p 2222 user@<k3s-host-ip>
       ↓
NodePort :2222 → slurm-login pod
                   ├── sbatch / squeue / sinfo（Slurm 指令即開即用）
                   └── /shared/（NFS 掛載，模型 + 輸出 + 模板共用）
```

### 實作路徑
- `docker/login/Dockerfile` 加入 `openssh-server`，SSH key 認證（禁用密碼登入）。
- `slurm-login` Service 改為 `NodePort`，固定 port 2222。
- `scripts/bootstrap.sh` 加入 SSH host key 初始化（`ssh-keygen -A`）。
- 後續（多租戶時）：`scripts/add-user.sh` 同時呼叫 `useradd` 和 `sacctmgr add user`。

---

## Debug Record

### 問題 1：slurmdbd 啟動後 hostname 不符

**現象：** slurmdbd 啟動後立即 fatal exit：`This host not configured to run SlurmDBD (slurmdbd-xxx != slurmdbd)`。

**原因：** `slurmdbd.conf` 的 `DbdHost=slurmdbd`，但 Deployment pod 的 hostname 是 `slurmdbd-{replicaset}-{random}`（Kubernetes 預設行為）。slurmdbd 在啟動時會驗證 `DbdHost` 是否匹配當前 hostname。

**修法：** `slurm-accounting.yaml` 的 Deployment pod spec 加入 `hostname: slurmdbd`，讓 pod hostname 固定為 `slurmdbd`。

---

### 問題 2：slurmctld 首次啟動 fatal（TRES 缺失）

**現象：** 新叢集第一次啟動時，slurmctld fatal exit：`You are running with a database but for some reason we have no TRES from it`。

**原因：** `slurm.conf` 設定了 `AccountingStorageType=accounting_storage/slurmdbd`，slurmctld 啟動時需要從 slurmdbd 取得 TRES（Trackable RESources）定義。若 slurmdbd 尚未 ready（容器剛建立），且又沒有本地 state file，slurmctld 就會 fatal exit 而非等待。

**修法：** `scripts/render-core.py` 的 controller 啟動腳本加入 wait loop：偵測到 `AccountingStorageType=slurmdbd` 時，先用 bash TCP 連線確認 `slurmdbd.slurm.svc.cluster.local:6819` 可達，再 exec slurmctld。

```bash
if grep -q 'AccountingStorageType=accounting_storage/slurmdbd' /etc/slurm/slurm.conf; then
  until (echo >/dev/tcp/slurmdbd.slurm.svc.cluster.local/6819) 2>/dev/null; do sleep 3; done
fi
exec slurmctld -Dvvv
```

### 問題 3：PDB 與 StatefulSet 縮容的關係

**常見誤解：** 認為 PDB 的 `maxUnavailable: 1` 會阻止 operator 把 replicas 從 4 降到 0。

**實際行為：**
- StatefulSet `replicas` 調整是 **Desired State**，K8s controller 會逐步刪除 Pod（最高優先）
- PDB 保護的是 **Voluntary Disruption**（如 `kubectl drain node`、節點升級）
- operator 調整 replicas = K8s 內部操作，**不受 PDB 約束**
- 結論：PDB 與 drain-then-scale 並不衝突；PDB 保護的是基礎設施層面，drain 保護的是 job 層面

---

### 問題 4：`MpiDefault=pmi2` 與 `mpi_pmi2.so` plugin 位置

**現象：** 改為 `MpiDefault=pmi2` 後，`srun --mpi=pmi2` job 可能出現 `srun: error: PMI2 not found`。

**原因：** Ubuntu 22.04 的 `slurmd` 套件把 MPI plugin 放在 `/usr/lib/x86_64-linux-gnu/slurm-wlm/`，路徑需在 `PluginDir` 中。

**排查步驟：**
```bash
# 在 worker pod 確認 pmi2 plugin 存在
kubectl -n slurm exec pod/slurm-worker-cpu-0 -- \
  find /usr/lib -name 'mpi_pmi2.so' 2>/dev/null

# 確認 PluginDir 設定（通常不用手動設）
kubectl -n slurm exec pod/slurm-controller-0 -- \
  scontrol show config | grep PluginDir
```

**實際發現：** Ubuntu 22.04 `slurmd`（Slurm 21.08）內建 PMI2，不需要額外安裝；plugin 會自動在 PluginDir 找到。

---

### 問題 5：`srun --mpi=pmi2` 在單節點多 task 的行為

**確認：** `--ntasks=2 --nodes=1` 加上 `srun --mpi=pmi2` 可以在同一個 worker pod 啟動兩個 MPI rank，`$SLURM_PROCID` 分別為 0 和 1。這對容器化 HPC 測試是最低門檻的 MPI 驗證方式，不需要 pod 間網路或 InfiniBand。

---

### 問題 6：`/etc/profile.d/slurm-modulepath.sh` 在 sbatch 裡不生效

**現象：** login pod 互動式 shell `module avail` 正常，但 sbatch job 內 `module load` 後 `MPI_HOME` 仍是 NOT_SET。

**原因：**
- `/etc/profile.d/*.sh` 只在 **login shell** 啟動時自動 source（`bash -l`）
- Slurm 以非互動、非 login 的 `/bin/bash` 執行 sbatch 腳本
- 因此 `/etc/profile.d/slurm-modulepath.sh` 完全沒被讀到，`MODULEPATH` 未設定
- Lmod 找不到 `/opt/modulefiles`，`module load openmpi/4.1` 靜默失敗

**修法：** 改用 Lmod 官方機制，在 Dockerfile 寫入 `/etc/lmod/modulespath`：
```dockerfile
RUN mkdir -p /etc/lmod && echo '/opt/modulefiles' > /etc/lmod/modulespath
```
Lmod 在每次 `source /etc/profile.d/lmod.sh` 時都會讀這個檔案，不論 shell 類型。

---

### 問題 7：job output 在 worker pod，不在 login pod

**現象：** `cat /tmp/phase5-verify-$jid.out` 在 login pod 找不到檔案。

**原因：** Slurm 的 `--output` 路徑是在**執行 job 的 worker node** 上建立的。
沒有共享 filesystem（NFS/Lustre），output 不會自動傳回 login node。

**在真實 HPC：** 所有節點共享 NFS，`/home/user/` 或 `/scratch/` 上的 output 到處都能讀。

**最終解法（Lmod + NFS 整合）：** `bootstrap-lmod.sh` 掛載 NFS 到所有 Pod，並確保 `/shared/jobs/` 目錄存在。job script 輸出路徑改為：
```bash
#SBATCH --output=/shared/jobs/phase5-verify-%j.out
#SBATCH --error=/shared/jobs/phase5-verify-%j.err
```
所有節點共享同一 NFS，login pod 可直接 `cat /shared/jobs/<outfile>` 取得輸出。

---

### 問題 8：Phase 3 E2E — slurmd 在新 pod 啟動後持續 NOT_RESPONDING

**影響範圍：** `verify-storage-e2e.sh` 的多節點 sbatch 驗證流程。

**現象：** operator 把 worker 從 1 台 scale-up 到 2 台後，`slurm-worker-cpu-1` pod 已 Running，但 `sinfo` 一直顯示 `idle*`（IDLE+NOT_RESPONDING）。SIGHUP 讓它短暫變成 `idle`，但 sbatch job 跑完卻卡在 COMPLETING 數分鐘不離開 queue。

**根因（三層疊加）：**

| 層次 | 現象 | 原因 |
|------|------|------|
| 1. NP race | pod 起來後 ~30s 內 `idle*` | CNI NetworkPolicy 比 slurmd 第一次 registration RPC 晚幾秒套用；slurmctld 的 back-ping（registration agent）此時失敗 → NOT_RESPONDING |
| 2. Slurm 扇出 RPC 使用 ephemeral port | RESPONSE_FORWARD_FAILED，心跳持續失敗 | slurmctld 的 fan-out tree RPC 要求 worker 連回 controller 的**臨時 port**（OS 隨機分配，非 6817）回傳聚合結果。NetworkPolicy `allow-worker-egress` 只開放 6817，其餘封包被 drop |
| 3. slurmctld IP cache 過期 | TERMINATE_JOB Connection timed out，COMPLETING 永遠不解 | pod 每次重啟取得新 IP（e.g. .44 → .84 → .91），但 slurmctld 把 NodeAddr 解析結果快取在記憶體，不重查 DNS；所有後續 PING/TERMINATE_JOB/KILL_JOB 打到舊 IP |

**診斷方式：**
```bash
# 看 slurmctld 在用哪個 IP 連 cpu-1
kubectl -n slurm logs pod/slurm-controller-0 | grep "connect to.*6818"
# 對比 pod 實際 IP
kubectl -n slurm get pod slurm-worker-cpu-1 -o jsonpath='{.status.podIP}'

# 看 slurmd 收到 zero-bytes 與 RESPONSE_FORWARD_FAILED
kubectl -n slurm logs pod/slurm-worker-cpu-1 | grep -E "Zero Bytes|FORWARD_FAILED|slurm_msg_sendto"
```

**修法（三步對應三層）：**

1. **NetworkPolicy 放開 worker → controller 所有 port**（解決 ephemeral port 被 block）

   `manifests/networking/network-policy.yaml` 的 `allow-worker-egress` 和 `allow-login-egress` 中，原本 `ports: [6817, 22]` 改為**不限制 port**（移除 ports 欄位），允許 worker/login 往 controller 送任何 TCP。

2. **SIGHUP 補充 registration**（解決 NP race）

   `verify-storage-e2e.sh` 的 `wait_slurm_node_responding()` 在 `idle*` 持續 30 秒後，對 worker pod 發 `kill -HUP $(pgrep slurmd)`，觸發 slurmd 重新送 registration RPC；此時 NP 已套用完成，back-ping 成功。

3. **更新 slurmctld 的 NodeAddr**（解決 IP cache 過期）

   在 `wait_slurm_node_responding()` 開始前（以及 SIGHUP 後）執行：
   ```bash
   pod_ip=$(kubectl -n slurm get pod slurm-worker-cpu-1 -o jsonpath='{.status.podIP}')
   kubectl -n slurm exec pod/slurm-controller-0 -- bash -lc \
     "scontrol update NodeName=slurm-worker-cpu-1 NodeAddr=${pod_ip}"
   ```
   強制 slurmctld 更新快取 IP，之後的 PING/TERMINATE_JOB 才能到達新 pod。

**附帶發現：**
- `sbatch --hold` 讓 trigger job 保持 PENDING 不執行；`scancel` 已 hold 的 job 瞬間消失（無 COMPLETING 殘留），比取消 RUNNING job 乾淨很多。
- 多節點 job 的 COMPLETING 卡住，主因是 `REQUEST_TERMINATE_JOB`（slurmctld→slurmd:6818）或 `EPILOG_COMPLETE`（slurmd→slurmctld:6817）任一方向失敗，不是 NFS 問題。
- `scontrol reconfigure` **不應**在 verify 流程呼叫：它會讓 slurmctld 嘗試 DNS resolve 所有靜態節點（包含未部署的 gpu 節點），Block 30–60 秒，導致 operator REST API 回傳空結果，誤觸 scale-down。

---

## Phase 5 建議優先順序

| 項目 | 難度 | 價值 | 建議順序 | 依賴 |
|------|------|------|---------|------|
| Helm Chart（5-A） | 中 | 部署可重複，消除環境差異 | **第一** | 無 |
| OpenTelemetry（5-B） | 高 | 差異化觀測，補 metrics 的盲點 | **第二** | Helm（OTel Collector 進 chart） |
| 工作負載模板（5-C） | 低 | 使用者開箱即用，展示平台能力 | **第三** | NFS（Phase 3 已完成） |
| SSH Login（5-D） | 中 | 真實使用體驗，不需要 kubectl | **第四** | Helm（Login Service 進 chart） |
| Fair-Share 多租戶 | 中 | 多人共用不互搶（後期） | 第五 | SSH Login（需要使用者帳號） |
| Operator HA | 中 | Zero-downtime 升級（生產需求） | 第六 | Helm（replicas 從 values 控制） |

5-C（模板）技術依賴最少，可以在 5-A 進行中同步完成。5-D 放第四是因為單一使用者情境下 `kubectl exec` 仍可接受，SSH 優先級低於讓平台本身功能完整。

---

## 設計重點

**為什麼 Slurm node 要預先全部宣告？**
所有節點在 `slurm.conf` 裡預先定義到 `maxNodes`，Operator 只調整 StatefulSet 的 replica 數，而不重寫 Slurm 設定檔。這避免了每次擴縮時 `slurmctld` 重新解析所有 DNS 造成的連鎖延遲。

**為什麼 Operator 不用 Kopf 或 CRD？**
刻意保持輕量。Operator 是純 Python，沒有自訂 CRD、沒有 webhook，部署門檻低，邏輯一眼就能看懂。Slurm 狀態查詢（queue、job、node）透過 slurmrestd REST API 進行；StatefulSet replica 調整仍透過 `kubectl patch`。

**Checkpoint Guard + Drain-then-Scale 是什麼？**
縮容分兩個階段保護執行中的 AI 訓練任務：

1. **Checkpoint Guard**：若 checkpoint 檔案不存在或超過 `MAX_CHECKPOINT_AGE_SECONDS`（預設 10 分鐘），縮容決策會被阻擋。`CHECKPOINT_PATH=""` 時自動停用（避免靜默 block）；`CHECKPOINT_GRACE_SECONDS` 允許 job 啟動初期尚未寫出 checkpoint 時仍可縮容（grace period 內不阻擋）。
2. **Drain-then-Scale**：通過 Guard 後，Operator 先呼叫 `scontrol update State=DRAIN` 將目標節點標記為不接受新 job，等到 `CPUAlloc == 0`（節點上所有 job 都跑完）才真正減少 StatefulSet replica，避免執行中的訓練被強制中斷。若在 drain 等待期間有新的 scale-up 需求，drain 會自動取消（`State=RESUME`）。

**Operator Cooldown 如何在 Pod 重啟後存活？**
每次 scale-up 成功後，Operator 把時間戳寫入 StatefulSet annotation `slurm.k8s/last-scale-up-at`。Pod 重啟時從 annotation 還原 cooldown 計時，避免重啟後立即觸發錯誤的縮容。

**為什麼 slurmctld 不怕 pod 重啟？**
`StateSaveLocation`（`/var/spool/slurmctld`）掛載了獨立的 PVC（`slurm-ctld-state`）。controller pod 重啟後，job queue、node 狀態、及會計紀錄指標都會從磁碟還原，不需要重新提交任務。

**slurmdbd 提供什麼？**
slurmdbd 搭配 MySQL 後端，把每個 job 的 CPU-hours、使用者、帳戶等資訊持久化。`sacct` 可以查詢歷史 job 統計，也是 Phase 5 Fair-Share 多租戶排程的前置條件。

# Job-Hardware Mapping

## 📦 資源模型概覽

本 repo 採用 **Slurm-on-K8s 雙層架構**，每個 worker node 是一個 K8s Pod，Slurm 把整個 Pod 視為一台 node：

```
Slurm Layer  ←─ 排程、配置記帳（CR_Core、GRES）
     ↓
K8s Layer    ←─ 容器資源請求（requests/limits）、device plugin
     ↓
Hardware     ←─ 實體 CPU cores、GPU SM + VRAM
```

關鍵設定（`slurm.conf`）：

| 參數 | 值 | 意義 |
|------|----|------|
| `SelectType` | `select/cons_tres` | CPU/GPU 皆以 consumable resource 模式分配 |
| `SelectTypeParameters` | `CR_Core` | Slurm 以 core 為單位追蹤 CPU 消耗 |
| `TaskPlugin` | `task/none` | **無 cgroup/CPU binding**，排程計帳但不強制隔離 |
| `GresTypes` | `gpu` | GPU 宣告為 GRES |

每台 worker 宣告：CPU worker → `CPUs=4`；GPU worker → `CPUs=4` + `Gres=gpu:<type>:1`

---

## 📍 部署環境規格：Linux + k3s + RTX 4070 + RTX 4080（雙 GPU）

> 本節以實際遷移目標環境為例，說明硬體資源如何對應到各層宣告。

### 主機硬體

| 項目 | 規格 | 說明 |
|------|------|------|
| CPU | 12 cores（如 Intel i7-12700 / Ryzen 9 5900X） | k3s 單節點，所有 pod 共用 |
| RAM | 32 GB DDR5 | 各 worker pod 依 `RealMemory` 宣告分配帳本 |
| GPU 0 | NVIDIA RTX 4070（Blackwell GB203） | `/dev/nvidia0`，主 GPU |
| GPU 0 VRAM | 12 GB GDDR7 | 單一作業可用全部 12 GB |
| GPU 0 SM | 48 SM | MPS 下 `mps:25` ≈ 12 SM |
| GPU 0 CUDA Cores | 6144 | 記憶體頻寬 ~672 GB/s |
| GPU 1 | NVIDIA RTX 4080（Ada Lovelace AD103） | `/dev/nvidia1`，附加 GPU |
| GPU 1 VRAM | 16 GB GDDR6X | 單一作業可用全部 16 GB |
| GPU 1 SM | 76 SM | MPS 下 `mps:25` ≈ 19 SM |
| GPU 1 CUDA Cores | 9728 | 記憶體頻寬 ~717 GB/s |
| OS / Runtime | Ubuntu 22.04 + containerd + k3s | `K8S_RUNTIME=k3s` |

### Worker Pod 資源宣告對照

在 k3s 單節點上，所有 worker pod 實際跑在同一台實體主機。Slurm 帳本追蹤的是「pod 宣告量」，OS 層透過 **cgroup v2**（`TaskPlugin=task/cgroup`，`REAL_GPU=true`）做實際隔離。

| Worker 類型 | StatefulSet 名稱 | Slurm CPUs | Slurm RealMemory | GRES | K8s resource.limits |
|------------|----------------|-----------|-----------------|------|---------------------|
| CPU worker | `slurm-worker-cpu` | 4 cores | 3500 MB (~3.4 GB) | 無 | cpu: 4, memory: 3500Mi |
| GPU worker (RTX 4070) | `slurm-worker-gpu-rtx4070` | 4 cores | 3500 MB | gpu:rtx4070:1, mps:100 | cpu: 4, memory: 3500Mi, nvidia.com/gpu: 1 |
| GPU worker (RTX 4080) | `slurm-worker-gpu-rtx4080` | 4 cores | 3500 MB | gpu:rtx4080:1, mps:100 | cpu: 4, memory: 3500Mi, nvidia.com/gpu: 1 |

> 兩張 GPU 各為獨立 StatefulSet，`maxNodes=1`（各只能開 1 個 GPU pod）。K8s device plugin 透過 `CUDA_VISIBLE_DEVICES` 決定 pod 拿到哪張 GPU（`0` = RTX 4070，`1` = RTX 4080）。CPU worker pool 最多可開 4 個 pod。

### 工作類型與資源分配

以下五種工作類型對應平台的典型 AI 使用情境，每種類型都由不同的系統能力撐起。

#### Type 1：資料前處理（Preprocessing）

**使用情境：** 在跑推論或訓練之前，先把原始文本 tokenize、格式轉換、資料清洗。

```bash
#!/bin/bash
#SBATCH --job-name=preprocess
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8    # 純 CPU，多 thread 平行處理
#SBATCH --mem=8G
#SBATCH --constraint=cpu
#SBATCH --output=/shared/jobs/%j-preprocess.out

python tokenize_dataset.py \
  --input  /shared/data/raw/ \
  --output /shared/data/tokenized/ \
  --workers 8
```

**分配流程：**
```
提交 → slurmctld 掃描 cpu-worker 帳本
     → slurm-worker-cpu-0 空閒，分配 8 cores
     → cgroup v2 將 pid 綁定到指定 cpuset
     → 與同時段的 GPU job 完全並行，互不干擾
     → 完成後輸出寫入 /shared/data/（NFS，所有 pod 可讀）
```

| 資源 | 宣告量 | 強制方式 |
|------|--------|---------|
| CPU | 8 cores | cgroup cpuset |
| RAM | 8 GB | cgroup memory |
| GPU | 無 | — |

> **展示的系統能力：** CPU pool 獨立 autoscale；CPU job 與 GPU job 不競爭資源；NFS 讓輸出跨節點共享。

---

#### Type 2：批次文字推論（Batch Inference with MPS）

**使用情境：** 對一批文件（1000 筆）跑模型推論，取得分類結果、摘要、或向量表示。多個推論 job 透過 MPS 共用同一張 GPU 的 SM。

```bash
#!/bin/bash
#SBATCH --job-name=batch-infer
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --gres=mps:25              # 請求 25% SM（RTX 4070 上 ≈ 12 SM）
#SBATCH --constraint=gpu-rtx4070
#SBATCH --output=/shared/jobs/%j-infer.out

python infer_batch.py \
  --model  /shared/models/bert-base/ \
  --input  /shared/data/tokenized/ \
  --output /shared/results/infer-$SLURM_JOB_ID/
```

**MPS 分配流程（4 個 job 同時跑）：**
```
Job 1（mps:25）+ Job 2（mps:25）+ Job 3（mps:25）+ Job 4（mps:25）
     ↓
RTX 4070 MPS Daemon（/tmp/nvidia-mps-0）
     ↓
48 SM 被 4 個 process 共享，每個各佔 12 SM
GPU SM utilization：4 × 12 = 48 SM（100% 滿載）
對比無 MPS 的串行：同樣 4 個 job 要花 4× 時間
```

| 資源 | 每個 job | 4 個 job 合計 |
|------|---------|-------------|
| GPU SM | 12 SM（25%） | 48 SM（100%） |
| GPU VRAM | 共享 12 GB（無隔離） | — |
| CPU | 2 cores | 8 cores |

> **展示的系統能力：** MPS 細粒度 GPU 分配；GPU utilization 從 ~20%（串行）提升到 ~80–100%（並行）；Operator 偵測 queue 後自動開啟 GPU worker。

---

#### Type 3：超參數搜尋（HPO with Job Array）

**使用情境：** 對同一個模型嘗試 8 組不同的 learning rate / batch size 組合，找出最佳設定。每組實驗是獨立的 sbatch job，透過 `--array` 並行提交。

```bash
#!/bin/bash
#SBATCH --job-name=hpo
#SBATCH --array=1-8               # 8 組實驗，Task ID 1–8
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --gres=mps:25             # 每個實驗佔 25% SM
#SBATCH --constraint=gpu-rtx4070
#SBATCH --output=/shared/jobs/%A-%a-hpo.out

# 從 Task ID 選參數
LR_LIST=(1e-5 2e-5 5e-5 1e-4 2e-4 5e-4 1e-3 2e-3)
LR=${LR_LIST[$((SLURM_ARRAY_TASK_ID - 1))]}

python train_experiment.py \
  --lr $LR \
  --output /shared/results/hpo-$SLURM_ARRAY_JOB_ID-$SLURM_ARRAY_TASK_ID/
```

**資源分配流程：**
```
提交 --array=1-8
     ↓
slurmctld 看到 8 個 pending job，各需要 mps:25
     ↓
Operator 偵測 pending count > 0 → 開啟 GPU worker pod
     ↓
RTX 4070 最多同時跑 4 個（4 × mps:25 = 100% SM）
     ↓
4 個跑完釋放 → 另外 4 個接著跑
     ↓
總時間 ≈ 2 輪 × 20 分鐘 = 40 分鐘
對比串行：8 × 20 分鐘 = 160 分鐘（縮短 4×）
```

> **展示的系統能力：** Job array 批次提交；MPS 讓多組實驗並行；Operator autoscale 依 queue 深度動態開 worker。

---

#### Type 4：LoRA Fine-tuning（GPU 獨佔 + Checkpoint 保護）

**使用情境：** 對預訓練模型做領域適應（fine-tuning），需要整張 GPU VRAM 和長時間運算，途中定期寫 checkpoint，確保意外中斷可以續跑。

```bash
#!/bin/bash
#SBATCH --job-name=finetune-lora
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=14G
#SBATCH --gres=gpu:rtx4080:1      # 獨佔整張 RTX 4080（16 GB VRAM）
#SBATCH --constraint=gpu-rtx4080
#SBATCH --output=/shared/jobs/%j-finetune.out

python finetune_lora.py \
  --model     /shared/models/llama-3.1-8b/ \
  --data      /shared/data/train.jsonl \
  --output    /shared/checkpoints/run-$SLURM_JOB_ID/ \
  --ckpt-every 500   # 每 500 steps 寫一次 checkpoint
```

**分配流程：**
```
提交 → slurmctld 查 GRES 帳本：gpu:rtx4080
     → slurm-worker-gpu-rtx4080-0 空閒，整卡分配
     → K8s device plugin 把 /dev/nvidia1 綁入 pod
     → CUDA_VISIBLE_DEVICES=0（pod 內）

縮容保護流程：
Operator 決策要縮容 GPU worker
     ↓
Checkpoint Guard：檢查 /shared/checkpoints/run-$JID/latest.ckpt
     → 若不存在或超過 MAX_CHECKPOINT_AGE（10 分鐘）→ 阻擋縮容
     → 存在且新鮮 → 允許縮容（等 job 完成）
```

| 資源 | 宣告量 | 說明 |
|------|--------|------|
| GPU VRAM | 16 GB（整卡） | 可跑 Llama 3.1 8B FP16 |
| GPU SM | 76 SM（100%） | 不與其他 job 共用 |
| CPU / RAM | 4 cores / 14 GB | 資料 loading / preprocessing |

> **展示的系統能力：** 整卡獨佔 GRES 分配；Checkpoint-aware 縮容保護；NFS PVC 讓 checkpoint 跨 pod 持久化。

---

#### Type 5：雙 GPU DDP 訓練

**使用情境：** 模型或 batch size 太大，單卡放不下，需要跨兩個 GPU worker 做梯度同步。每個 worker pod 各持一張 GPU，NCCL AllReduce 走 K8s pod 網路。

```bash
#!/bin/bash
#SBATCH --job-name=ddp-2gpu
#SBATCH --nodes=2                  # 2 個 Slurm worker pod
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=12G
#SBATCH --gres=gpu:1               # 不指定型號，各給 1 張
#SBATCH --output=/shared/jobs/%j-ddp.out

torchrun \
  --nproc_per_node=1 \
  --nnodes=2 \
  --node_rank=$SLURM_NODEID \
  --master_addr=$(scontrol show hostnames "$SLURM_NODELIST" | head -1) \
  --master_port=29500 \
  train_ddp.py \
    --data       /shared/data/tokenized/ \
    --checkpoint /shared/checkpoints/ddp-$SLURM_JOB_ID/
```

**分配流程：**
```
提交 --nodes=2 --gres=gpu:1
     ↓
slurmctld 找 2 台空閒 GPU worker
     → rank 0：slurm-worker-gpu-rtx4070-0（/dev/nvidia0，12 GB VRAM）
     → rank 1：slurm-worker-gpu-rtx4080-0（/dev/nvidia1，16 GB VRAM）
     ↓
torchrun 啟動，NCCL 透過 K8s pod network 建立 rendezvous
     ↓
AllReduce gradient 在兩個 rank 間同步（TCP backend）
     ↓
兩個 worker 同時寫 checkpoint 到 NFS /shared/checkpoints/
```

| 資源 | Rank 0（RTX 4070） | Rank 1（RTX 4080） |
|------|-------------------|-------------------|
| GPU VRAM | 12 GB | 16 GB |
| GPU SM | 48 SM | 76 SM |
| 通訊 | NCCL over TCP（K8s pod network） | 同左 |

> ⚠️ 混合 GPU 型號：batch size 以較小的 RTX 4070（12 GB）為準；RTX 4070 的 SM 數也是速度瓶頸。  
> **展示的系統能力：** 跨 worker pod 的多節點 Slurm 排程；NFS 讓兩個 worker 共讀 dataset；Checkpoint guard 保護長時間訓練不被縮容打斷。

### 資源帳本總覽（雙 GPU 單節點部署）

```
實體主機 (Linux + k3s)
├── CPU: 12 physical cores, 32 GB RAM
│   ├── slurm-controller-0         (系統服務 pod)
│   ├── slurm-worker-cpu-0         → Slurm 宣告 CPUs=4, Mem=3.4G
│   ├── slurm-worker-cpu-1         → 同上（operator 依需求開啟）
│   ├── slurm-worker-cpu-2         → 同上
│   ├── slurm-worker-cpu-3         → 同上
│   ├── slurm-worker-gpu-rtx4070-0 → CPUs=4, Mem=3.4G, gpu:rtx4070:1, mps:100
│   └── slurm-worker-gpu-rtx4080-0 → CPUs=4, Mem=3.4G, gpu:rtx4080:1, mps:100
│
├── GPU 0: RTX 4070 /dev/nvidia0  (12 GB VRAM, 48 SM)
│   ├── 整卡模式：slurm-worker-gpu-rtx4070-0 獨占
│   └── MPS 模式：nvidia-mps-daemon-0 管理，socket: /tmp/nvidia-mps-0
│                 ← worker pod 掛載此 socket，mps:N 分配 N% SM
│
└── GPU 1: RTX 4080 /dev/nvidia1  (16 GB VRAM, 76 SM)
    ├── 整卡模式：slurm-worker-gpu-rtx4080-0 獨占
    └── MPS 模式：nvidia-mps-daemon-1 管理，socket: /tmp/nvidia-mps-1
                  ← worker pod 掛載此 socket，mps:N 分配 N% SM
```

> **k3s vs Kind 差異：** k3s 直接使用 containerd + NVIDIA runtime，不需要 container-in-container；`/dev/nvidia0` 直接可見，`TaskPlugin=task/cgroup` 有真實 OS 隔離效果。Kind 在 Windows 下無法直通 GPU，所有 GPU 宣告為 `File=/dev/null`（排程帳本有效，實際不存在硬體）。

---

## CPU Job 分配

### 排程語意：同一 worker 可容納多個 job

`cons_tres` + `CR_Core` 使 Slurm 以 core 為單位消耗 CPU slot。同一台 node 的 CPU slot 可由多個 job 分攤，只要總需求不超過宣告量。這是 HPC 常見的 **bin-packing** 行為，無需 `OverSubscribe`。

> `OverSubscribe` 是讓多個 job「共用同一批 CPU」（超賣）；**bin-packing 是不同 job 用不同 CPU slot**，兩個概念不同。

### 分配範例（CPUs=4 的 cpu-worker）

| Job | 請求 | 分配在同一 worker？ | 說明 |
|-----|------|-------------------|------|
| A `--cpus-per-task=2` | 2 cores | ✅ | 佔用 slot 0-1 |
| B `--cpus-per-task=2` | 2 cores | ✅ 與 A 共存 | 佔用 slot 2-3，worker 滿載 |
| C `--cpus-per-task=1` | 1 core | ❌ 排到其他 worker | worker 已滿，Slurm 等待或排第二台 |

更複雜的例子——4 個 job 競搶 2 台 worker（各 CPUs=4）：

```
Worker-0 [4 slots]         Worker-1 [4 slots]
┌─────────────────┐        ┌─────────────────┐
│ Job A (2 cores) │        │ Job C (3 cores) │
│ Job B (2 cores) │        │ Job D (1 core)  │
└─────────────────┘        └─────────────────┘
  bin-packed: 100%            bin-packed: 100%
```

Job A、B 同時跑在 Worker-0；Job C、D 同時跑在 Worker-1。Slurm 的 backfill scheduler 會盡量填滿 slot。

### 邊界：隔離層缺失

`TaskPlugin=task/none` + `ProctrackType=proctrack/linuxproc` 表示：

- Slurm **記帳**說分給 Job A 2 cores，但 OS 不會強制 Job A 只跑在那 2 顆上
- 若應用程式內部開 `OMP_NUM_THREADS=16`，它可能吃掉 Worker 上所有 CPU，影響同 worker 的 Job B
- **效能隔離在 Kind 環境是虛的**，僅排程語意層面有效

真實 HPC 環境通常改用 `TaskPlugin=task/cgroup`，搭配 `CgroupAutomount=yes`，OS 層級強制 CPU pinning。

---

## GPU Job 分配

### 預設模型：整卡獨占

每台 GPU worker 宣告 1 個 GRES slot（如 `Gres=gpu:rtx4070:1`）。GRES 是整數消耗，無分數分配：

```
# Linux + k3s + RTX 4070 環境（maxNodes=1，只有 1 台 GPU worker）
Job A: --gres=gpu:rtx4070:1  →  佔用整張 RTX 4070，該 worker GRES=0
Job B: --gres=gpu:rtx4070:1  →  Pending，等 Job A 釋放（因為只有 1 台 GPU worker）
```

### 分配範例

**場景 A：Linux + k3s + 1 台 RTX 4070 worker（實際部署）**

| Job | 請求 | 分配結果 |
|-----|------|---------|
| A `--gres=gpu:rtx4070:1 -N 1` | 1× RTX 4070 | → worker-gpu-rtx4070-0，整張 12 GB 獨占 |
| B `--gres=gpu:rtx4070:1 -N 1` | 1× RTX 4070 | → Pending，只有 1 台 GPU worker，等 A 結束 |

**場景 B：假設未來多張 GPU 機器（可參考設計）**

| Job | 請求 | 分配結果 |
|-----|------|---------|
| A `--gres=gpu:rtx4070:1 -N 1` | 1× GPU | → worker-gpu-rtx4070-0，整張獨占 |
| B `--gres=gpu:rtx4070:1 -N 1` | 1× GPU | → worker-gpu-rtx4070-1，整張獨占 |
| C `--gres=gpu:rtx4070:1 -N 1` | 1× GPU | → Pending，等 A 或 B 釋放 |

多節點 DDP job（多 GPU worker 情境）：

```bash
#SBATCH --gres=gpu:rtx4070:1
#SBATCH -N 4          # 需要 4 台 GPU worker 同時空閒
#SBATCH --ntasks-per-node=1
```

Slurm 要求 4 台 `gpu-rtx4070` worker 同時空閒。這正是 Gang Scheduling 解決的問題——若只有 3 台空閒，K8s 1.35 原生 `GangScheduling` 會讓 4 個 worker Pod 要嘛全部調度，要嘛全不調度，避免佔著資源等人。

### GPU 共用機制（進階）

若要讓多個 job 共用同一張 GPU，需要額外機制：

#### Time-Slicing（時間切片）

CUDA context 輪流使用 GPU，類似 CPU 分時多工。

- 適用：**所有 NVIDIA GPU**
- 隔離：**無記憶體隔離**（所有 context 共享 VRAM），context switch 有開銷
- K8s 設定：GPU Operator ConfigMap 把 1 張 GPU 虛擬成 N 份 `nvidia.com/gpu`

```yaml
# ConfigMap：1 張 RTX 4070 虛擬成 4 份（需 GPU Operator，本架構未使用）
sharing:
  timeSlicing:
    resources:
    - name: nvidia.com/gpu
      replicas: 4
```

```ini
# gres.conf（Slurm 端）
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 Count=4 File=/dev/nvidia0
```

4 個 job 可同時各請求 `--gres=gpu:rtx4070:1`，時間輪流使用同一張 GPU。**不適合 DDP 訓練**（延遲不可預測、無記憶體保護）。

#### MPS（Multi-Process Service）

多個 CUDA process 合併進同一 CUDA context，共享 command queue 和 SM，減少 context switch 開銷。SM 可設 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 比例。適合延遲敏感的小型推論，但無完整記憶體隔離。

---

### GPU MPS 完整實作指南（2026）

#### 本架構環境：Linux + k3s + RTX 4070 + RTX 4080（雙 GPU）

> **本架構採用路徑 B（Slurm MPS DaemonSet）**，因為我們使用 k3s + 直接安裝 NVIDIA Container Toolkit，**未部署 NVIDIA GPU Operator**。路徑 A 需要 GPU Operator，在 k3s 上需額外安裝，非必要複雜度。

| 項目 | GPU 0：RTX 4070 | GPU 1：RTX 4080 |
|------|----------------|----------------|
| 架構 | Blackwell GB203 | Ada Lovelace AD103 |
| SM 數量 | 48 SM | 76 SM |
| VRAM | 12 GB GDDR7 | 16 GB GDDR6X |
| 裝置路徑 | `/dev/nvidia0` | `/dev/nvidia1` |
| MPS socket 目錄 | `/tmp/nvidia-mps-0` | `/tmp/nvidia-mps-1` |
| MPS 支援 | ✅（Volta+） | ✅（Volta+） |
| MIG 支援 | ❌ | ❌ |
| K8s Runtime | k3s + containerd + NVIDIA Container Toolkit（共用） | ← |

---

#### MPS 運作原理 vs Time-Slicing

| 維度 | Time-Slicing | MPS | MIG |
|------|-------------|-----|-----|
| 機制 | 多個 CUDA context 輪流使用 GPU（OS 時間片） | 所有 process 共用**同一個** CUDA context，SM 並行執行 | 硬體切分 GPU 為獨立 instance |
| 並行度 | 無（序列執行） | **高**（多 process 真正同時跑在 SM 上） | 有（instance 間獨立） |
| 記憶體隔離 | ❌（VRAM 共享） | ❌（一個 OOM 可能拖垮其他 process） | ✅（各 instance VRAM 隔離） |
| context switch overhead | 高（µs 級別） | **極低**（無 context switch） | 無（instance 獨立） |
| SM 配額控制 | ❌ | ✅ `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` | ✅（profile 固定） |
| 支援 GPU | 全部 NVIDIA | **Volta+（含 RTX 4070）** | A100/H100/A30 only |
| RTX 4070 適用 | ✅ | **✅ 本架構採用** | ❌ 不支援 |
| 最佳場景 | 開發/測試 | **推論服務多 replica 共用 GPU** | 多租戶需記憶體隔離 |

**雙 GPU + MPS 適合的場景：**
- 多個小型推論 server 並排（LLM serving、image classifier），兩張 GPU 分流不同服務
- 批次推論（offline batch inference），task 間共享 SM bandwidth
- 教授展示：同一張 GPU 跑多個 AI 服務而不互相等待；兩張 GPU 同時跑不同服務

**不適合的場景：**
- PyTorch DDP 訓練（DDP 本身已充分利用整張 GPU，加 MPS 只增加風險；DDP 請用整卡模式）
- 需要記憶體隔離的多租戶（兩張卡均無 MIG，MPS 沒有 VRAM 隔離）
- 不同 Linux 用戶共用同一張 GPU（每個 Linux 用戶只能有一個 MPS server）

---

#### 實作架構：雙 GPU MPS Control Daemon on k3s

每張 GPU 需要獨立的 MPS daemon process，使用不同的 socket 目錄。本架構部署**兩個 MPS daemon Pod**（各管一張 GPU）。

```
Linux Host（k3s 單節點）
┌──────────────────────────────────────────────────────────────────┐
│  namespace: slurm                                                 │
│                                                                   │
│  ┌───────────────────────┐    ┌───────────────────────┐          │
│  │ nvidia-mps-daemon-0   │    │ nvidia-mps-daemon-1   │          │
│  │ CUDA_VISIBLE_DEVICES=0│    │ CUDA_VISIBLE_DEVICES=1│          │
│  │ nvidia-cuda-mps-ctl -d│    │ nvidia-cuda-mps-ctl -d│          │
│  └──────────┬────────────┘    └──────────┬────────────┘          │
│             │ socket                     │ socket                 │
│    /tmp/nvidia-mps-0/           /tmp/nvidia-mps-1/               │
│             │                            │                        │
│  ┌──────────┴──────────┐    ┌────────────┴────────────┐          │
│  │ worker-gpu-rtx4070  │    │  worker-gpu-rtx4080     │          │
│  │ Job A (mps:50=24SM) │    │  Job D (mps:50=38SM)    │          │
│  │ Job B (mps:25=12SM) │    │  (4080 單獨或共用)       │          │
│  │ Job C (mps:25=12SM) │    └─────────────────────────┘          │
│  └─────────────────────┘                                         │
│                                                                   │
│  /dev/nvidia0  RTX 4070 (12 GB, 48 SM)                           │
│  /dev/nvidia1  RTX 4080 (16 GB, 76 SM)                           │
└──────────────────────────────────────────────────────────────────┘
```

各 worker pod 掛載對應 GPU 的 MPS socket 目錄，CUDA 呼叫由該 GPU 的 MPS Server 代理。兩張 GPU 的 MPS daemon 完全獨立，互不干擾。

---

#### 路徑 B 實作步驟（本架構採用，Linux + k3s + RTX 4070）

**前提：** 已執行 `K8S_RUNTIME=k3s REAL_GPU=true bash scripts/bootstrap.sh` 完成基礎部署。

---

**步驟一：確認 NVIDIA 環境（兩張 GPU）**

```bash
# 確認 host 看到兩張 GPU
nvidia-smi --list-gpus
# 期望輸出：
#   GPU 0: NVIDIA GeForce RTX 4070  (UUID: ...)
#   GPU 1: NVIDIA GeForce RTX 4080  (UUID: ...)

# 確認 K8s 看到 GPU 資源
kubectl get nodes -o custom-columns='NODE:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
# 期望輸出：GPU 欄位為 "2"（device plugin 計算所有 GPU）
```

---

**步驟二：部署雙 GPU MPS DaemonSet**

雙 GPU 需要兩個 MPS daemon 分別管理，各自設定 `CUDA_VISIBLE_DEVICES` 和不同的 socket 目錄。`manifests/gpu/mps-daemonset.yaml` 的 DaemonSet pod 請求 `nvidia.com/gpu: 2` 並在同一個 container 啟動兩個 daemon process：

```yaml
# mps-daemonset.yaml（雙 GPU 版本）關鍵部分
containers:
- name: mps-control
  resources:
    limits:
      nvidia.com/gpu: "2"   # 佔用兩張 GPU
  env:
  - name: CUDA_MPS_PIPE_DIRECTORY_0
    value: /tmp/nvidia-mps-0
  - name: CUDA_MPS_PIPE_DIRECTORY_1
    value: /tmp/nvidia-mps-1
  command:
  - /bin/bash
  - -c
  - |
    set -e
    mkdir -p /tmp/nvidia-mps-0 /tmp/nvidia-mps-1 \
             /tmp/nvidia-mps-log-0 /tmp/nvidia-mps-log-1
    # GPU 0 (RTX 4070)
    CUDA_VISIBLE_DEVICES=0 \
    CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps-0 \
    CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-mps-log-0 \
    nvidia-cuda-mps-control -d
    # GPU 1 (RTX 4080)
    CUDA_VISIBLE_DEVICES=1 \
    CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps-1 \
    CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-mps-log-1 \
    nvidia-cuda-mps-control -d
    echo "[mps] both MPS daemons started"
    # 保活 + 健康檢查
    while true; do
      pgrep -x nvidia-cuda-mps- >/dev/null || {
        echo "[mps] daemon died, restarting..." >&2
        CUDA_VISIBLE_DEVICES=0 CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps-0 \
          nvidia-cuda-mps-control -d 2>/dev/null || true
        CUDA_VISIBLE_DEVICES=1 CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps-1 \
          nvidia-cuda-mps-control -d 2>/dev/null || true
      }
      sleep 30
    done
volumeMounts:
- name: mps-socket-0
  mountPath: /tmp/nvidia-mps-0
- name: mps-socket-1
  mountPath: /tmp/nvidia-mps-1
volumes:
- name: mps-socket-0
  hostPath:
    path: /tmp/nvidia-mps-0
    type: DirectoryOrCreate
- name: mps-socket-1
  hostPath:
    path: /tmp/nvidia-mps-1
    type: DirectoryOrCreate
```

```bash
# 部署並驗證
kubectl apply -f manifests/gpu/mps-daemonset.yaml
kubectl -n slurm rollout status daemonset/nvidia-mps-daemon

mps_pod=$(kubectl -n slurm get pod -l app=nvidia-mps-daemon -o jsonpath='{.items[0].metadata.name}')
# 驗證兩個 MPS daemon 都回應
kubectl -n slurm exec "pod/${mps_pod}" -- bash -c '
  echo "=== GPU 0 (RTX 4070) ==="
  CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps-0 \
    echo get_server_list | nvidia-cuda-mps-control
  echo "=== GPU 1 (RTX 4080) ==="
  CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps-1 \
    echo get_server_list | nvidia-cuda-mps-control
'
```

---

**步驟三：重新 render 帶 MPS mount 的 manifests**

```bash
K8S_RUNTIME=k3s REAL_GPU=true WITH_MPS=true bash scripts/bootstrap.sh
```

`render-core.py --with-mps` 會在 GPU worker StatefulSet 自動加入。**雙 GPU 版本需根據 GPU 型號掛載不同 socket 目錄**：

```yaml
# RTX 4070 worker StatefulSet（由 render-core.py 注入）
spec:
  hostIPC: true
  containers:
  - name: worker
    volumeMounts:
    - name: mps-socket
      mountPath: /tmp/nvidia-mps        # 統一掛載點（pod 內看到的路徑）
    env:
    - name: CUDA_MPS_PIPE_DIRECTORY
      value: /tmp/nvidia-mps
  volumes:
  - name: mps-socket
    hostPath:
      path: /tmp/nvidia-mps-0           # ← GPU 0 的 socket
      type: DirectoryOrCreate
```

```yaml
# RTX 4080 worker StatefulSet（GPU 1 的 socket）
volumes:
- name: mps-socket
  hostPath:
    path: /tmp/nvidia-mps-1             # ← GPU 1 的 socket
    type: DirectoryOrCreate
```

> ⚠️ `hostIPC: true` 讓 pod 存取 host IPC namespace，已受 NetworkPolicy 限制只有 GPU worker pod 才有此設定。

---

**步驟四：gres.conf 設定 MPS slot（RTX 4070）**

在 `slurm.conf`（由 `render-core.py` 生成）加入 MPS GresType：

```ini
# slurm.conf（render-core.py 生成，REAL_GPU=true）
GresTypes=gpu,mps
TaskPlugin=task/cgroup
CgroupPlugin=cgroup/v2
```

```ini
# gres.conf — 雙 GPU 宣告，各自有 GPU 和 MPS 兩個 GRES 類型
# RTX 4070（GPU 0）
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 File=/dev/nvidia0 Count=1
NodeName=slurm-worker-gpu-rtx4070-0 Name=mps Count=100   # 100% = 48 SM

# RTX 4080（GPU 1）
NodeName=slurm-worker-gpu-rtx4080-0 Name=gpu Type=rtx4080 File=/dev/nvidia1 Count=1
NodeName=slurm-worker-gpu-rtx4080-0 Name=mps Count=100   # 100% = 76 SM
```

`mps:N` 代表 N% 的該卡 SM，兩張卡的 SM 數量不同：

| 請求 + constraint | RTX 4070 分配 SM | RTX 4080 分配 SM | 適合工作 |
|-----------------|----------------|----------------|---------|
| `--gres=mps:50` | ~24 SM (50%) | ~38 SM (50%) | 中型推論（7B LLM serving） |
| `--gres=mps:25` | ~12 SM (25%) | ~19 SM (25%) | 小型推論（image classifier） |
| `--gres=mps:10` | ~5 SM (10%)  | ~8 SM (10%)  | 極輕量 embedding service |
| `--gres=gpu:rtx4070:1` | 48 SM（整卡） | — | 訓練（需 ≤12 GB VRAM）|
| `--gres=gpu:rtx4080:1` | — | 76 SM（整卡） | 訓練（需 ≤16 GB VRAM）|
| `-N 2 --gres=gpu:1` | 48 SM | 76 SM | **2-GPU DDP**（兩 rank） |

---

**步驟五：Prolog/Epilog 腳本（可選，精細 SM 控制）**

若希望每個 job 嚴格限制 SM 百分比（掛入 worker container via ConfigMap）：

```bash
# /etc/slurm/prolog.d/99-mps.sh
#!/bin/bash
[[ "${SLURM_JOB_GRES:-}" != *mps* ]] && exit 0
export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
export CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-mps-log
mps_count=$(echo "$SLURM_JOB_GRES" | grep -oP 'mps:\K[0-9]+' || echo 100)
echo "set_active_thread_percentage ${mps_count}" | nvidia-cuda-mps-control
```

```bash
# /etc/slurm/epilog.d/99-mps.sh
#!/bin/bash
[[ "${SLURM_JOB_GRES:-}" != *mps* ]] && exit 0
export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
echo quit | nvidia-cuda-mps-control 2>/dev/null || true
```

---

**步驟六：Job 提交語法**

```bash
# MPS 推論 job：明確指定哪張 GPU
# 在 RTX 4070 上（48 SM，12 GB VRAM）
#SBATCH --job-name=infer-rtx4070
#SBATCH --gres=mps:25             # 25% = ~12 SM
#SBATCH --constraint=gpu-rtx4070
#SBATCH --mem=1G
python infer.py --model small_model

# 在 RTX 4080 上（76 SM，16 GB VRAM，適合較大模型）
#SBATCH --job-name=infer-rtx4080
#SBATCH --gres=mps:25             # 25% = ~19 SM（比 5070 多）
#SBATCH --constraint=gpu-rtx4080
#SBATCH --mem=2G
python infer.py --model large_model

# 整卡訓練（不使用 MPS）
#SBATCH --job-name=train
#SBATCH --gres=gpu:rtx4080:1      # 16 GB VRAM，訓練首選
#SBATCH --mem=3G
torchrun --nproc_per_node=1 train.py

# 2-GPU DDP（兩 rank，各持 1 張卡）
#SBATCH --job-name=ddp-2gpu
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1              # 每個 node 1 張，Slurm 自動分配
torchrun --nproc_per_node=1 --nnodes=2 --node_rank=$SLURM_NODEID \
  --master_addr=$(scontrol show hostnames "$SLURM_NODELIST" | head -1) \
  --master_port=29500 train.py
```

---

**步驟七：驗證**

```bash
# 完整 GPU + MPS 驗證（5 步驟）
bash scripts/verify-gpu.sh

# 手動確認 MPS 狀態
mps_pod=$(kubectl -n slurm get pod -l app=nvidia-mps-daemon -o jsonpath='{.items[0].metadata.name}')
kubectl -n slurm exec "pod/${mps_pod}" -- bash -c '
  echo get_server_list | nvidia-cuda-mps-control
  echo get_client_list | nvidia-cuda-mps-control
'
# 查看 SM 使用率（MPS 下多 job 應同時可見）
kubectl -n slurm exec "pod/${mps_pod}" -- nvidia-smi dmon -s u -d 2
```

---

#### 路徑 A：GPU Operator MPS（參考，本架構未使用）

**適用：** 部署了 NVIDIA GPU Operator 的正式叢集（需要 ClusterPolicy CRD）。

```yaml
# GPU Operator ConfigMap：1 張 RTX 4070 虛擬成 4 個 MPS slot
data:
  any: |
    sharing:
      mps:
        resources:
        - name: nvidia.com/gpu
          replicas: 4
```

```bash
kubectl patch clusterpolicies.nvidia.com/cluster-policy \
  -n gpu-operator --type merge \
  -p '{"spec": {"devicePlugin": {"config": {"name": "mps-config", "default": "any"}}}}'
```

```ini
# gres.conf（對應 replicas: 4）
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 Count=4 File=/dev/nvidia0
```

---

#### 本架構實作建議

| 部署環境 | 推薦路徑 | 理由 |
|---------|---------|------|
| **Linux + k3s + RTX 4070 + RTX 4080（本架構）** | **路徑 B（雙 MPS DaemonSet）** | 無 GPU Operator；兩張卡各一個 daemon，socket 目錄分開 |
| Kind（Windows 開發） | ❌ 不適用 | Kind 無真實 GPU，hostIPC 無效 |
| 部署了 GPU Operator 的正式叢集 | 路徑 A（GPU Operator MPS） | GPU Operator 處理 daemon 生命週期 |
| 大型 HPC 叢集（多租戶精細 SM 控制） | 路徑 B + Prolog/Epilog | 可按 job 動態調整 SM 百分比 |
| 混合推論+訓練叢集（雙卡） | 路徑 B + 分 partition | RTX 4080 作訓練 partition，RTX 4070 作 MPS 推論 partition |

**對 operator/main.py 的影響：**

兩個 GPU pool 各自獨立擴縮，`PARTITIONS_JSON` 需包含兩個 pool：

```json
[
  {
    "name": "slurm-worker-gpu-rtx4070",
    "match_gres": "gpu:rtx4070",
    "gres_per_node": "gpu:rtx4070:1,mps:100",
    "maxNodes": 1
  },
  {
    "name": "slurm-worker-gpu-rtx4080",
    "match_gres": "gpu:rtx4080",
    "gres_per_node": "gpu:rtx4080:1,mps:100",
    "maxNodes": 1
  }
]
```

兩張 GPU 各只有 1 張（maxNodes=1），operator 不會 scale-up GPU pool（已有節點），主要幫 CPU pool 做擴縮。縮放邏輯不需修改。

**對 Slurm 設定的影響（render-core.py）：**

```python
# render-core.py 的 gres.conf 生成邏輯（REAL_GPU=true, WITH_MPS=true）
# 每個 GPU pool 的 device index 由 worker-pools.json 的 gpuIndex 決定
device_path = f"/dev/nvidia{pool.get('gpuIndex', 0)}"
gres_lines.append(
    f"NodeName={node_name} Name=gpu Type={gpu_type} File={device_path} Count=1"
)
if args.with_mps:
    # socket 目錄根據 GPU index 區分
    gpu_idx = pool.get('gpuIndex', 0)
    gres_lines.append(f"NodeName={node_name} Name=mps Count=100")
    # 同時在 StatefulSet volume 使用 /tmp/nvidia-mps-{gpu_idx}
```

---

#### 監控 MPS 使用狀況

```bash
# 在 MPS Daemon 所在 pod 或 host 執行
echo "get_server_list" | nvidia-cuda-mps-control    # 查看活躍 server
echo "get_client_list" | nvidia-cuda-mps-control    # 查看連線的 client

# 查看 GPU 利用率（多個 MPS process 應能同時看到 SM 使用）
nvidia-smi dmon -s u

# 用 DCGM 追蹤 MPS 下的 per-process GPU 使用（需 DCGM Exporter）
dcgmi group -c mps-jobs --default
dcgmi dmon -e 203,204,1002   # SM Active, SM Occupancy, Memory Active
```

---

#### 參考來源

- [Slurm GRES MPS 官方文件](https://slurm.schedmd.com/gres.html) — `GresTypes=gpu,mps` 設定與 Prolog 行為
- [GPU Slicing in CycleCloud Slurm with CUDA MPS](https://techcommunity.microsoft.com/blog/azurehighperformancecomputingblog/gpu-slicing-in-cyclecloud-slurm-with-cuda-multi-process-service-mps/4365999) — Microsoft Azure HPC，Slurm Prolog/Epilog 實戰
- [GKE MPS 實作](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nvidia-mps-gpus) — `hostIPC: true` 要求和 Google 自訂 GPU stack
- [SURF MPS for Slurm GitHub](https://github.com/basvandervlies/surf_slurm_mps) — 荷蘭國家超算中心的 MPS Prolog/Epilog 完整實作
- [NVIDIA GPU Operator MPS 文件](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html) — ClusterPolicy ConfigMap `sharing.mps` 設定
- [MIG vs Time-Slicing vs MPS 比較](https://www.kubenatives.com/p/mig-vs-time-slicing-vs-mps-which) — 三種機制的適用場景分析

### 核心限制：為何不能跨 GPU 分割 SM？

用戶場景：GPU0 剩 4 SM 閒置、GPU1 剩 4 SM 閒置，Job C 需要 8 SM，能否合用？

**直接答案：不可能。** 硬體架構根本限制：

```
  GPU0 [SM0..SM107]                  GPU1 [SM0..SM107]
┌──────────────────┐               ┌──────────────────┐
│  SM0  SM1  ...   │               │  SM0  SM1  ...   │
│  L2 Cache        │               │  L2 Cache        │
│  HBM (80 GB)     │<─PCIe/NVLink─>│  HBM (80 GB)     │
└──────────────────┘               └──────────────────┘
```

1. **無跨 GPU 共享記憶體**：CUDA thread block 必須在同一張 GPU 的 SM 上存取 Shared Memory / L1 cache；跨 GPU 只能用 P2P copy（NVLink/PCIe），延遲是 SM 內 shared memory 的 100 倍以上
2. **CUDA 程式模型不支援**：沒有 API 可讓一個 kernel 跑在「GPU0 SM 0-3 + GPU1 SM 0-3」上
3. MIG / Time-Slicing / MPS 都是**單一 GPU 內**的分割，無法橫跨兩張

「用完碎片化 GPU 資源」的正確方法：

| 場景 | 解法 | 支援 GPU |
|------|------|---------|
| GPU0 有 25% 閒置，想跑小 job | Time-Slicing 或 MIG `1g.10gb` | 全部 / 僅 A100,H100 |
| 多推論任務共用一張 GPU | MPS 或 time-slicing | Volta+ |
| 多租戶需記憶體隔離 | MIG | 僅 A100,H100,A30 |
| DDP 大型訓練 | 整張 GPU（1 job per GPU）| 全部 |
| 跨 GPU SM 碎片整合 | ❌ 硬體不支援 | 無 |

---

## AI/HPC Infra Review

### 實作設計是否妥當？

| 面向 | 評估 | 說明 |
|------|------|------|
| CPU consumable resource 模型 | ✅ 正確 | `cons_tres` + `CR_Core` 是 HPC 業界標準做法 |
| GPU 整卡獨占預設 | ✅ 合理 | DDP 訓練場景下整卡獨占是正確的起點 |
| `TaskPlugin=task/none` | ⚠️ 可接受於模擬 | Kind 環境做驗證可以，**不能用於效能測試或多租戶** |
| GPU GRES `File=/dev/null` | ✅ Kind 環境唯一可行方案 | 排程邏輯可驗證，硬體部分需真實環境補齊 |
| 無 GPU sharing 機制 | ✅ DDP 場景下正確 | DDP 不應 time-slice；加 GPU sharing 反而造成干擾 |

**整體評語：** 以驗證排程控制流為目標，目前設計選擇是合理的。主要缺口在隔離層（`task/none`）和 Kind GPU 模擬的不完整性，這兩個缺口在設計文件中已有明確說明，不是未知風險。

### Kind 環境 vs 真實環境差距

| 項目 | Kind 環境 | 真實 GPU 叢集 |
|------|----------|-------------|
| GPU 裝置 | `/dev/null`（純排程帳本） | NVIDIA device plugin，實際 GPU 分配 |
| CPU 隔離 | 無（task/none） | task/cgroup + cpuset 強制 binding |
| GPU sharing | 不可用 | Time-Slicing / MIG / MPS（按需啟用）|
| 記憶體限制 | Slurm 記帳但不強制 | cgroup v2 memory.max 強制 OOM |
| NCCL / collective | CPU 模擬 | NVLink / RDMA InfiniBand |

### 若要部署真實環境，建議的改動順序

1. 換掉 `TaskPlugin=task/none` → `task/cgroup`，配合 `CgroupPlugin=cgroup/v2`
2. 部署 NVIDIA GPU Operator，device plugin 取代 `/dev/null` 模擬
3. 視 GPU 型號選 sharing 策略：A100/H100 → MIG；一般推論服務 → time-slicing
4. Slurm `gres.conf` 對齊實際 MIG partition，`File=` 指向真實 `/dev/nvidia*`
5. Gang Scheduling → 啟用 K8s 1.35 `GangScheduling` feature gate（已完成）

---

## 快速查詢表

| 問題 | 答案 |
|------|------|
| Job 能指定 CPU 數量？ | ✅ `--cpus-per-task`、`--ntasks` |
| Job 能指定 GPU 型號和數量？ | ✅ `--gres=gpu:a10:1` |
| 同一 CPU worker 能跑多個 job？ | ✅ 排程層（`CR_Core` bin-packing） |
| CPU 有實體隔離？ | ❌ `task/none`，無 binding/cgroup（Kind 限制） |
| 同一張 GPU 能讓多個 job 共用？ | ✅ 可透過 time-slicing 或 MIG（需真實 GPU + 額外設定） |
| GPU core 能跨多張 GPU 分割給同一 job？ | ❌ CUDA 硬體架構根本限制 |
| Kind 環境的 GPU GRES 是真實硬體？ | ❌ `File=/dev/null`，純排程模擬 |

---

# Phase 5-B：Linux + k3s 真實 GPU 驗證 Debug 紀錄（2026-04-27）

這一輪的目標是把整個 migration.md 的 Linux+k3s 路徑實際跑起來，並驗證 GPU、MPS、NFS 共享儲存都能正常運作。以下是踩到的每個坑與修法。

---

## 坑 1：NFS Server 的 export 沒涵蓋 LAN 介面 IP

**現象**

```
Warning  FailedMount  ...  MountVolume.SetUp failed for volume "nfs-client-root":
  mount failed: exit status 32
  mount.nfs: access denied by server while mounting 192.168.0.111:/srv/nfs/k8s
```

`nfs-subdir-external-provisioner` Pod 卡在 `ContainerCreating` 永遠無法啟動。

**根本原因**

`setup-nfs-server.sh` 只設定了：

```
/srv/nfs/k8s 10.0.0.0/8(rw,sync,no_subtree_check,no_root_squash,insecure)
```

k3s 以 native（非 VM）方式跑在主機上，Pod volume 的 NFS mount 是由 **kubelet（節點）** 執行，不是由 Pod 內部發起。節點的 LAN IP 是 `192.168.0.111`（`enp5s0`，在 `192.168.0.0/25`），不在 `10.0.0.0/8` 內。

**修法**

```bash
# /etc/exports 加上 LAN subnet：
/srv/nfs/k8s 10.0.0.0/8(rw,sync,...) 192.168.0.0/25(rw,sync,no_subtree_check,no_root_squash,insecure)
sudo exportfs -ra
```

**教訓**

Docker/Kind 環境的容器網段通常是 `172.x` 或 `10.x`，所以舊的 export 規則剛好夠用。k3s native 部署時節點 IP 就是主機的物理 IP，必須額外加入。

---

## 坑 2：GPU job 卡在 COMPLETING 永不結束

**現象**

```
[21:34:15] job 16 state=COMPLETING
[21:35:33] job 16 state=COMPLETING
WARN: timed out waiting for GPU job 16
FAIL: GPU job did not complete (state=COMPLETING)
```

job 的 ExitCode 已經是 `0:0`（batch script 確實跑完），但 slurmctld 一直等 epilog-done 訊號。

**根本原因**

COMPLETING 在 Slurm 裡代表「batch script 結束，等 slurmstepd 回報 epilog 完成」。GPU worker 是 elastic pool（用完就縮容），operator 把 Pod evict 掉後，preStop hook 設了 `state=drain`，接著 kubelet 殺掉 Pod。slurmstepd 跟著消失，slurmctld 收不到 epilog-done，job 就永遠卡在 COMPLETING。

**修法**

在 `scripts/render-core.py` 的 `slurm.conf` 加一行：

```python
"CompleteWait=0",
```

`CompleteWait=0` 讓 slurmctld 在 batch script 結束後不等 epilog-done，直接把 job 轉到 COMPLETED。Worker Pod 沒有任何 epilog script，這個設定完全安全。

**復現方式驗證**

加入後，job 在 RUNNING → COMPLETED 只需幾秒，不再卡住。

---

## 坑 3：verify-gpu.sh 的 job output 讀不到

**現象**

```
(no output file on submit pod — no shared storage)
ExitCode=0:0  TresPerNode=gres:gpu:rtx4070:1
PASS: GPU job ExitCode=0:0 and GPU GRES allocated ...
```

job 有跑完，但看不到 `nvidia-smi` 的實際輸出。

**根本原因**

原本的 sbatch script 是：

```bash
#SBATCH --output=/tmp/gpu-verify-%j.out
```

output 寫在 GPU worker Pod 的 `/tmp`。GPU worker Pod 跑完 job 後被 operator 縮容，Pod 消失，login pod 去讀這個 `/tmp` 路徑當然讀不到。

**修法**

改把 output 寫到 NFS 共享的 `/shared`：

```bash
JOB_OUT_DIR="${SHARED_DIR}/gpu-verify-$$"   # $$=verify腳本的PID，唯一目錄
# 在 sbatch submission 前先在 login pod 建立該目錄
mkdir -p '${JOB_OUT_DIR}'
# sbatch 腳本內：
#SBATCH --output=${JOB_OUT_DIR}/%j.out
```

所有 Pod（controller、worker、login）都 mount 同一個 `slurm-shared-rwx` PVC，job output 寫進去後 login pod 一定讀得到。

---

## 坑 4：verify.sh GPU job 丟到 cpu partition 導致立刻 fail

**現象**

```
sbatch: error: Batch job submission failed: Requested node configuration is not available
```

**根本原因**

`verify.sh` 有：

```bash
PARTITION=${PARTITION:-cpu}
```

GPU job 的 sbatch script 用了 `#SBATCH -p ${PARTITION}`，結果是 `-p cpu`，cpu partition 根本沒有 `--gres=gpu:rtx4070:1` 的節點，Slurm 立刻拒絕。

**修法**

加一個獨立的 `GPU_PARTITION` 變數，GPU job 改用它：

```bash
GPU_PARTITION=${GPU_PARTITION:-gpu-rtx4070}
# sbatch 腳本改為：
#SBATCH -p ${GPU_PARTITION}
```

---

## 坑 5：DRAIN 狀態導致 sbatch 立刻被拒

**現象**

```
sbatch: error: Batch job submission failed: Requested node configuration is not available
```

（跟坑 4 的錯誤訊息一模一樣，但原因不同。）

**根本原因**

preStop hook（`scontrol update nodename=$(hostname) state=drain reason=k8s-eviction`）在每次 Pod 縮容時都會被觸發，DRAIN 狀態會在 slurmctld 的 StateSaveLocation 裡持久化。

下一次 sbatch 提交時：
- GPU worker Pod 已縮容，replicas=0
- slurmctld 裡所有 GPU node 都是 `drained*`
- Slurm 直接拒絕投遞（DRAINED ≠ DOWN，不會排到 queue 等節點恢復）

Worker Pod 啟動時雖然有 `scontrol update nodename=$(hostname) state=resume`（坑 6 的修法），但 Pod 還沒啟動就已經 sbatch 失敗了。

**修法**

在 verify.sh GPU job 提交前，先 resume 所有 drained 的節點：

```bash
login_exec "sinfo -t drain,drained -N --noheader -o '%N' 2>/dev/null \
  | xargs -r -I{} scontrol update nodename={} state=resume 2>/dev/null" || true
```

這樣 GPU 節點變成 `idle*`（slurmd 未連線但不是 DRAIN），sbatch 成功送出 PENDING，operator 看到 PENDING job 後 scale up，Pod 啟動後 slurmd 重新 register，job 才能 dispatch。

**為何不用 `awk` 解析 `sinfo --Format=NodeList,StateLong`？**

`sinfo --Format` 的欄位是固定寬度，節點名稱過長時會截斷，截斷後與 state 欄位黏在一起，awk `$2` 抓不到正確 state。改用 `-o '%N'` 配合 `-t drain,drained` 過濾才能拿到完整節點名稱。

---

## 坑 6（舊）：DRAIN 在 Pod 重啟後持久化導致 job PENDING

這是上一輪已修的坑，這裡補充說明驗證結果。

**修法**（在 `scripts/render-core.py` worker 啟動 script 裡）：

```bash
su -s /bin/sh -c '/usr/sbin/munged --syslog' munge
sleep 1
pgrep -x munged >/dev/null
# Clear stale DRAIN from preStop hook of previous pod.
scontrol update nodename="$(hostname)" state=resume 2>/dev/null || true
exec slurmd -Dvvv -N "$(hostname)"
```

`state=resume` 在 munge 起來後、slurmd 啟動前執行，可以把 slurmctld 裡殘留的 DRAIN/DOWN 清掉。`ReturnToService=2` 只清 DOWN，不清 DRAIN，所以這行 `state=resume` 是必要的。

---

## 最終驗證結果

### verify-gpu.sh

```
=== [1] NVIDIA device plugin DaemonSet ===
  PASS: device plugin DaemonSet has ready pods

=== [2] GPU node capacity ===
  PASS: cluster has 4 allocatable GPU(s)     ← 1 張 RTX 4070 time-slicing 成 4 個 replica

=== [4] Slurm GPU GRES (sinfo) ===
  PASS: Slurm nodes have GPU GRES configured

=== [5] sbatch GPU job (nvidia-smi) ===
  Job stdout:
    NVIDIA GeForce RTX 4070, 535.288.01, 0 %
    CUDA_VISIBLE_DEVICES=0
    SLURM_JOB_GPUS=0
  PASS: GPU name visible in Slurm job output

=== [6] sbatch MPS job (--gres=mps:25) ===
  MPS job stdout:
    CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25
    CUDA_VISIBLE_DEVICES=0
    NVIDIA GeForce RTX 4070
  PASS: CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25 injected by Slurm prolog
  WARN: CUDA_MPS_PIPE_DIRECTORY missing — time-slicing 模式，MPS daemon 不啟動（預期行為）
```

### verify.sh

```
[dev verify] done. all checks passed.
```

所有測試通過，包含 CPU pool 彈性擴縮、MPI PMI2 多節點、GPU pool 彈性擴縮。

---

## Linux + k3s 完整復現步驟

> 適用環境：Ubuntu 24.04 x86\_64，RTX 4070，NVIDIA driver 535+，k3s v1.34+

### 步驟 0：主機準備

```bash
# 安裝 NVIDIA Container Toolkit（先裝好 driver）
sudo bash scripts/setup-linux-gpu.sh
# 確認 driver
nvidia-smi
```

### 步驟 1：k3s 安裝

`setup-linux-gpu.sh` 內部已處理，或手動：

```bash
INSTALL_K3S_EXEC="--container-runtime-endpoint unix:///run/containerd/containerd.sock --disable traefik" \
  curl -sfL https://get.k3s.io | sh -
# 複製 kubeconfig（setup-linux-gpu.sh 已做）
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $USER ~/.kube/config
export KUBECONFIG=~/.kube/config
```

### 步驟 2：部署 NVIDIA device plugin + RuntimeClass

```bash
KUBECONFIG=~/.kube/config kubectl apply -f manifests/gpu/runtime-class.yaml
KUBECONFIG=~/.kube/config kubectl apply -f manifests/gpu/nvidia-device-plugin.yaml
# 驗證（RTX 4070 + time-slicing → 4 GPU）
KUBECONFIG=~/.kube/config kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
```

> **注意**：`nvidia-device-plugin.yaml` 目前以 `rtx4070-timeslicing` config key 掛載。  
> MPS config (`rtx4070-mps`) 在 driver 535.288.01 + k3s 1.34 + Ubuntu 24.04 上無法啟動 MPS daemon（device-plugin subprocess spawn 問題，v0.15.0 與 v0.17.4 皆失敗）。  
> Time-slicing 模式下 `--gres=mps:25` 仍可正常投遞，`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25` 由 Slurm prolog 注入，但 `CUDA_MPS_PIPE_DIRECTORY` 不會設定。

### 步驟 3：部署核心 Slurm 叢集

```bash
KUBECONFIG=~/.kube/config K8S_RUNTIME=k3s REAL_GPU=true bash scripts/bootstrap.sh
```

### 步驟 4：部署 NFS 共享儲存

**4-A：主機 NFS Server（只需執行一次）**

```bash
sudo bash scripts/setup-nfs-server.sh
```

⚠️ 執行完後，確認 `/etc/exports` 涵蓋了節點的 LAN IP（不只 pod CIDR）：

```bash
cat /etc/exports
# 應包含類似：
# /srv/nfs/k8s 10.0.0.0/8(...) 192.168.0.0/25(...)
#                                ^^^^^^^^^^^^^^^^^^^^^^
#                                加上主機 LAN subnet
```

若 `setup-nfs-server.sh` 沒加 LAN subnet，手動補上後 `sudo exportfs -ra`。

**4-B：部署 NFS subdir provisioner**

先確認主機 IP（從 Pod 可達的 IP，通常是 LAN 介面）：

```bash
ip addr show enp5s0 | grep 'inet '   # e.g., 192.168.0.111
```

```bash
NFS_SERVER=192.168.0.111 \
NFS_PATH=/srv/nfs/k8s \
KUBECONFIG=~/.kube/config \
KUBE_CONTEXT=default \
  bash scripts/bootstrap-storage.sh
```

### 步驟 5：驗證

```bash
# 儲存驗證
KUBECONFIG=~/.kube/config KUBE_CONTEXT=default bash scripts/verify-storage.sh
KUBECONFIG=~/.kube/config KUBE_CONTEXT=default bash scripts/verify-storage-e2e.sh

# GPU 驗證（含 MPS prolog 測試）
KUBECONFIG=~/.kube/config K8S_RUNTIME=k3s REAL_GPU=true KUBE_CONTEXT=default bash scripts/verify-gpu.sh

# 全叢集驗證（CPU 彈性擴縮、MPI、GPU pool）
KUBECONFIG=~/.kube/config K8S_RUNTIME=k3s REAL_GPU=true KUBE_CONTEXT=default bash scripts/verify.sh
```

### 預期輸出（摘要）

| 驗證項目 | 預期結果 |
|----------|----------|
| device plugin DaemonSet | PASS |
| GPU node capacity | PASS（RTX4070 × 4 replicas） |
| GPU GRES in sinfo | PASS |
| sbatch GPU job (`nvidia-smi`) | PASS（輸出 GPU 型號與驅動版本） |
| sbatch MPS job (`--gres=mps:25`) | PASS（`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25`） |
| `CUDA_MPS_PIPE_DIRECTORY` | WARN（time-slicing 模式，MPS daemon 不存在，預期） |
| verify.sh all checks | PASS |

---

## 已修改的檔案清單

| 檔案 | 變更 |
|------|------|
| `scripts/render-core.py` | 加 `CompleteWait=0`；worker 啟動時加 `scontrol update state=resume` |
| `scripts/verify.sh` | 加 `GPU_PARTITION` 變數；GPU sbatch 改用 `GPU_PARTITION`；GPU 提交前 resume drained 節點 |
| `scripts/verify-gpu.sh` | job output 改寫到 `/shared/gpu-verify-<pid>/<jid>.out` |
| `scripts/setup-linux-gpu.sh` | 移除無效的 `--kube-apiserver-arg feature-gates=GangScheduling=true,GenericWorkload=true` |
| `scripts/bootstrap.sh` | 修正 `WITH_MPS=true` 的 log 訊息（MPS 由 device-plugin 負責） |
| `manifests/gpu/nvidia-device-plugin.yaml` | ConfigMap key 改為 `rtx4070-timeslicing`（MPS daemon spawn 失敗） |
| `/etc/exports`（主機） | 加入 `192.168.0.0/25` export rule |
