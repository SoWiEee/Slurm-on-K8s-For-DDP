# 遷移指南：Windows + Kind → Linux Host + k3s (GPU/MPS)

本文件說明如何將開發環境從 Windows 11 + Docker Desktop + Kind 遷移至 Linux 主機，以支援真實 NVIDIA GPU 和 MPS（Multi-Process Service）功能。

> **更新（2026-04-27）：** MPS 改採 NVIDIA k8s-device-plugin v0.17 內建的 `sharing.mps` 模式（自架 MPS DaemonSet 已棄用）。worker pod 不再需要 `hostIPC: true` 或 `/tmp/nvidia-mps` 掛載。partition 由 `debug` 拆成 `cpu` / `gpu-rtx5070` / `gpu-rtx4080`。

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
                    └── slurm-worker-gpu-rtx5070-* (request nvidia.com/gpu: 1)
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
- `gres.conf`：`File=/dev/nvidia0` + `Name=mps Count=100`（rtx5070 池）
- `slurm.conf`：`TaskPlugin=task/cgroup`、`AccountingStorageTRES=gres/gpu,gres/mps`
- 三個 PartitionName：`cpu`（Default=YES）、`gpu-rtx5070`、`gpu-rtx4080`
- GPU worker StatefulSet：`resources.limits.nvidia.com/gpu: "1"`

### 3. 部署 NVIDIA Device Plugin（含 MPS sharing）

```bash
bash scripts/bootstrap-gpu.sh
```

部署 `manifests/gpu/nvidia-device-plugin.yaml`，內含三個 ConfigMap key：
- `default`：無 sharing（保險用，未 label 的 GPU 節點走這個）
- `rtx5070-mps`：`sharing.mps replicas: 4` → 一張 RTX 5070 暴露成 4 個 `nvidia.com/gpu` 切片，內建 MPS daemon 啟動於 `/run/nvidia/mps`
- `rtx4080-exclusive`：獨佔模式（高顯存／長時訓練用）

### 4. 對 GPU 節點打 label，告訴 device-plugin 用哪個 config

**這一步不可省略。** 沒打 label，device-plugin 會走 `default` config，`nvidia.com/gpu` 仍是獨佔 1 張，MPS 不會啟動。

```bash
# 假設你只有一台 Linux 機器，hostname 也就是節點名：
NODE=$(kubectl get node -o jsonpath='{.items[0].metadata.name}')

# RTX 5070 host：啟用 MPS sharing（4 切片）
kubectl label node "$NODE" nvidia.com/device-plugin.config=rtx5070-mps --overwrite

# RTX 4080 host（如果有的話）：獨佔模式
# kubectl label node <RTX4080-NODE> nvidia.com/device-plugin.config=rtx4080-exclusive --overwrite
```

label 後 device-plugin DaemonSet 會自動 reload，等 30 秒後可驗證：

```bash
kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'
# RTX 5070 + sharing.mps replicas: 4 → 應該回傳 "4"
```

### 5. 驗證

```bash
# 驗證 GPU 存取 + Slurm GRES + MPS env 注入
bash scripts/verify-gpu.sh

# 驗證核心 Slurm（含 CPU jobs）
K8S_RUNTIME=k3s bash scripts/verify.sh
```

`verify-gpu.sh` 涵蓋 6 個 step，最後一步 (step 6) 提交 `--gres=mps:25` 的 sbatch 並檢查 job 環境裡 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25` 與 `CUDA_MPS_PIPE_DIRECTORY` 是否被 Slurm prolog 注入。

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
NodeName=slurm-worker-gpu-rtx5070-0 Name=gpu Type=rtx5070 Count=1 File=/dev/null
```

`REAL_GPU=true`（Linux GPU）時產生的 `slurm.conf`：
```
TaskPlugin=task/cgroup
CgroupPlugin=cgroup/v2
AccountingStorageTRES=gres/gpu,gres/mps
# 三個 partition
PartitionName=cpu          Nodes=slurm-worker-cpu-[0-3]                Default=YES
PartitionName=gpu-rtx5070  Nodes=slurm-worker-gpu-rtx5070-[0-1]        Default=NO
PartitionName=gpu-rtx4080  Nodes=slurm-worker-gpu-rtx4080-[0-1]        Default=NO
# gres.conf（rtx5070 池同時暴露 gpu 與 mps GRES）
NodeName=slurm-worker-gpu-rtx5070-0 Name=gpu  Type=rtx5070 Count=1   File=/dev/nvidia0
NodeName=slurm-worker-gpu-rtx5070-0 Name=mps  Count=100
NodeName=slurm-worker-gpu-rtx4080-0 Name=gpu  Type=rtx4080 Count=1   File=/dev/nvidia0
```

> 注意 `File=/dev/nvidia0` 對所有 GPU 池都一樣 — device-plugin 把分配到的實體 GPU 一律以 `/dev/nvidia0` 路徑暴露給 container，不論該 GPU 在 host 上的 PCI index 是 0 還是 1。

---

## MPS 工作流程（device-plugin sharing.mps 模式）

```
[一次性] kubectl label node <rtx5070> nvidia.com/device-plugin.config=rtx5070-mps
              │
              ▼
nvidia-device-plugin DaemonSet (kube-system)
  reload 後讀取 ConfigMap key "rtx5070-mps"：
    sharing.mps.replicas: 4
              │
              ▼
device-plugin 自己 fork:
  nvidia-cuda-mps-control -d   (socket on /run/nvidia/mps)
  並向 kubelet 宣告 nvidia.com/gpu: 4 (= 1 張實體 × 4 切片)
              │
              ▼
slurmctld 排程 sbatch --gres=mps:25 --partition=gpu-rtx5070
              │
              ▼
slurm-worker-gpu-rtx5070-0 pod 啟動
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
#SBATCH --partition=gpu-rtx5070
#SBATCH --gres=mps:25                # 請求 25% SM
# 或：
#SBATCH --gres=gpu:rtx5070:1         # 請求 1 個切片獨佔（不啟用 MPS context 共享）
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
