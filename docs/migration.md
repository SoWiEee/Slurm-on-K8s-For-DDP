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
| device-plugin | `nvcr.io/nvidia/k8s-device-plugin:v0.17.4` |

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

### MPS 啟動失敗（**未解，目前用 time-slicing 暫代**）

#### 症狀

device-plugin v0.17.0 與 v0.17.4 都重複吐：

```
Failed to start plugin: error waiting for MPS daemon:
  error checking MPS daemon health:
    failed to send command to MPS daemon: exit status 1
```

每 30 秒重試一次，永遠不變綠。`nvidia.com/gpu` allocatable 維持 `0`。

#### 已排除

| 排查項 | 實測結果 |
|------|---------|
| Image tag 錯（v0.17.0 是否 EOL） | 換 v0.17.4 同樣失敗 |
| `runtimeClassName: nvidia` 缺漏 | 已加，pod 內 nvidia-smi 看得到 RTX 4070 |
| `hostIPC` / `hostPID` / `privileged` | manifest 都已開 |
| `/run/nvidia/mps` 權限 | root:root 755，從 pod 內 `ls -la` 沒問題 |
| MPS binary 版本 mismatch | container 與 host 都是 driver 535.288.01 注入的同一支（54208 bytes，2026-01-15 時間戳）|
| Stale socket 干擾 | 清乾淨 + 重啟 pod 還是失敗 |
| `CUDA_VISIBLE_DEVICES=GPU-uuid` 模擬 device-plugin env | 手動仍成功 |
| 進入**同一 pod** 手動 `nvidia-cuda-mps-control -d` + 健康檢查 | ✅ 完美運作，回傳 `100.0` |
| 在 host 直接跑 daemon | ✅ 完美運作 |

→ 只要不是 device-plugin 自己 spawn，全部都成功。

#### 已知差異

device-plugin（v0.17 source）spawn MPS 時 env 含：
- `CUDA_MPS_PIPE_DIRECTORY=/run/nvidia/mps`
- `CUDA_MPS_LOG_DIRECTORY=/run/nvidia/mps/log`
- `CUDA_VISIBLE_DEVICES=<GPU UUID>`

我手動設一模一樣的 env 也成功。所以差異只在「Go subprocess 啟動 + `cmd.Run()` 等待 + 緊接著 `cmd.CombinedOutput()` probe」這串連環動作的某處。`dmesg` 同期看到 `nvidia-drm` 的 `Failed to grab modeset ownership` error，但跟 MPS 不直接相關。

#### 候選根因（皆未證實）

1. **kernel 6.17 + driver 535 相容問題** — driver 535.288.01 官方支援到 6.x；6.17 是 mainline 新版（Ubuntu 24.04 預設是 6.8），可能有 silent regression。
2. **device-plugin v0.17.x 的 MPS spawn race** — daemon 跟 plugin 的 health check 同步有微妙 race；但手動測試 1ms 內 probe 也會過，這條解釋不通。
3. **device-plugin 把 `cmd.Env` 改成 `[]string` 而非 `append(os.Environ(), ...)`** — 切掉了 `LD_LIBRARY_PATH` 之類的 NVIDIA library env，讓 daemon fork 後 dlopen libcuda 失敗。這條最有可能但需要 strace 證實。

#### 暫時的解法：time-slicing

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

`verify-gpu.sh` step 1–5（device-plugin、GPU 切片、`--gres=gpu:rtx4070:1` 的 Slurm job）會通過；step 6 (`--gres=mps:25` 檢查 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`) 會 fail，這是預期的。

### 跑完現況

| 元件 | 狀態 |
|------|-----|
| k3s + nvidia-container-toolkit + RuntimeClass nvidia | ✅ |
| docker（Ubuntu apt `docker.io` 29.1.3，build 用） | ✅ |
| Slurm controller / slurmdbd / slurm-worker-cpu / slurm-login | ✅ Running |
| Slurm partitions `cpu` / `gpu-rtx4070` / `gpu-rtx4080` | ✅ |
| nvidia-device-plugin DaemonSet（time-slicing） | ✅ Running，allocatable `nvidia.com/gpu=4` |
| `--gres=gpu:rtx4070:1` 路徑 | 待 verify-gpu.sh 跑 |
| `--gres=mps:N` 路徑（MPS daemon） | ❌ 未解 |

### 下次接手要做的事

依優先順序：

1. **跑 verify-gpu.sh** 看 step 1–5 是否全綠（time-slicing 模式下的 GPU job 端到端）。
2. **strace MPS spawn 找根因** — 在 device-plugin pod 內 `apk add strace`（或先 docker build 一個帶 strace 的 sidecar），attach 到 `nvidia-device-plugin` 主程序，看它 spawn `nvidia-cuda-mps-control -d` 時的完整 syscall 序列；對照手動成功的版本找出環境變數或 fd 差異。
3. **試降版 device-plugin（v0.16.x、v0.15.x）** — 看 v0.17 是否引入 regression。
4. **試 mainline 預設 kernel 6.8** — `apt install linux-image-generic-hwe-24.04` 切回 6.8，重啟驗證 driver 535 是否能正確 spawn MPS daemon。
5. **MPS 解掉後** — 把 `nvidia-device-plugin.yaml` 的 mount items 從 `rtx4070-timeslicing` 切回 `rtx4070-mps`；`verify-gpu.sh` 全綠。

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

部署 `manifests/gpu/nvidia-device-plugin.yaml`，內含三個 ConfigMap key：
- `default`：無 sharing（保險用，未 label 的 GPU 節點走這個）
- `rtx4070-mps`：`sharing.mps replicas: 4` → 一張 RTX 4070 暴露成 4 個 `nvidia.com/gpu` 切片，內建 MPS daemon 啟動於 `/run/nvidia/mps`
- `rtx4080-exclusive`：獨佔模式（高顯存／長時訓練用）

### 4. 對 GPU 節點打 label，告訴 device-plugin 用哪個 config

**這一步不可省略。** 沒打 label，device-plugin 會走 `default` config，`nvidia.com/gpu` 仍是獨佔 1 張，MPS 不會啟動。

```bash
# 假設你只有一台 Linux 機器，hostname 也就是節點名：
NODE=$(kubectl get node -o jsonpath='{.items[0].metadata.name}')

# RTX 4070 host：啟用 MPS sharing（4 切片）
kubectl label node "$NODE" nvidia.com/device-plugin.config=rtx4070-mps --overwrite

# RTX 4080 host（如果有的話）：獨佔模式
# kubectl label node <RTX4080-NODE> nvidia.com/device-plugin.config=rtx4080-exclusive --overwrite
```

label 後 device-plugin DaemonSet 會自動 reload，等 30 秒後可驗證：

```bash
kubectl get node "$NODE" -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'
# RTX 4070 + sharing.mps replicas: 4 → 應該回傳 "4"
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
[一次性] kubectl label node <rtx4070> nvidia.com/device-plugin.config=rtx4070-mps
              │
              ▼
nvidia-device-plugin DaemonSet (kube-system)
  reload 後讀取 ConfigMap key "rtx4070-mps"：
    sharing.mps.replicas: 4
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
