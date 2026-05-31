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

部署統一使用 Helm；目前實機部署固定以 Linux + k3s + GPU 為目標，主要 values 使用 `chart/values-k3s.yaml`。`chart/values.yaml` 保留為 chart default，不作為目前的實際部署路徑。

> Helm chart 名為 `slurm-platform`，把 namespace、ConfigMap、controller/worker StatefulSet、operator、login、NetworkPolicy、device-plugin-config、monitoring（Prometheus/Grafana/Alertmanager/exporters）、storage（NFS subdir provisioner + RWX PVC）全部納入。GPU Operator 因為 PSS=privileged 需求，透過 `scripts/deploy-2.sh` 裝到自己的 `gpu-operator` namespace。完整背景見 [`docs/note.md §5-A`](docs/note.md)。

> 驗證環境：Ubuntu 24.04 x86\_64 + k3s v1.34 + RTX 4070 + NVIDIA driver 580 ✅️

## 1. 準備 k3s/GPU 部署前置資源

`deploy-1.sh` 會整合原本部署步驟 1~4，並輸出時間戳 log：

- 檢查 Linux、NVIDIA driver、Docker、k3s、kubectl、Helm 與 kubeconfig
- 建置 controller、worker、operator、slurm-exporter 映像
- 匯入映像到 k3s containerd
- 建立或重用 munge、ssh、JWT secrets（由 deploy-1.sh 內建處理）
- 套用 NVIDIA RuntimeClass 與 Slurm accounting backend（mysql + slurmdbd）

```bash
export KUBECONFIG=~/.kube/config
bash scripts/deploy-1.sh
```

若主機尚未完成 Linux + k3s + GPU 基礎安裝，先執行 `sudo bash scripts/setup-linux-gpu.sh --k3s`。一般重跑部署時可用下列環境變數略過已完成的階段：

```bash
SKIP_BUILD=1 SKIP_IMPORT=1 bash scripts/deploy-1.sh
SKIP_SECRETS=1 SKIP_PREREQS=1 bash scripts/deploy-1.sh
REGENERATE_SECRETS=true SKIP_BUILD=1 SKIP_IMPORT=1 SKIP_PREREQS=1 bash scripts/deploy-1.sh
```

## 2. 主機 NFS server + LAN exports (Optional)

```bash
sudo bash scripts/setup-nfs-server.sh
cat /etc/exports                       # 必須含 pod CIDR (10.0.0.0/8) AND LAN subnet
sudo exportfs -ra
```

## 3. 部署平台、GPU Operator 與 DSAC Scheduler

`deploy-2.sh` 會把平台主體、GPU Operator 與 live DSAC scheduler 一次收斂到最終狀態。它會先 build/import `slurm-rl-scheduler:m11`，再用一次 `helm upgrade --install` 部署 `slurm-platform` 並直接開啟 DSAC live 設定，最後用一次 Helm install/upgrade 收斂 NVIDIA GPU Operator；不需要額外 rollout restart。

```bash
export KUBECONFIG=~/.kube/config
bash scripts/deploy-2.sh
```

一般重跑時可用下列環境變數略過已完成的階段：

```bash
SKIP_BUILD=1 SKIP_IMPORT=1 bash scripts/deploy-2.sh
SKIP_GPU_OPERATOR=1 bash scripts/deploy-2.sh
SKIP_WAIT=1 bash scripts/deploy-2.sh
```

DSAC scheduler 會讓 `job_submit.lua` 在 `sbatch` 時呼叫 `/decide`；`shadowMode=false` 代表 DSAC 回傳的 `priority_boost` 會實際加到 `job_desc.priority`。`valueAbstain=-100000` 與 `snapshotTtlSeconds=86400` 是目前單機 live 實驗設定，用來避免 checkpoint value scale 與缺少常駐 snapshot collector 時讓所有 decision 都被 guardrail 擋掉。

預設行為（k3s overlay）：

- `gpu.enabled=true`：在 `gpu-operator` namespace 放 device-plugin-config ConfigMap + 跨節點 labeler Job
- `monitoring.enabled=true`：Prometheus + Alertmanager + Grafana + kube-state-metrics + slurm-exporter（namespace `monitoring`）
- `storage.enabled=true` + `nfsServer=192.168.0.111`：NFS subdir provisioner + StorageClass `slurm-shared-nfs` + 20Gi RWX PVC

LAN IP 不一樣時用 `VALUES_FILE=<your-values.yaml>` 或 Helm values 檔調整 `storage.nfsServer`。GPU Operator 使用 `driver.enabled=false` 與 `toolkit.enabled=false`，因為 host 已經由 `setup-linux-gpu.sh` 裝好驅動與 NVIDIA Container Toolkit。

