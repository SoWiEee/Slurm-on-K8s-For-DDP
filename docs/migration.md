# 遷移指南：Windows + Kind → Linux Host + k3s (GPU/MPS)

本文件說明如何將開發環境從 Windows 11 + Docker Desktop + Kind 遷移至 Linux 主機，以支援真實 NVIDIA GPU 和 MPS（Multi-Process Service）功能。

> **更新（2026-04-27）：** MPS 改採 NVIDIA k8s-device-plugin v0.17 內建的 `sharing.mps` 模式（自架 MPS DaemonSet 已棄用）。worker pod 不再需要 `hostIPC: true` 或 `/tmp/nvidia-mps` 掛載。partition 由 `debug` 拆成 `cpu` / `gpu-rtx4070` / `gpu-rtx4080`。

---

## 實機遷移日誌（2026-04-27，RTX 4070 / Ubuntu 24.04 / k3s 1.34）

第一次在實機上跑這份指南時，文件描述的流程**沒辦法直接 work**。本節記錄遇到的問題、已修的點、與還沒解的點。所有改動都已 commit 在 `mps-migration` 分支。

### 實機環境

| 項目 | 值 |
|------|-----|
| GPU | NVIDIA GeForce RTX 4070（12 GB） |
| Driver / CUDA | 535.288.01 / CUDA 12.2 |
| Kernel | 6.17.0-14-generic |
| OS | Ubuntu 24.04.4 LTS |
| k3s | v1.34.6+k3s1（內嵌 containerd 2.2.2-bd1.34）|
| nvidia-container-toolkit | 1.19.0 |
| Slurm | slurm-wlm 21.08.5（Ubuntu 22.04 base image apt 包） |
| device-plugin | `nvcr.io/nvidia/k8s-device-plugin:v0.15.0`（測試中；v0.17.4 MPS spawn 失敗） |

> ⚠️ 原 doc 假設 GPU pool 名是 `rtx5070` / `rtx4080`。實機只有一張 RTX 4070，已將分支內所有 `rtx5070` → `rtx4070` 全面改名（16 個檔案：`worker-pools.json`、`nvidia-device-plugin.yaml`、`bootstrap.sh`、`verify-gpu.sh`、`verify.sh`、`operator/collector.py`、`network-policy.yaml`、`slurm-elastic-operator.yaml`、`grafana-dashboards-cm.yaml`、`README.md`、`docs/*.md` 等）。

### 必要修補（已修）

下列 5 處原本就是 bug 或缺漏；不修就跑不起來：

#### 1. `manifests/gpu/nvidia-device-plugin.yaml` 的 image path 錯誤

舊：`nvcr.io/nvidia/k8s/device-plugin:v0.17.0`（NotFound）  
正確：`nvcr.io/nvidia/k8s-device-plugin:v0.17.4`（破折號，非斜線）

#### 2. 缺 `RuntimeClass nvidia` + 缺 `runtimeClassName: nvidia`

k3s 1.27+ 會自動把 `nvidia-container-runtime` 註冊成 containerd 的 `nvidia` runtime，但 pod 必須 `spec.runtimeClassName: nvidia` 才會走 NVIDIA hook（注入 `/dev/nvidia*` + 處理 `NVIDIA_VISIBLE_DEVICES`）。沒有的話 pod 用預設 runc，nvidia-smi 在 container 內看不到 GPU。

修補：
- 新增 `manifests/gpu/runtime-class.yaml`（apiVersion `node.k8s.io/v1`, handler `nvidia`）
- `bootstrap-gpu.sh` 在 apply device-plugin 前先 apply runtime-class
- `nvidia-device-plugin.yaml` DaemonSet pod spec 加 `runtimeClassName: nvidia`
- `render-core.py` 在 `--real-gpu` 時讓 GPU worker StatefulSet 也加 `runtimeClassName: nvidia`

#### 3. `render-core.py` 寫無效的 slurm.conf key `CgroupPlugin=cgroup/v2`

`CgroupPlugin` 不是 slurm.conf 的合法 directive（它應該在 `cgroup.conf`，不是 `slurm.conf`）。slurmd 解析直接 fatal：

