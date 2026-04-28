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

### MPS 啟動失敗（**已知 NVIDIA upstream bug，目前用 time-slicing 暫代**）

#### 症狀

device-plugin v0.15.0 / v0.17.0 / v0.17.4 都重複吐：

```
Failed to start plugin: error waiting for MPS daemon:
  error checking MPS daemon health:
    failed to send command to MPS daemon: exit status 1
```

每 30 秒重試一次，永遠不變綠。`nvidia.com/gpu` allocatable 維持 `0`。

#### 已排除

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

→ 只要不是 device-plugin 自己 spawn，全部都成功。

#### 根因（已從 upstream 確認）

device-plugin 內部以 `exec.Command("nvidia-cuda-mps-control", "-d")` daemonize 模式啟動 MPS daemon（見 [`internal/mps/daemon.go`](https://github.com/NVIDIA/k8s-device-plugin/blob/v0.17.4/internal/mps/daemon.go)），接著立刻 `echo get_default_active_thread_percentage` 透過 pipe directory 做 health check。`-d` 模式會 fork 出新 process 並以原 process group leader 退出；在 container 的 PID namespace 下，新 process 的初始化在 plugin probe 時還沒完成，導致 control pipe 還沒就緒就被 query，回 `exit status 1`，plugin 判定啟動失敗。

這個 bug 在 NVIDIA k8s-device-plugin 的 v0.15–v0.17.x 全系列都存在，與 driver / kernel / 機器型號無關。相關 GitHub issue：

| Issue | 標題 | 狀態 |
|-------|------|------|
| [#712](https://github.com/NVIDIA/k8s-device-plugin/issues/712) | MPS daemon health check fails immediately after spawn | open |
| [#983](https://github.com/NVIDIA/k8s-device-plugin/issues/983) | sharing.mps not working with v0.15+ | open |
| [#1094](https://github.com/NVIDIA/k8s-device-plugin/issues/1094) | error waiting for MPS daemon across versions | open |
| [#1614](https://github.com/NVIDIA/k8s-device-plugin/issues/1614) | MPS spawn race in containerized PID namespace | closed (not planned) |

換版本沒用（已實測 v0.15.0、v0.17.0、v0.17.4 三個 tag 都同樣失敗，與 issue #1094 報告一致）。release log 沒有任何一版宣告修了這個 spawn race。

#### 三種繞道方案

##### 方案 1：繼續用 time-slicing（**目前採用**）

`manifests/gpu/nvidia-device-plugin.yaml` 加了 `rtx4070-timeslicing` ConfigMap key：

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

對 demo / 開發 / 單機 RTX 4070 多 job 並行，time-slicing 已足夠。`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 由 Slurm prolog 注入（render-core.py 已內建），但因為沒有 daemon，這個環境變數實際不影響 SM 配額——保留是為了未來切回 MPS 時不必改 prolog。

##### 方案 2：分離 MPS DaemonSet（用 `-f` 前景模式）

繞過 device-plugin 自己 spawn 的問題：另起一個 DaemonSet 用 `nvidia-cuda-mps-control -f` 前景模式跑 daemon，device-plugin 改成「**假設 daemon 已存在**」模式。NVIDIA 官方 GPU Operator 採這個架構（見下）。手刻版本要：

1. 寫一個 DaemonSet 跑 `nvidia-cuda-mps-control -f`，掛 `/run/nvidia/mps` hostPath
2. patch device-plugin source 把 spawn-and-probe 改成只 probe（或 fork 一份不 spawn 的 build）
3. 處理 daemon 重啟時 plugin 的健康檢查 race

工程量中等，需要自編 device-plugin image。短期不值得。

##### 方案 3：改用 NVIDIA GPU Operator（推薦長期方向）

[NVIDIA GPU Operator](https://github.com/NVIDIA/gpu-operator) 把 MPS daemon 拆成獨立 `mps-control-daemon` DaemonSet（用前景模式），device-plugin 只 probe 不 spawn，已知能在 RTX 40 系列 + driver 535 上跑通。代價是：

- Helm chart 把整套 driver / toolkit / device-plugin / DCGM exporter 一起裝，跟我們手動裝 `nvidia-driver-535` + `nvidia-container-toolkit` 的路徑會撞
- k3s 上有人成功（issue 串裡有報告），但需要關掉 GPU Operator 內建的 driver / toolkit 安裝（`driver.enabled=false`、`toolkit.enabled=false`），讓它只接管 device-plugin + MPS daemon

如果之後要做多機 GPU 或 H100，建議直接切 GPU Operator，比繼續維護手刻 manifest 划算。

#### 對本專案的建議

RTX 4070 + driver 535 + k3s 的 demo 環境**短期維持 time-slicing**：
- `verify-gpu.sh` step 1–5 全綠（device-plugin、GPU 切片、`--gres=gpu:rtx4070:1` 的 Slurm job）
- step 6（`--gres=mps:25` 檢查 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`）會 fail，這是預期的，腳本已加註解

要正式上 MPS（例如要在 demo 展示 SM 配額）就直接跳到方案 3 換 GPU Operator，不要再花時間 debug device-plugin 內建 MPS spawn。

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