目前 live scheduler 主線是 **DSAC**。`services/rl_scheduler/smoke_ppo.py` 只是歷史 PPO smoke test，用來快速檢查 simulator API 與 SB3 相容性；不是目前 live cluster 使用的演算法。

> 注意：目前 `slurm-rl-scheduler:m11` 映像會載入 `runs/eval_mlp_20260514-210824/train/dsac.pt`。若要換成新的 DSAC checkpoint，更新 `services/rl_scheduler/Dockerfile` 的 `COPY ... /models/dsac.pt` 後重新執行 `bash scripts/deploy-2.sh`。

**選用功能**（在 `chart/values-k3s.yaml` 開啟）：

| 功能 | 設定 | 說明 |
|------|------|------|
| SSH Login | `login.ssh.authorizedKeys: \|` + 公鑰 | `ssh -p 30022 root@192.168.0.111` |
| OpenTelemetry | `monitoring.otel.enabled: true` | 部署 Tempo + OTel Collector，Grafana 自動加 datasource |

```bash
# 快速加 SSH key（不需重新 helm install）
bash scripts/add-ssh-key.sh add "ssh-ed25519 AAAA... user@laptop"

# 啟用 OTel（helm upgrade）
helm upgrade slurm-platform ./chart -f chart/values-k3s.yaml -n slurm \
  --set monitoring.otel.enabled=true
```

## 4. 驗證 live cluster

`verify-live.sh` 會在 Linux + k3s + GPU live 環境一次完成部署後驗證，涵蓋 chart render、核心 workload rollout、NFS RWX、GPU/GRES、Prometheus/Grafana、DSAC smoke job 與 Lmod 基本檢查。

```bash
export KUBECONFIG=~/.kube/config
bash scripts/verify-live.sh
```

需要略過特定驗證時可用環境變數：

```bash
SKIP_HELM_RENDER=1 bash scripts/verify-live.sh
SKIP_STORAGE=1 SKIP_GPU=1 bash scripts/verify-live.sh
SKIP_MONITORING=1 SKIP_DSAC_SMOKE=1 SKIP_LMOD=1 bash scripts/verify-live.sh
```

## 5. DSAC 訓練與評估

> 以下步驟需要 `.venv-m11`（含 PyTorch）。`PYTHONPATH=.` 確保 `sim/` 和 `services/` 可被找到。

### 快速訓練（本機 CPU）

```bash
# 預設：500k steps, n-step=10, PER, potential shaping, CQL=0.1
PYTHONPATH=. .venv-m11/bin/python -m services.rl_scheduler.sim_train \
    --n-nodes 1 --gpus-per-node 1 \
    --trace philly burst ali \
    --total-steps 500000 \
    --out-dir runs/dsac_sim_$(date +%Y%m%d)

# 加 GPU 加速
PYTHONPATH=. .venv-m11/bin/python -m services.rl_scheduler.sim_train \
    --device cuda --total-steps 500000 \
    --curriculum \
    --out-dir runs/dsac_cuda_$(date +%Y%m%d)

# 2×2 DRL 實驗：需搭配 chart/values-2x2.yaml 與新的 dsac.pt checkpoint。
PYTHONPATH=. .venv-m11/bin/python -m services.rl_scheduler.sim_train \
    --n-nodes 2 --gpus-per-node 2 \
    --trace philly burst ali \
    --total-steps 500000 \
    --out-dir runs/dsac_2x2_$(date +%Y%m%d)
```

### 完整評估（3 families × 5 seeds，對比 score baseline）

```bash
# 完整評估（所有改進開啟，CUDA）
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --n-nodes 1 --gpus-per-node 1 \
    --total-steps 500000 \
    --trace-families philly burst ali \
    --seeds 42 43 44 45 46 \
    --device cuda \
    --curriculum \
    --out-dir runs/eval_dsac_$(date +%Y%m%d-%H%M%S)

# 僅 MLP（無 attention，停用 shaping/PER 作為 ablation baseline）
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --no-attention --no-per --no-potential-shaping --cql-alpha 0 \
    --total-steps 200000 --device cuda \
    --out-dir runs/eval_mlp_ablation_$(date +%Y%m%d-%H%M%S)

# IQN critic（quantile Huber loss）
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --use-iqn --device cuda \
    --out-dir runs/eval_iqn_$(date +%Y%m%d-%H%M%S)

# 載入已有 checkpoint，跳過訓練直接評估
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --ckpt runs/dsac_sim/dsac.pt --no-train
```

### 架構與改進 flags 對照