```
slurmd: error: _parse_next_key: Parsing error at unrecognized key: CgroupPlugin
slurmd: fatal: Unable to process configuration file
```

進一步嘗試 `task/cgroup` + `proctrack/cgroup` + 寫入 cgroup.conf 也失敗：
- Slurm 21.08 不認識 `IgnoreSystemd=yes`（22.05+ 才有）
- 純 cgroup v2 host（Ubuntu 24.04 預設 unified hierarchy）沒掛 freezer，proctrack/cgroup 會 fatal：`cgroup namespace 'freezer' not mounted`

最終決定：**`real_gpu` 模式仍用 `TaskPlugin=task/none` + `proctrack/linuxproc`**，把資源隔離留給 kubelet + libnvidia-container 處理。Slurm 在這層只負責 GRES 排程記帳。

#### 4. `bootstrap.sh` 沒 apply `manifests/core/slurm-accounting.yaml`

但 `render-core.py` 產出的 `slurm.conf` 無條件帶 `AccountingStorageType=accounting_storage/slurmdbd`，且 controller 的 startup script 有：

```bash
until (echo >/dev/tcp/slurmdbd.slurm.svc.cluster.local/6819) 2>/dev/null; do sleep 3; done
```

→ 沒 slurmdbd 時 controller pod 卡死無 timeout。已在 `bootstrap.sh` 加：

```bash
if [[ -f manifests/core/slurm-accounting.yaml ]]; then
  kubectl apply -f manifests/core/slurm-accounting.yaml
fi
```

#### 5. setup-linux-gpu.sh 用 sudo 跑會把 kubeconfig 寫到 `/root/.kube/config`

腳本裡 `${HOME}` 在 sudo 下 = `/root`。需要手動：

```bash
sudo chmod 644 /etc/rancher/k3s/k3s.yaml
mkdir -p ~/.kube && sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config && chmod 600 ~/.kube/config
```

或在 ENV 設 `KUBECONFIG=/etc/rancher/k3s/k3s.yaml`（chmod 644 後）。腳本本身尚未修，只是繞過。

### GPU 共享：MPS（目標）／Time-slicing（目前 fallback）

device-plugin 內建 `sharing.mps` 在 v0.15–v0.17.x 全系列都壞掉（NVIDIA upstream bug，下面有 root cause + issue 列表）。本專案決定：

- **目前**：用 `sharing.timeSlicing` 跑通端到端，`--gres=gpu:rtx4070:1` 路徑全綠。
- **目標**：在 Phase 5-A 把 chart 化推上來時，同步把 device-plugin 換成 **NVIDIA GPU Operator**（它把 MPS daemon 拆成獨立 DaemonSet 用前景模式跑，繞過 spawn race），讓 `--gres=mps:N` SM 配額可用。

---

#### MPS（目標方案：NVIDIA GPU Operator）

##### 為什麼 device-plugin 內建 MPS 在 v0.15–v0.17.x 都失敗

