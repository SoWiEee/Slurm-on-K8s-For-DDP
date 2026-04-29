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

#### Time-slicing（fallback）

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

## 環境變數對照表

| 變數 | 預設 | 說明 |
|------|------|------|
| `K8S_RUNTIME` | `kind` | `kind` 或 `k3s`；k3s 時改用 `k3s ctr images import` 匯入 image |
| `KUBE_CONTEXT` | `kind-slurm-lab` | k3s 時自動改為 `default` |
| `REAL_GPU` | `false` | `true` 時啟用真實 GPU 資源（gres.conf 用 `/dev/nvidia0` + cgroup task plugin） |
| `CLUSTER_NAME` | `slurm-lab` | Kind cluster 名稱（k3s 忽略此變數） |

> **`WITH_MPS` 已棄用：** 舊版需要 `WITH_MPS=true` 把 `hostIPC: true` 與 `/tmp/nvidia-mps` 掛載塞進 worker pod；採用 device-plugin `sharing.mps` 後完全不需要。`bootstrap.sh --with-mps` 與 `bootstrap-gpu.sh --with-mps` 都保留為 no-op flag 僅供 back-compat。

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

