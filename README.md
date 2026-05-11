<div align="center">
  
# 〰️ Kelpflux
 
### Elastic Slurm scheduling on Kubernetes for shared GPU AI workloads.
 
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/SoWiEee/Kelpflux)
![Slurm](https://img.shields.io/badge/Slurm-23.11-2E86AB?logo=data:image/svg+xml;base64,)
![Kubernetes](https://img.shields.io/badge/Kubernetes-1.34-326CE5?logo=kubernetes&logoColor=white)
![Helm](https://img.shields.io/badge/Helm-3.16+-0F1689?logo=helm&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-1B4332?logoColor=white)
 
*A resilient forest of compute — scheduled by Slurm, scaled by Kubernetes.*
 
Kelpflux brings HPC-grade batch scheduling to Kubernetes, so AI researchers can
submit `sbatch` jobs against a cloud-native cluster that auto-scales CPU and
GPU pools on demand — with MPS-based GPU sharing, checkpoint-aware draining,
and full Prometheus observability.
 
[快速開始](#-getting-started) ·
[叢集規格](docs/cluster.md) ·
[優化排程研究](docs/scheduler.md) ·
[採坑紀錄和實作筆記](docs/note.md)
 
</div>
 
<div align="left">
  
## What is Kelpflux?
 
Kelpflux is a cloud-native AI workload platform that runs **Slurm on Kubernetes**.
Researchers submit jobs with familiar `sbatch` commands; the platform handles
the rest — elastic CPU/GPU pool autoscaling, MPS-based GPU sharing,
checkpoint-aware draining, and end-to-end observability.
 
The name fuses two ideas: *kelp forests*, where many independent fronds anchor
to a shared seabed and grow or retreat with the tides; and *flux*, the
continuous flow of compute demand through GPU pools. Together they describe
exactly what Kelpflux does — independent worker pools sharing a common Slurm
control plane, with job throughput flowing dynamically across resources as
demand rises and falls.
 
</div>

---


# 🌱 Motivation

一台有 CPU 和 GPU 的機器，同時有多種 AI 工作要跑——模型推論、超參數搜尋、fine-tuning、資料前處理。  
沒有好的排程系統時，會發生：

- GPU 跑推論時大量閒置（utilization < 20%），同一張卡只讓一個 process 用。
- 多人共用一台主機互相搶資源，沒有隊列、沒有隔離、先到先得。
- Fine-tuning 跑到一半機器重啟，checkpoint 沒存好，重頭來過。
- 工作量少的時候，worker 進程還是佔著資源不釋放。

這些問題的根源在於：現有工具在**資源彈性**和**排程精準度**之間做了取捨。

| 工具 | 擅長 | 不擅長 |
|------|------|--------|
| Kubernetes | 彈性伸縮、容器管理、雲端原生 | HPC workload 的精細資源語意（CPU affinity、GPU GRES、MPS 分配） |
| Slurm | 批次排程、CPU/GPU 精準分配、叢集治理、多使用者隊列 | 動態節點、雲端彈性、容錯恢復 |

本專案的目標很直接：**讓兩者合作**。把 Slurm 跑在 Kubernetes 上，用 K8s 的彈性伸縮撐起 Slurm 的排程能力，解決硬體資源分配的核心問題：

- **利用率**：透過 Slurm MPS（`--gres=mps:25`）讓多個 AI job 共用同一張 GPU 的 SM，utilization 從 < 20% 提升至 70%+
- **隔離性**：CPU pool 和 GPU pool 獨立 autoscale，不同類型的工作互不競爭
- **彈性**：沒有 job 時 worker pod 自動縮回 0；job 進 queue 時 Operator 自動擴出對應節點
- **容錯**：Checkpoint-aware 縮容保護，確保 fine-tuning job 不被中途打斷；NFS PVC 讓結果跨節點持久化

使用者只需要 SSH 進 login node，用熟悉的 `sbatch` 提交工作，不需要知道底層 K8s 的存在。

---

# 🚀 Getting Started

部署統一使用 Helm，baseline 在 `chart/values.yaml`（Kind dev / 無 GPU），生產環境用 `chart/values-k3s.yaml` overlay。

> Helm chart 名為 `slurm-platform`，把 namespace、ConfigMap、controller/worker StatefulSet、operator、login、NetworkPolicy、device-plugin-config、monitoring（Prometheus/Grafana/Alertmanager/exporters）、storage（NFS subdir provisioner + RWX PVC）全部納入。GPU Operator 因為 PSS=privileged 需求，獨立用 `scripts/install-gpu-operator.sh` 裝到自己的 `gpu-operator` namespace。完整背景見 [`docs/note.md §5-A`](docs/note.md)。

> 驗證環境：Ubuntu 24.04 x86\_64 + k3s v1.34 + RTX 4070 + NVIDIA driver 595 ✅️

## 1. 主機準備

安裝 NVIDIA Container Toolkit + k3s + helm，並複製 kubeconfig：

```bash
sudo bash scripts/setup-linux-gpu.sh
export KUBECONFIG=~/.kube/config

nvidia-smi
kubectl get nodes
helm version --short    # 需要 v3.16+
```

## 2. 建置容器映像並匯入 k3s

```bash
docker build -t slurm-controller:latest         -f docker/controller/Dockerfile         docker/controller
docker build -t slurm-worker:latest             -f docker/worker/Dockerfile             docker/worker
docker build -t slurm-elastic-operator:latest   -f docker/operator/Dockerfile           .
docker build -t slurm-exporter:latest           -f docker/slurm-exporter/Dockerfile     docker/slurm-exporter

for img in slurm-controller:latest slurm-worker:latest slurm-elastic-operator:latest slurm-exporter:latest; do
  docker save "$img" | sudo k3s ctr images import -
done
```

## 3. 建立 secrets (munge/ssh/JWT)

```bash
bash scripts/create-secrets.sh
```

## 4. 套用必要的 cluster-scoped 資源 + accounting 後端

```bash
kubectl apply -f manifests/gpu/runtime-class.yaml          # NVIDIA RuntimeClass（GPU pool 用）
kubectl apply -f manifests/core/slurm-accounting.yaml      # mysql + slurmdbd（chart 之外的 prerequisite）
```

## 5. 主機 NFS server + LAN exports (Optional)

```bash
sudo bash scripts/setup-nfs-server.sh
cat /etc/exports                       # 必須含 pod CIDR (10.0.0.0/8) AND LAN subnet
sudo exportfs -ra
```

## 6. 透過 Helm 部署整套平台

```bash
helm install slurm-platform ./chart \
  -f chart/values-k3s.yaml \
  -n slurm \
  --create-namespace
```

預設行為（k3s overlay）：

- `gpu.enabled=true`：在 `gpu-operator` namespace 放 device-plugin-config ConfigMap + 跨節點 labeler Job
- `monitoring.enabled=true`：Prometheus + Alertmanager + Grafana + kube-state-metrics + slurm-exporter（namespace `monitoring`）
- `storage.enabled=true` + `nfsServer=192.168.0.111`：NFS subdir provisioner + StorageClass `slurm-shared-nfs` + 20Gi RWX PVC

LAN IP 不一樣時用 `--set storage.nfsServer=<your-ip>` 覆寫。

## 7. 安裝 NVIDIA GPU Operator

```bash
bash scripts/install-gpu-operator.sh    # 進 gpu-operator namespace，PSS=privileged
```

腳本是 idempotent 的，重跑等於 helm upgrade。`--set driver.enabled=false --set toolkit.enabled=false` 因為 host 已經由 setup-linux-gpu.sh 裝好驅動。

## 8. 驗證

```bash
KUBE_CONTEXT=default bash scripts/verify-helm.sh           # chart 渲染 + dry-run + helm-unittest
KUBE_CONTEXT=default bash scripts/verify-storage.sh        # PVC Bound、跨 pod 讀寫
KUBE_CONTEXT=default bash scripts/verify-storage-e2e.sh    # 多節點 sbatch 寫共用儲存
KUBE_CONTEXT=default bash scripts/verify-monitoring.sh     # Prometheus 抓得到 slurm-exporter / operator
K8S_RUNTIME=k3s REAL_GPU=true KUBE_CONTEXT=default bash scripts/verify-gpu.sh
K8S_RUNTIME=k3s REAL_GPU=true KUBE_CONTEXT=default bash scripts/verify.sh
```

## 9. 執行 Phase 6 M8 evaluation（離線模擬 + 出圖）

論文 evaluation 章節的 7 張圖跟主表，純跑在離線模擬器上，不需要 cluster 跑著。約 5 分鐘出齊全部 raw data + figures。

```bash
# 一次性：venv + 依賴
uv venv .venv-m5
uv pip install --python .venv-m5/bin/python pytest matplotlib

# 1. 跑 E1..E6（FCFS / multifactor / score-M3 / score-M5 / score-M7 +
#    9-cell sensitivity grid），輸出到 eval/results/
bash eval/scripts/run_all.sh

# 2. 出圖到 eval/figures/{fig1..fig7}.{png,pdf}
.venv-m5/bin/python eval/scripts/plot_all.py

# 3. 印主表到 stdout
.venv-m5/bin/python eval/scripts/print_summary.py

# 4. （可選）E7 真機 50-job mix —— 需要 kubeconfig 指到一個跑 chart 的 cluster
bash eval/scripts/run_e7_live.sh our      # 我們的 stack (M3+M5+M7)
# 翻成 vendor baseline 後再跑一次：
# helm upgrade ... -f vendor-baseline.yaml
bash eval/scripts/run_e7_live.sh vendor
```

論述跟結論寫在 `docs/eval-writeup.md`（headline：mean JCT 12.6h → 2.62h，
比 Slurm vendor multifactor 多砍 28.6%）。`eval/results/` 已 gitignore，
重跑 `run_all.sh` 會重產。

## 🗑️ 清理環境

```bash
helm uninstall slurm-platform -n slurm
helm uninstall gpu-operator   -n gpu-operator
kubectl delete -f manifests/core/slurm-accounting.yaml
kubectl delete namespace slurm gpu-operator monitoring nfs-provisioner
# 主機層
/usr/local/bin/k3s-uninstall.sh
sudo systemctl stop nfs-kernel-server
```

> StorageClass 與 gpu-operator namespace 都帶 `helm.sh/resource-policy=keep` 註記，所以 `helm uninstall` 不會自動把它們連同 PV/PVC 拔掉；手動 `kubectl delete namespace` 才會清乾淨。

---

## 部署監控

`monitoring.enabled=true`（k3s overlay 預設打開，Kind baseline 預設關閉）。

```bash
# 存取 Grafana
kubectl -n monitoring port-forward svc/grafana 3000:3000

# 驗證 Prometheus 抓得到 slurm-exporter / operator / kube-state-metrics
bash scripts/verify-monitoring.sh
```

## Lmod 模組系統（已整合至核心）

Lmod 已整合進 `docker/controller` 與 `docker/worker` image，`helm install` 起來後即可使用 `module load`。Modulefile 定義在 `manifests/core/lmod-modulefiles.yaml`，以 ConfigMap 管理（chart 之外，獨立 apply）。

執行一次以確保 NFS job 輸出路徑存在（**需先完成 §4**）：

```bash
kubectl apply -f manifests/core/lmod-modulefiles.yaml
bash scripts/bootstrap-lmod.sh
```

**部署後的操作體驗：**

```bash
# 進 login pod（如同登入 HPC login node）
kubectl -n slurm exec -it deploy/slurm-login -- bash

# 查看可用模組
module avail

# 載入 OpenMPI
module load openmpi/4.1

# 確認環境變數已設定
echo $MPI_HOME           # /usr/lib/x86_64-linux-gnu/openmpi
echo $SLURM_MPI_TYPE     # pmi2

# 卸載全部
module purge
```

**在 sbatch 腳本中使用 module（關鍵：需明確 source lmod.sh）：**

```bash
cat > /tmp/my-mpi-job.sh << 'EOF'
#!/bin/bash
#SBATCH --ntasks=2
#SBATCH --nodes=1

source /etc/profile.d/lmod.sh   # 讓 module 指令在批次作業內可用
module load openmpi/4.1

srun --mpi=pmi2 /bin/sh -c 'echo "rank:${SLURM_PROCID} host:$(hostname)"'
EOF

sbatch /tmp/my-mpi-job.sh
```

> **為什麼要明確 source lmod.sh？**  
> `sbatch` 執行腳本時使用非互動、非 login 的 bash，`/etc/profile.d/` 不會自動載入。  
> 明確 source 是標準 HPC 做法，與 TACC、NCHC 等真實系統的 job script 寫法一致。

驗證腳本：

```bash
bash scripts/verify-lmod.sh
```

驗證項目包含：Lmod 安裝確認 → `module avail` 顯示三個模組 → `module load` 設定 MPI_HOME → `module purge` 清除環境 → sbatch 提交雙 task MPI job → 確認 rank:0 / rank:1 在 job 內正確執行。

目前內建模組如下：

| 模組 | 描述 |
|------|------|
| `openmpi/4.1` | OpenMPI 4.1.2（Ubuntu 22.04 套件），設定 MPI_HOME、LD_LIBRARY_PATH、SLURM_MPI_TYPE=pmi2 |
| `python3/3.10` | 系統 Python 3.10，設定 PYTHON_HOME |
| `cuda/stub` | CUDA 佔位模組，示範 GPU 叢集的 modulefile 結構 |

---

# 🏗️ System Architecture

用一句話說：你提交一個 Slurm job，系統自動把需要的節點準備好，跑完之後再把資源還回去。

稍微展開一點：

1. 使用者登入 Login Pod，用熟悉的 `sbatch` 指令提交訓練任務。
2. Elastic Operator 偵測到有 pending job，自動擴充對應的 worker 節點（CPU / GPU-A10 / GPU-H100 各自獨立管理）。
3. 訓練結果存在所有節點都能讀寫的 NFS 共享磁碟（`/shared`）。
4. 任務結束後，Operator 確認節點閒置且 checkpoint 安全，才把資源縮回去。

```
使用者 → sbatch → Slurm Controller → 排程到 Worker Pod
                        ↑
              Elastic Operator（Python）
              偵測 Queue → 擴 / 縮 Worker StatefulSet
```

---

## 系統架構

<img width="4400" height="2280" alt="圖片" src="https://github.com/user-attachments/assets/5d27ca15-525c-4936-a447-252a8a081934" />


> 完整架構圖請看 [`architecture.html`](assets/architecture.html)

### 主要元件說明

| 元件 | 角色 |
|------|------|
| `slurm-controller` | 執行 `slurmctld`，負責所有排程決策；job 狀態存於獨立 PVC（`slurm-ctld-state`），pod 重啟後 queue 不遺失 |
| `slurm-login` | 使用者入口，提供 `sbatch`、`srun`、`squeue` 等指令 |
| `slurm-worker-*` | 實際執行計算的節點，分 CPU / GPU-A10 / GPU-H100 三個池 |
| `slurm-elastic-operator` | 自製 Python Operator，監控 Queue 狀態並動態調整各 pool 的 replicas；縮容前先 drain 節點，等待 job 完成後才減少 StatefulSet replica |
| `slurmdbd` | Slurm Database Daemon，將 job 會計紀錄（CPU-hours、用戶統計）持久化到 MySQL，為 Fair-Share 排程提供基礎 |
| `mysql` | 後端資料庫（StatefulSet），儲存 slurmdbd 的會計資料，使用 5 Gi PVC |
| NFS + RWX PVC | 跨所有節點的共享磁碟，job 輸出直接寫入 `/shared` |
| `lmod` + modulefile ConfigMaps | HPC 標準模組系統；`module load openmpi/4.1` 等指令在 login pod 與 job 內均可用；modulefile 以 K8s ConfigMap 管理，`kubectl apply` 即可新增/更新模組 |

---

# 🎯 Development Progress

| Phase# | 狀態 | 內容 |
|-------|------|------|
| 1：基礎 Slurm 叢集 | ✅ 完成 | Controller + Worker + Login Pod，Munge 認證，靜態節點預宣告；slurmctld state PVC（job queue 持久化）；slurmdbd + MySQL 會計後端；PodDisruptionBudget 保護所有關鍵元件；**Lmod 整合**（modulefile ConfigMap，`module load` 開機即可用） |
| 2：彈性 Operator | ✅ 完成 | 多節點池自動擴縮（CPU/GPU 各自獨立）、結構化日誌、Checkpoint-aware 縮容保護（Grace Period 支援）、drain-then-scale；Cooldown 持久化（StatefulSet annotation）；熔斷器 + readinessProbe；全套 NetworkPolicy（Ingress + Egress）|
| 2-E：雙網路拓撲 | ✅ MVP 完成 | 透過 Multus 增加第二張網卡（`net2`），DDP collective traffic（NCCL/Gloo）走獨立網路 |
| 3：共享儲存 | ✅ 完成 | NFS + RWX PVC 掛載到所有節點，`sbatch -o /shared/out-%j.txt` 可直接取得輸出；多節點 E2E 驗證通過（含 slurmctld IP cache 修正） |
| 4：可觀測性 | ✅ 完成 | Prometheus + Grafana 監控，統一呈現 Slurm 排程語意與 K8s 彈性伸縮行為，視覺化兩個世界的橋接過程 |
| 5：平台封裝（Lmod + Helm） | ✅ 完成 | Lmod 整合、`/shared/jobs/` 路徑、Worker preStop Hook；Helm chart |
| 6：自訂 Slurm 排程 | ✅ M1-M8 完成 | Score-based scheduling、runtime predictor、fragmentation requeue、trace replay simulator 與 evaluation 已落地；進階排程功能預設仍以 Helm flag 關閉，需按環境逐項啟用 |
| 7：分散式追蹤 + SSH Login | 📋 規劃中 | OpenTelemetry job lifecycle trace（Tempo + Operator span + Prometheus exemplar）；Login pod 開放 SSH NodePort 取代 `kubectl exec` |

---

# ⚡ Useful Commands

## Slurm Cluster

```bash
# 查看所有 pod 狀態
kubectl -n slurm get pods -o wide

# 查看 Operator 決策日誌（結構化 JSON）
kubectl -n slurm logs deployment/slurm-elastic-operator -f | python3 -m json.tool

# 查看 Slurm controller 日誌
kubectl -n slurm logs statefulset/slurm-controller -f

# 查詢 Operator 寫下的 cooldown 時間戳
kubectl -n slurm get statefulset slurm-worker-cpu \
  -o jsonpath='{.metadata.annotations.slurm\.k8s/last-scale-up-at}'

# 查詢 job 會計紀錄（需要 slurmdbd 正常運行）
kubectl -n slurm exec pod/slurm-controller-0 -- sacct -X --format=JobID,User,State,CPUTime,Start,End

# 確認 slurmdbd / MySQL 狀態
kubectl -n slurm get pods -l app=slurmdbd
kubectl -n slurm get pods -l app=mysql

# 進 login pod 提交 job
kubectl -n slurm exec -it deploy/slurm-login -- bash
```

---

# 📊 Evaluation Metrics

| 指標 | 描述 | 目標 |
|------|------|------|
| Provisioning Latency | 從 job 提交到 worker pod ready 的時間 | < 30 秒 |
| Recovery Time | 節點故障到訓練恢復的時間 | < 60 秒 |
| Resource Efficiency | 任務結束後閒置資源回收速度 | 任務結束 1 分鐘內釋放 |
| Scheduling Overhead | Operator 本身的 CPU/Memory 佔用 | < 5% 總資源 |

---

# 🧱 Tech Stack

| 類別 | 工具 |
|------|------|
| 環境 | Ubuntu 24.04 + k3s |
| 容器編排 | Kubernetes |
| HPC 排程器 | Slurm (slurmctld + slurmd)，MpiDefault=pmi2 |
| 節點認證 | Munge |
| Elastic Operator | Python 3.11 + Slurm REST API (slurmrestd) + Kubernetes Python SDK |
| 會計後端 | slurmdbd + MySQL 8.0（job CPU-hours / 使用者統計 / Fair-Share 前置）|
| 共享儲存 | NFS + nfs-subdir-external-provisioner + RWX PVC |
| 網路介面 | Multus CNI + secondary NIC (net2) |
| MPI | OpenMPI 4.1.2 + Slurm PMI2 整合 |
| 模組系統 | Lmod 6.6；modulefile 以 K8s ConfigMap 管理，掛載至 `/opt/modulefiles/` |
| 監控 | Prometheus + Grafana + slurm-exporter + kube-state-metrics + Alertmanager |
| 告警 | 8 條 SLO 規則（provisioning latency、queue wait、flapping 等） |

---

# 🔭 Roadmap

> Phase 5 已完成（Lmod + Helm chart cutover）；Phase 6 的 score-based scheduling 主線已完成到 M8 evaluation。下一步重點是 Phase 7 補齊使用者體驗（端到端 trace + SSH Login），以及把 Phase 6 的 live-cluster E7 驗證與 production rollout policy 補完。
> 目前以**單一使用者**情境為主，多租戶（Fair-Share / 帳號配額）為更後期擴充方向。

## Phase 6：自訂 Slurm 排程策略 ✅ M1-M8 完成

> 目標是針對本平台特有的 DDP / MPS / 跨 pool 共用情境，加入超出原生 Slurm backfill 的排程邏輯。詳細 roadmap 與驗收紀錄見 [`docs/scheduler.md`](docs/scheduler.md#phase-6-roadmap--score-based-scheduling-開發追蹤r17)，evaluation writeup 見 [`docs/eval-writeup.md`](docs/eval-writeup.md)。

- **M1-M3：Slurm + Lua score path**：Helm values expose backfill / multifactor knobs；`job_submit.lua` 實作 `mps_fit`、`vram_fit`、fragmentation proxy，將 score 作為 priority kicker。
- **M4-M6：trace replay + runtime predictor**：`sim/` 可離線重播 Philly-like trace；FastAPI + LightGBM predictor 可由 Lua submit plugin 呼叫並覆寫過鬆的 `--time`。
- **M7：fragmentation requeue**：Operator 加入 fragmentation detector / decider，預設 `shadowMode=true`，可觀察解卡決策後再開實際 `scontrol requeue`。
- **M8：evaluation**：`eval/results/`、`eval/figures/` 與 `docs/eval-writeup.md` 已產出；目前主要結論是 E5（score + predictor + fragmentation）相對 vendor multifactor mean JCT 改善約 28.6%。
- **仍待補強**：E7 live-cluster 50-job 驗證、checkpoint resume cost 實測、更多 trace / sensitivity sweep，以及 M9 optional weight tuner。

## Phase 7：分散式追蹤 + SSH Login 📋 規劃中

### 7-A：OpenTelemetry 分散式追蹤

**目標：** 一個 AI job 從提交到完成的完整鏈路變成一條可視化的 Trace，讓使用者清楚看到時間花在哪裡（排隊、K8s 啟動、實際執行）。

```
[sbatch submit] → [pending in queue] → [Operator scale-up decision]
  → [K8s pod provisioning] → [slurmd registration] → [job execution]
    → [checkpoint write] → [Operator scale-down] → [job complete]
```

每個 span 攜帶 `job_id`、`pool`、`gres`、`provisioning_latency` 等 attribute，用 Grafana Tempo 可視化。這是目前所有 Slurm-on-K8s 開源方案都沒有做到的端到端觀測視角。

**需要做的事：**
- Operator 加入 `opentelemetry-sdk`，在 `scale_action`、`loop_observation` 事件上建立 span
- 部署 Grafana Tempo（chart `templates/monitoring/` 加 `enabled` flag）
- Prometheus histogram exemplar → Tempo 連結，從 latency spike 直接跳到對應 trace

### 7-B：SSH Login

**現狀問題：** 目前登入 login node 需要執行 `kubectl exec`，使用者必須先安裝 kubectl 並取得 kubeconfig。

**目標：** 使用者用標準 SSH 直接進入 login pod，不需要知道 K8s 的存在。

```
使用者電腦 → ssh -p 2222 user@<k3s-host-ip>
                  ↓
           NodePort :2222 → slurm-login pod
                              ├── sbatch / squeue / sinfo
                              └── /shared/（NFS 掛載，模型 + 輸出共用）
```

**需要做的事：**
- `docker/login/Dockerfile` 加入 `openssh-server`，設定 SSH key 認證（禁用密碼登入）
- `chart/templates/login.yaml` Service 改為 NodePort，固定 port 2222
- Login 容器啟動腳本加入 SSH host key 初始化（`ssh-keygen -A`）
- 後續（多租戶時）：`scripts/add-user.sh` 同時在 Linux 和 Slurm（`sacctmgr`）建帳號

---

# 📝 References

- [Slurm Workload Manager Documentation](https://slurm.schedmd.com/)
  - [Slurm Plugin API](https://slurm.schedmd.com/plugins.html)
- [PyTorch Distributed Elastic](https://docs.pytorch.org/docs/stable/distributed.elastic.html)
- [Kubernetes Operator Pythonic Framework (Kopf)](https://github.com/nolar/kopf)
- [Converged Computing: Integrating HPC and Cloud Native](https://www.computer.org/csdl/magazine/cs/2024/03/10770850/22fgId5NFpC)
- [Running Slurm on Amazon EKS with Slinky](https://aws.amazon.com/tw/blogs/containers/running-slurm-on-amazon-eks-with-slinky/)
- [Gang Scheduling](https://kubernetes.io/docs/concepts/scheduling-eviction/gang-scheduling/)
- [Workload Aware Scheduling](https://kubernetes.io/blog/2025/12/29/kubernetes-v1-35-introducing-workload-aware-scheduling/)
- [Slinky Project](https://github.com/slinkyproject)
- [Slonk: Slurm on Kubernetes for ML Research at Character.ai](https://blog.character.ai/slonk/)
- [Prometheus Slurm Exporter](https://github.com/vpenso/prometheus-slurm-exporter)
- [AWS ParallelCluster](https://github.com/aws/aws-parallelcluster)
- [Lmod: An Environment Module System](https://github.com/TACC/Lmod)
- [kube-scheduler Scoring](https://kubernetes.io/docs/reference/scheduling/config/)
- [Grafana](https://grafana.com/)
- [Kube State Metrics](https://github.com/kubernetes/kube-state-metrics)