device-plugin 內部以 `exec.Command("nvidia-cuda-mps-control", "-d")` daemonize 模式啟動 MPS daemon（見 [`internal/mps/daemon.go`](https://github.com/NVIDIA/k8s-device-plugin/blob/v0.17.4/internal/mps/daemon.go)），接著立刻 `echo get_default_active_thread_percentage` 透過 pipe directory 做 health check。`-d` 模式會 fork 出子 process 並讓父 process 立即退出；在 container 的 PID namespace 下，子 process 的 control pipe 初始化在 plugin probe 時還沒就緒，回 `exit status 1`，plugin 判定啟動失敗，每 30 秒重試一次永遠不會變綠。

這個 bug 與 driver / kernel / 機器型號無關，本機已實測 v0.15.0 / v0.17.0 / v0.17.4 三個 tag 都同樣失敗。相關 upstream issue：

| Issue | 標題 | 狀態 |
|-------|------|------|
| [#712](https://github.com/NVIDIA/k8s-device-plugin/issues/712) | MPS daemon health check fails immediately after spawn | open |
| [#983](https://github.com/NVIDIA/k8s-device-plugin/issues/983) | sharing.mps not working with v0.15+ | open |
| [#1094](https://github.com/NVIDIA/k8s-device-plugin/issues/1094) | error waiting for MPS daemon across versions | open |
| [#1614](https://github.com/NVIDIA/k8s-device-plugin/issues/1614) | MPS spawn race in containerized PID namespace | closed (not planned) |

release log 沒有任何一版宣告修了這個 spawn race，短期內也看不到要修的跡象。

##### GPU Operator 為什麼能繞過

[NVIDIA GPU Operator](https://github.com/NVIDIA/gpu-operator) 把 MPS daemon 拆成獨立的 `mps-control-daemon` DaemonSet 用前景模式（`nvidia-cuda-mps-control -f`）長駐，device-plugin 只透過共享的 `/run/nvidia/mps` 對既存 daemon 做 probe，不再 spawn，根源就消失了。已知在 RTX 40 系列 + driver 535 + k3s 上能跑通。

##### 落地路徑（搭 5-A Helm 化一起做）

GPU Operator 是 Helm chart，本專案 5-A 也要 Helm 化，兩件事最自然的做法是合併。Phase 5-A 的 dependency 從 `nvidia-device-plugin` 換成 `gpu-operator`，並關掉它內建的 driver / toolkit 安裝（host 已經手裝過 `nvidia-driver-535` + `nvidia-container-toolkit`，重複裝會撞）：

```yaml
# chart/Chart.yaml
dependencies:
  - name: gpu-operator
    version: 24.x.x        # 待 5-A 實作時鎖版
    repository: https://helm.ngc.nvidia.com/nvidia
    condition: gpu.enabled
```

```yaml
# chart/values-k3s.yaml
gpu-operator:
  driver:
    enabled: false        # 用 host 已裝的 nvidia-driver-535
  toolkit:
    enabled: false        # 用 host 已裝的 nvidia-container-toolkit
  devicePlugin:
    config:
      name: rtx4070-mps   # ConfigMap 由本 chart 產生，含 sharing.mps
  mps:
    root: /run/nvidia/mps
```

對應的 Slurm 端不用動：`gres.conf` `Name=mps Count=100`、prolog 注入 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 都已就位，只要 K8s 端 daemon 能起來，`--gres=mps:25` 路徑就會通。

##### 是不是該現在就 Helm 化？

**是，建議直接把 5-A 提前到下一個 milestone 開做。** 三個理由：

1. **GPU Operator 本來就是 Helm 派發**——獨立 `helm install gpu-operator` 雖然技術上可行，但會跟我們手刻的 `manifests/gpu/nvidia-device-plugin.yaml` 共存兩條路徑，反而比較髒。
2. **5-A 既有規劃就要把 device-plugin 變 chart dependency**（見 [`docs/note.md §5-A`](note.md#5-aHelm-Chart-封裝)），把 dependency 從 `nvidia-device-plugin` 換成 `gpu-operator` 是 1-line 改動，工程成本沒有暴增。
3. **時機剛好**——`mps-migration` 分支已把 Linux+k3s 路徑驗證通過，正準備合回 main；下一個分支起頭做 5-A 就能直接拿乾淨的基線往前推。

需要修正一處 5-A 既有設計：原稿 `dependencies` 用 `nvidia-device-plugin`（[`docs/note.md` 行 278-282](note.md)），改採 GPU Operator 後 `templates/gpu/device-plugin-config.yaml` 不再自己裝 daemonset，只剩 ConfigMap；`node-labeler-job.yaml` 仍保留（GPU Operator 也讀 `nvidia.com/device-plugin.config` label）。

#### Time-slicing（目前 fallback）

在 5-A + GPU Operator 上線之前，`manifests/gpu/nvidia-device-plugin.yaml` 用 `rtx4070-timeslicing` ConfigMap key 跑：

```yaml
rtx4070-timeslicing: |-
  version: v1
  flags:
    migStrategy: none
    failOnInitError: false
  sharing:
    timeSlicing:
      resources:
        - name: nvidia.com/gpu
          replicas: 4
```

並把 mount items 從 `key: rtx4070-mps` 改成 `key: rtx4070-timeslicing`。

兩種 sharing 模式對 K8s scheduler 完全等效（`nvidia.com/gpu: 4`）。差別只在：
- **MPS**：硬體 SM 切割（須 daemon），可用 `--gres=mps:N` 指定 SM%
- **time-slicing**：CUDA context-switch（無 daemon），只能整片 slice 拿（`--gres=gpu:rtx4070:1`）

對 demo / 單機 RTX 4070 多 job 並行，time-slicing 已足夠。`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 由 Slurm prolog 注入（`render-core.py` 已內建），time-slicing 模式下因為沒有 daemon、這個變數實際不影響 SM 配額——保留是為了切到 GPU Operator 之後不必改 prolog。

`verify-gpu.sh` step 1–5（device-plugin、GPU 切片、`--gres=gpu:rtx4070:1` 的 Slurm job）會通過；step 6（`--gres=mps:25` 檢查 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`）會 fail，這是預期的，等 GPU Operator 上來才會綠。

##### 既有的 device-plugin spawn 失敗排查紀錄（保留供日後參考）

| 排查項 | 實測結果 |
|------|---------|
| Image tag 錯（v0.17.0 是否 EOL） | 換 v0.17.4 / v0.15.0 同樣失敗 |
| `runtimeClassName: nvidia` 缺漏 | 已加，pod 內 nvidia-smi 看得到 RTX 4070 |
| `hostIPC` / `hostPID` / `privileged` | manifest 都已開 |
| `/run/nvidia/mps` 權限 | root:root 755，從 pod 內 `ls -la` 沒問題 |
| MPS binary 版本 mismatch | container 與 host 都是 driver 535.288.01 注入的同一支（54208 bytes，2026-01-15 時間戳）|
| Stale socket 干擾 | 清乾淨 + 重啟 pod 還是失敗 |
| `CUDA_VISIBLE_DEVICES=GPU-uuid` 模擬 device-plugin env | 手動仍成功 |
| 進入**同一 pod** 手動 `nvidia-cuda-mps-control -d` + 健康檢查 | ✅ 完美運作，回傳 `100.0` |
| 在 host 直接跑 daemon | ✅ 完美運作 |

→ 只要不是 device-plugin 自己 spawn，全部都成功——印證了 root cause 在 device-plugin 的 `-d` daemonize spawn-and-probe 邏輯，不是環境問題。

### 跑完現況

| 元件 | 狀態 |
|------|-----|
| k3s + nvidia-container-toolkit + RuntimeClass nvidia | ✅ |
| docker（Ubuntu apt `docker.io` 29.1.3，build 用） | ✅ |
| Slurm controller / slurmdbd / slurm-worker-cpu / slurm-login | ✅ Running |
| Slurm partitions `cpu` / `gpu-rtx4070` / `gpu-rtx4080` | ✅ |
| nvidia-device-plugin DaemonSet（time-slicing 4 replicas） | ✅ |
| `--gres=gpu:rtx4070:1` 路徑（verify-gpu.sh step 1–5） | ✅ |
| `--gres=mps:N` 路徑（MPS daemon，verify-gpu.sh step 6） | ❌ upstream bug，需換 GPU Operator |

### 別忘了清掉

整個 migration 過程為了讓 Claude Code 能 sudo，臨時加了 NOPASSWD：

```bash
# 跑完整理乾淨：
sudo rm /etc/sudoers.d/99-claude-mps-migration
```

---

## 為什麼要遷移？

| 需求 | Windows + Kind | Linux + k3s |
|------|---------------|------------|
| Slurm CPU 模擬 | ✅ 完整支援 | ✅ 完整支援 |
| 真實 NVIDIA GPU | ❌ 無法直通 | ✅ 直接存取 `/dev/nvidia*` |
| NVIDIA MPS | ❌ POSIX IPC 在 WSL2 不穩 | ✅ device-plugin sharing.mps 原生 |
| CUDA workload 實測 | ❌ | ✅ |
| 生產環境接近度 | 低 | 高 |

---

## Linux + k3s（完整 GPU/MPS 支援）

```
Linux host
  └── containerd + NVIDIA runtime
        └── k3s
              ├── nvidia-device-plugin (kube-system)
              │     └── 內建 MPS daemon (/run/nvidia/mps)
              │     └── 一張實體 GPU → N 個 nvidia.com/gpu 切片
              └── slurm namespace
                    ├── slurmctld + slurmrestd
                    └── slurm-worker-gpu-rtx4070-* (request nvidia.com/gpu: 1)
```

優點：沒有 Kind 的容器嵌套，`/dev/nvidia0` 直接可見，MPS 由 device-plugin 統一管理，最接近生產部署。
缺點：需要在 Linux 主機上安裝 k3s，不能與 Docker Desktop + Kind 共存。

---

## 前置需求

### 硬體
- NVIDIA GPU（RTX 3090+ 消費卡或 A10 / A100 / H100）
- Ubuntu 22.04 / 24.04（推薦）或 RHEL/Rocky 9

### 軟體
- NVIDIA Driver >= 520：`nvidia-smi` 可用
- Python 3.8+、kubectl、git

---

## Path B：k3s 完整遷移步驟

### 1. 安裝 NVIDIA Container Toolkit + k3s

以 root 執行（一次性，約 3–5 分鐘）：

```bash
sudo bash scripts/setup-linux-gpu.sh --k3s
```

這個腳本會：
1. 驗證 `nvidia-smi` 可用
2. 安裝 `nvidia-container-toolkit`
3. 設定 containerd 使用 NVIDIA runtime
4. 安裝 k3s，設定 `--container-runtime-endpoint` 指向 containerd
5. 複製 kubeconfig 到 `~/.kube/config`
6. 等待 k3s node 進入 Ready 狀態

> ⚠️ **sudo kubeconfig 問題（N5）**：腳本以 root 執行，kubeconfig 會被複製到 `/root/.kube/config`，不是你的個人帳號。k3s 安裝完後需手動修正：
> ```bash
> sudo chmod 644 /etc/rancher/k3s/k3s.yaml
> mkdir -p ~/.kube && cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
> chmod 600 ~/.kube/config
> ```

### 2. 部署核心 Slurm 叢集

```bash
K8S_RUNTIME=k3s REAL_GPU=true bash scripts/bootstrap.sh
```

`K8S_RUNTIME=k3s` 會切換以下行為：
- 跳過 `kind create cluster`
- 用 `sudo k3s ctr images import -` 匯入 docker build 出的 image（不用 `kind load docker-image`）
- `KUBE_CONTEXT` 自動改為 `default`
- namespace 顯式 label 為 `pod-security.kubernetes.io/enforce=baseline`

`REAL_GPU=true` 會讓 `render-core.py` 產生：
- `gres.conf`：`File=/dev/nvidia0` + `Name=mps Count=100`（rtx4070 池）
- `slurm.conf`：`TaskPlugin=task/none`、`AccountingStorageTRES=gres/gpu,gres/mps`（[註](#實機遷移日誌2026-04-27rtx-4070--ubuntu-2404--k3s-134)）
- 三個 PartitionName：`cpu`（Default=YES）、`gpu-rtx4070`、`gpu-rtx4080`
- GPU worker StatefulSet：`resources.limits.nvidia.com/gpu: "1"` + `runtimeClassName: nvidia`

### 3. 部署 NVIDIA Device Plugin（含 MPS sharing）

```bash
bash scripts/bootstrap-gpu.sh
```

部署 `manifests/gpu/nvidia-device-plugin.yaml`，內含四個 ConfigMap key：
- `default`：無 sharing（placeholde；目前未被使用）
- `rtx4070-mps`：`sharing.mps.resources[].replicas: 4` → 一張 RTX 4070 暴露成 4 個 `nvidia.com/gpu` 切片，由 device-plugin 自行啟動 MPS daemon 於 `/run/nvidia/mps`
- `rtx4070-timeslicing`：同樣 4 切片，但用 CUDA context-switch（無 MPS daemon，MPS 失敗時的 fallback）
- `rtx4080-exclusive`：獨佔模式（高顯存／長時訓練用）

**DaemonSet 使用哪個 config** 由 `manifests/gpu/nvidia-device-plugin.yaml` 底部的 volume mount 決定（靜態選擇，不依賴 node label）：

```yaml
items:
  - key: rtx4070-mps   # ← 改這裡即可切換 config
    path: config.yaml
```

要切換 config（例如切回 time-slicing）：

```bash
# 編輯 manifests/gpu/nvidia-device-plugin.yaml，把 key 改為 rtx4070-timeslicing
# 再 re-apply：
kubectl apply -f manifests/gpu/nvidia-device-plugin.yaml
kubectl -n kube-system rollout restart daemonset/nvidia-device-plugin-daemonset
```

> **注意**：`nvidia.com/device-plugin.config` node label 需要額外的 config-manager sidecar 才能動態切換，目前 DaemonSet 沒有部署這個 sidecar，打 label **不會有任何效果**。

等 DaemonSet rollout 完成後驗證 GPU 切片數：

```bash
kubectl get node -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}'
# RTX 4070 + sharing.mps replicas: 4 → 應該回傳 "4"
```

### 5. 驗證

```bash
# 驗證 GPU 存取 + Slurm GRES + MPS env 注入
bash scripts/verify-gpu.sh

# 驗證核心 Slurm（含 CPU jobs）
K8S_RUNTIME=k3s bash scripts/verify.sh
```

`verify-gpu.sh` 涵蓋 6 個 step：

| Step | 驗證項目 | 失敗時 |
|------|---------|--------|
| 1 | device plugin DaemonSet ready | 先跑 `bootstrap-gpu.sh` |
| 2 | 節點 `nvidia.com/gpu` allocatable > 0 | 確認 DaemonSet 正常 |
| 3 | GPU worker pod 內 `nvidia-smi` 成功 | 確認 `runtimeClassName: nvidia` |
| 4 | `sinfo` 顯示 GPU GRES | 確認 `gres.conf` / `slurm.conf GresTypes` |
| 5 | `sbatch --gres=gpu:rtx4070:1` 完成並看到 GPU name | GPU job 端到端 |
| 6 | `sbatch --gres=mps:25` 注入 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25` | MPS daemon 端到端 |

> **Step 5 注意**：GPU worker StatefulSet 預設 replicas=0，operator 在看到 pending GPU job 後才 scale up（最多 15s polling + pod 啟動時間）。若 job 在 120s 內未完成，會顯示 WARN 而非 FAIL；可用 `JOB_TIMEOUT=300 bash scripts/verify-gpu.sh` 延長等待時間。

> **Step 6（MPS）**：目前正在測試 device-plugin v0.15.0；若 MPS daemon 仍失敗，可先跳過 step 6：
> ```bash
> SKIP_MPS=true bash scripts/verify-gpu.sh
> ```

---

## 環境變數對照表

| 變數 | 預設 | 說明 |
|------|------|------|
| `K8S_RUNTIME` | `kind` | `kind` 或 `k3s`；k3s 時改用 `k3s ctr images import` 匯入 image |
| `KUBE_CONTEXT` | `kind-slurm-lab` | k3s 時自動改為 `default` |
| `REAL_GPU` | `false` | `true` 時啟用真實 GPU 資源（gres.conf 用 `/dev/nvidia0` + cgroup task plugin） |
| `CLUSTER_NAME` | `slurm-lab` | Kind cluster 名稱（k3s 忽略此變數） |

> **`WITH_MPS` 已棄用：** 舊版需要 `WITH_MPS=true` 把 `hostIPC: true` 與 `/tmp/nvidia-mps` 掛載塞進 worker pod；採用 device-plugin `sharing.mps` 後完全不需要。`bootstrap.sh --with-mps` 與 `bootstrap-gpu.sh --with-mps` 都保留為 no-op flag 僅供 back-compat。

---

## render-core.py 差異說明

`REAL_GPU=false`（dev/Kind）時產生的 `slurm.conf`：
```
TaskPlugin=task/none
ProctrackType=proctrack/linuxproc
# gres.conf
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 Count=1 File=/dev/null
```

`REAL_GPU=true`（Linux GPU）時產生的 `slurm.conf`：
```
# real_gpu 仍用 task/none + linuxproc — 詳見「實機遷移日誌」第 3 點
TaskPlugin=task/none
ProctrackType=proctrack/linuxproc
AccountingStorageTRES=gres/gpu,gres/mps
# 三個 partition
PartitionName=cpu          Nodes=slurm-worker-cpu-[0-3]                Default=YES
PartitionName=gpu-rtx4070  Nodes=slurm-worker-gpu-rtx4070-[0-1]        Default=NO
PartitionName=gpu-rtx4080  Nodes=slurm-worker-gpu-rtx4080-[0-1]        Default=NO
# gres.conf（rtx4070 池同時暴露 gpu 與 mps GRES）
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu  Type=rtx4070 Count=1   File=/dev/nvidia0
NodeName=slurm-worker-gpu-rtx4070-0 Name=mps  Count=100
NodeName=slurm-worker-gpu-rtx4080-0 Name=gpu  Type=rtx4080 Count=1   File=/dev/nvidia0
```

> 注意 `File=/dev/nvidia0` 對所有 GPU 池都一樣 — device-plugin 把分配到的實體 GPU 一律以 `/dev/nvidia0` 路徑暴露給 container，不論該 GPU 在 host 上的 PCI index 是 0 還是 1。

---

## MPS 工作流程（device-plugin sharing.mps 模式）

```
[一次性] manifests/gpu/nvidia-device-plugin.yaml 中設定：
  volumes.nvidia-config.configMap.items[0].key = rtx4070-mps
  kubectl apply -f manifests/gpu/nvidia-device-plugin.yaml
              │
              ▼
nvidia-device-plugin DaemonSet (kube-system)
  讀取掛載的 ConfigMap key "rtx4070-mps"：
    sharing.mps.resources[].replicas: 4
              │
              ▼
device-plugin 自己 fork:
  nvidia-cuda-mps-control -d   (socket on /run/nvidia/mps)
  並向 kubelet 宣告 nvidia.com/gpu: 4 (= 1 張實體 × 4 切片)
              │
              ▼
slurmctld 排程 sbatch --gres=mps:25 --partition=gpu-rtx4070
              │
              ▼
slurm-worker-gpu-rtx4070-0 pod 啟動
  k8s scheduler 配 1 個 nvidia.com/gpu 切片給 pod
  CUDA_VISIBLE_DEVICES、CUDA_MPS_PIPE_DIRECTORY 由 device-plugin 注入
              │
              ▼
Slurm prolog 把 mps:25 翻譯成
  CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25 寫進 job env
              │
              ▼
job 內 CUDA process 連 /run/nvidia/mps socket → 拿到 25% SM 配額
```

提交語法：
```bash
#SBATCH --partition=gpu-rtx4070
#SBATCH --gres=mps:25                # 請求 25% SM
# 或：
#SBATCH --gres=gpu:rtx4070:1         # 請求 1 個切片獨佔（不啟用 MPS context 共享）
```

兩個 GRES 都會走 device-plugin 的同一個切片配額；差別在 Slurm 排程語意（mps 允許多個 job 共用同一切片，gpu:N 是該節點 GPU 配額）。

---

## 回退到 Windows 開發

遷移不影響 Windows 分支，只需切回 main：

```bash
git checkout main
bash scripts/bootstrap.sh       # 回到 Kind + CPU 模擬模式
```

`mps-migration` 分支上的所有 GPU 變更只在 `K8S_RUNTIME=k3s REAL_GPU=true` 時生效；Windows/Kind 環境下的行為與之前相同（`File=/dev/null`、`TaskPlugin=task/none`、無 GPU resource request、partition `cpu` 為 default）。
