# 遷移指南：Windows + Kind → Linux Host + k3s (GPU/MPS)

本文件說明如何將開發環境從 Windows 11 + Docker Desktop + Kind 遷移至 Linux 主機，以支援真實 NVIDIA GPU 和 MPS（Multi-Process Service）功能。

---

## 為什麼要遷移？

| 需求 | Windows + Kind | Linux + k3s |
|------|---------------|------------|
| Slurm CPU 模擬 | ✅ 完整支援 | ✅ 完整支援 |
| 真實 NVIDIA GPU | ❌ 無法直通 | ✅ 直接存取 `/dev/nvidia*` |
| NVIDIA MPS | ❌ `hostIPC` 無效 | ✅ 原生 POSIX IPC |
| CUDA workload 實測 | ❌ | ✅ |
| 生產環境接近度 | 低 | 高 |

MPS 需要 `hostIPC: true` 與 POSIX shared memory，在 Windows WSL2 kernel 上無法正確運作。

---

## 遷移路徑選擇

### Path A — Linux 上繼續用 Kind（最小改動）

適合只需要驗證 GPU 裝置存取，不需要 MPS 的情境。

```
Linux host → Docker + NVIDIA runtime → Kind cluster → device plugin → GPU pod
```

優點：幾乎不需修改現有腳本。  
缺點：`hostIPC` 在 container-in-container 架構下仍有限制，MPS socket 需要額外 `Bidirectional` propagation 設定。

### Path B — Linux + k3s（推薦，完整 GPU/MPS 支援）

```
Linux host → containerd + NVIDIA runtime → k3s → device plugin + MPS daemon → GPU pod
```

優點：沒有 Kind 的容器嵌套，`/dev/nvidia0` 直接可見，MPS socket 天然共享，最接近生產部署。  
缺點：需要在 Linux 主機上安裝 k3s，不能與 Docker Desktop + Kind 共存。

---

## 前置需求

### 硬體
- NVIDIA GPU（A10 / A100 / H100 或消費級 RTX 3090+）
- Ubuntu 22.04 / 24.04（推薦）或 RHEL/Rocky 9

### 軟體
- NVIDIA Driver >= 520：`nvidia-smi` 可用
- Python 3.8+、kubectl、git

---

## Path B：k3s 完整遷移步驟

### 1. 安裝 NVIDIA Container Toolkit + k3s

以 root 執行（一次性，約 3–5 分鐘）：

```bash
# 安裝 NVIDIA Container Toolkit 並設定 containerd；同時安裝 k3s
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

`REAL_GPU=true` 會讓 `render-core.py` 產生：
- `gres.conf`：`File=/dev/nvidia0`（而非 `/dev/null`）
- `slurm.conf`：`TaskPlugin=task/cgroup`、`CgroupPlugin=cgroup/v2`
- GPU worker StatefulSet：加入 `resources.limits: nvidia.com/gpu: "N"`

### 3. 部署 NVIDIA Device Plugin

```bash
bash scripts/bootstrap-gpu.sh
```

這個腳本會：
1. 部署 `manifests/gpu/nvidia-device-plugin.yaml`（kube-system DaemonSet）
2. 等待 DaemonSet rollout 完成
3. 確認 node 上的 `nvidia.com/gpu` allocatable capacity

### 4.（選用）啟用 MPS

```bash
bash scripts/bootstrap-gpu.sh --with-mps
```

再重新 render 並重新部署（讓 worker pod 加上 `hostIPC: true` 和 MPS socket mount）：

```bash
K8S_RUNTIME=k3s REAL_GPU=true WITH_MPS=true bash scripts/bootstrap.sh
kubectl apply -f manifests/gpu/mps-daemonset.yaml
```

### 5. 驗證

```bash
# 驗證 GPU 存取與 Slurm GRES
bash scripts/verify-gpu.sh

# 驗證核心 Slurm（含 CPU jobs）
K8S_RUNTIME=k3s bash scripts/verify.sh
```

---

## Path A：Kind-on-Linux 步驟

如果不想安裝 k3s，可用 Kind + GPU config：

```bash
# 安裝 NVIDIA CT，設定 Docker runtime（不安裝 k3s）
sudo bash scripts/setup-linux-gpu.sh

# 建立 Kind cluster 並掛入 GPU 裝置
KIND_CONFIG=kind-config-gpu.yaml bash scripts/bootstrap.sh

# 部署 device plugin
bash scripts/bootstrap-gpu.sh

# 驗證
bash scripts/verify-gpu.sh
```

`kind-config-gpu.yaml` 的關鍵設定：
- `extraMounts`：將 `/dev/nvidia*` 掛入 Kind worker 節點
- `/tmp/nvidia-mps` mount 設定為 `Bidirectional` propagation

---

## 環境變數對照表

| 變數 | 預設 | 說明 |
|------|------|------|
| `K8S_RUNTIME` | `kind` | `kind` 或 `k3s` |
| `KUBE_CONTEXT` | `kind-slurm-lab` | k3s 時自動改為 `default` |
| `REAL_GPU` | `false` | `true` 時啟用真實 GPU 資源（gres.conf + cgroup） |
| `WITH_MPS` | `false` | `true` 時在 GPU worker pod 加入 MPS mount |
| `CLUSTER_NAME` | `slurm-lab` | Kind cluster 名稱（k3s 忽略此變數） |

---

## render-core.py 差異說明

`REAL_GPU=false`（dev/Kind）時產生的 `slurm.conf`：
```
TaskPlugin=task/none
# gres.conf
NodeName=slurm-worker-gpu-a10-0 Name=gpu Type=a10 Count=1 File=/dev/null
```

`REAL_GPU=true`（Linux GPU）時產生的 `slurm.conf`：
```
TaskPlugin=task/cgroup
CgroupPlugin=cgroup/v2
# gres.conf
NodeName=slurm-worker-gpu-a10-0 Name=gpu Type=a10 Count=1 File=/dev/nvidia0
```

`WITH_MPS=true` 額外在每個 GPU worker StatefulSet 加入：
```yaml
hostIPC: true
volumeMounts:
  - name: mps-socket
    mountPath: /tmp/nvidia-mps
volumes:
  - name: mps-socket
    hostPath:
      path: /tmp/nvidia-mps
      type: DirectoryOrCreate
```

---

## MPS 工作流程（啟用後）

```
nvidia-cuda-mps-control -d     (mps-daemonset pod 啟動)
         │
         ▼
/tmp/nvidia-mps/                (hostPath, 對 GPU worker pod 可見)
         │
         ▼
Slurm job 提交：
  sbatch --gres=mps:25 job.sh   (mps:25 = 25% SM)
         │
         ▼
Slurm prolog (可選)：
  echo start_server | nvidia-cuda-mps-control
  echo set_active_thread_percentage 25 | nvidia-cuda-mps-control
         │
         ▼
多個 CUDA process 共享同一 GPU context，SM 並行執行
```

提交語法（Path B Slurm Prolog/Epilog 模式）：
```bash
#SBATCH --gres=mps:25           # 請求 25% SM
#SBATCH --constraint=gpu-a10
```

---

## 回退到 Windows 開發

遷移不影響 Windows 分支，只需切回 main：

```bash
git checkout main
bash scripts/bootstrap.sh       # 回到 Kind + CPU 模擬模式
```

mps-migration 分支上的所有 GPU 變更僅在 Linux + REAL_GPU=true 時生效；Windows/Kind 環境下的行為與之前相同（`File=/dev/null`、`TaskPlugin=task/none`、無 hostIPC）。