| Flag | 說明 | 預設 |
|------|------|------|
| `--curriculum` | n_jobs 從 10→30→50 漸進 | 關 |
| `--no-per` | 停用 Prioritized Experience Replay | PER 開 |
| `--no-potential-shaping` | 停用 per-step 等待時間 shaping | Shaping 開 |
| `--cql-alpha` | CQL 正則化係數（0=停用） | 0.1 |
| `--use-iqn` | IQN critic（quantile Huber loss） | 關 |
| `--no-attention` | MLP Q-network（非 attention） | 關 |

### 執行單元測試

```bash
.venv-m11/bin/python -m pytest sim/tests/ -q
```

---

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

`monitoring.enabled=true`（k3s overlay 預設打開；chart default 預設關閉）。

```bash
# 存取 Grafana
kubectl -n monitoring port-forward svc/grafana 3000:3000

# 驗證 Prometheus 抓得到 slurm-exporter / operator / kube-state-metrics
bash scripts/verify-live.sh
```

## Lmod 模組系統（已整合至核心）

Lmod 已整合進 `docker/controller` 與 `docker/worker` image，`deploy-2.sh` 部署後即可使用 `module load`。Modulefile 定義由 chart 管理，live 驗證由 `scripts/verify-live.sh` 覆蓋。

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

`scripts/verify-live.sh` 會檢查 login pod 內 `module avail`、`module load openmpi/4.1`、`MPI_HOME`、`SLURM_MPI_TYPE` 與 `module purge`。

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
| 6：自訂 Slurm 排程 | ✅ 開發中 | DRL 模型排程器插件 |
| 7：分散式追蹤 + SSH Login | 🔄 進行中 | SSH Login（NodePort + key auth）✅；OpenTelemetry trace（Tempo + admin_comment propagation）📋 規劃中 |

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

## Phase 7：分散式追蹤 + SSH Login

### 7-A：OpenTelemetry 分散式追蹤 📋 規劃中

**目標：** 一個 AI job 從提交到完成的完整鏈路變成一條可視化的 Trace，讓使用者清楚看到時間花在哪裡（排隊、K8s 啟動、實際執行）。

```
[sbatch submit] → [pending in queue] → [Operator scale-up decision]
  → [K8s pod provisioning] → [slurmd registration] → [job execution]
    → [checkpoint write] → [Operator scale-down] → [job complete]
```

Trace context 傳播方式：`serve.py` 在 `/decide` 時建立 root span，將 W3C traceparent 寫入 Slurm job 的 `admin_comment`；Operator polling loop 讀取後 continue 同一條 trace。詳見 `docs/note.md § 7-A`。

**需要做的事：**
- `serve.py` 加入 `opentelemetry-sdk`，`/decide` 建立 `job_submit` span 並寫入 `admin_comment`
- Operator（`app.py`）讀取 `admin_comment`，continue trace 建立後續 span
- 部署 OTel Collector + Grafana Tempo（`chart/templates/monitoring/`）
- Prometheus histogram exemplar → Tempo 連結，從 latency spike 直接跳到對應 trace

### 7-B：SSH Login ✅ 已完成

使用者可用標準 SSH 直接進入 login pod，不需要安裝 kubectl 或持有 kubeconfig。

```
ssh -p 30022 root@<k3s-host-ip>
       ↓
NodePort :30022 → slurm-login pod
                   ├── sbatch / squeue / sinfo（Slurm 指令即開即用）
                   └── /shared/（NFS 掛載，模型 + 輸出共用）
```

**初次設定 SSH Key（在 k3s host 上執行）：**
```bash
# 1. 生成 key pair（私鑰存在 ~/.ssh/slurm_login_key）
ssh-keygen -t ed25519 -f ~/.ssh/slurm_login_key -C "slurm-login"

# 2. 把公鑰填進 chart/values-k3s.yaml 的 login.ssh.authorizedKeys
cat ~/.ssh/slurm_login_key.pub

# 3. 套用
helm upgrade slurm-platform ./chart -f chart/values-k3s.yaml -n slurm --no-hooks

# 4. 連線
ssh -i ~/.ssh/slurm_login_key -p 30022 root@<k3s-host-ip>
```

**之後新增/移除 key（不需重新 helm install）：**
```bash
bash scripts/add-ssh-key.sh add    "ssh-ed25519 AAAA... user@laptop"
bash scripts/add-ssh-key.sh remove "ssh-ed25519 AAAA... user@laptop"
bash scripts/add-ssh-key.sh list
```

**`chart/values-k3s.yaml` 設定格式：**
```yaml
login:
  ssh:
    nodePort: 30022         # k3s NodePort 範圍 30000-32767；0 = 維持 ClusterIP
    authorizedKeys: |
      ssh-ed25519 AAAA... user@laptop   # 一行一個 key
      ssh-ed25519 AAAA... user@workstation
```

sshd 已加固：`PasswordAuthentication no`、`PermitRootLogin prohibit-password`（key-only）。

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
