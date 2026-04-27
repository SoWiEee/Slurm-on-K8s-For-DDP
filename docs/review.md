# HPC / AI Infra 架構審查報告（v3 — MPS migration / k3s 上線前）

> **評估對象：** Phase 1–5 已完成範圍 + `mps-migration` 分支（rtx5070 / rtx4080 + MPS）
> **評估時間：** 2026-04-04（v1）、2026-04-10（v2）、2026-04-26（v3：本次）
> **評估視角：** HPC 叢集工程師 + k3s/K8s SRE
> **環境前提：** v3 撰寫時程式碼仍在 Windows 主機編輯，**Linux + k3s + 真實 GPU 尚未跑過任何驗證**；本輪審核盡量聚焦於可由閱讀 manifest / 腳本判斷的設計缺陷。
>
> 本文件以 README.md 揭示的四大動機（**利用率 / 隔離性 / 彈性 / 容錯**）為審核基準，逐項對照實作是否能兌現該承諾。
> ✅ 已解決並附簡述、⚠️ 部分達成、❌ 設計上未兌現或反而被新 migration 打破。

---

## 0. 執行摘要

本專案以最小依賴（Kind/k3s + StatefulSet + Python Operator）實作了一套彈性 Slurm 叢集，學習性原型完成度高。但對照 README 的四大動機：

| 動機 | 實作狀態 | 主要差距 |
|-----|---------|---------|
| 利用率（MPS 70%+） | ⚠️ Migration 完成設定，但 **K8s 與 Slurm 兩層 GPU 排程互相打架**，實機跑不起來 |
| 隔離性（CPU/GPU 池獨立） | ⚠️ StatefulSet 已分離，但 **partition 仍只有 `debug`**，CPU job 可能落到 GPU node |
| 彈性（縮回 0 / Operator 擴出） | ⚠️ Drain-then-scale 已實作，但 **無 drain 超時**，遇 hang job 永遠縮不回去 |
| 容錯（Checkpoint guard / NFS） | ✅ 大致完成，但 SPOF 與 NFS I/O 瓶頸未解決 |

v3 新增的關鍵風險集中在 **MPS migration 的 K8s 整合層**（§五、§九）與 **k3s 第一次部署的腳本盲點**（§九），這兩塊是上 Linux 機器後最先會踩的坑。

**本輪新發現（migration 相關，按嚴重度排序）：**

| # | 議題 | 類別 | 嚴重度 | 狀態 |
|---|------|-----|:----:|:----:|
| N1 | MPS DaemonSet 與 worker pod 同時 request `nvidia.com/gpu:1`，互搶設備 | GPU/K8s | 🔴 P0 | ✅ |
| N2 | rtx4080 `devicePath=/dev/nvidia1` 在 device-plugin 模式下錯誤 | GPU | 🔴 P0 | ✅ |
| N3 | `maxNodes=2` × 單張實體 GPU → 第二個 pod 永遠 Pending | GPU/K8s | 🟠 P1 | ⬜ |
| N4 | `bootstrap.sh` 對 k3s runtime 仍呼叫 `kind load docker-image`（line 285）| k3s migration | 🔴 P0 | ✅ |
| N5 | NetworkPolicy `allow-operator-egress` 只開 TCP/443，k3s API server 預設 6443 | k3s migration | 🔴 P0 | ✅ |
| N6 | `AccountingStorageTRES` 未設，sacct 不會記 GPU/MPS 用量 → fairshare 失效 | 排程 | 🟠 P1 | ✅ |
| N7 | `partition=debug` 涵蓋所有 CPU/GPU node，無 constraint 的 CPU job 可落到 GPU node | 排程 | 🟠 P1 | ✅ |
| N8 | Operator scale-down 無 drain timeout，hang job 永久阻擋縮回 0 | 彈性 | 🟠 P1 | ⬜ |
| N9 | `hostIPC: true` 在 k3s PSS=baseline 預設下會被 admission 擋下 | k3s migration | 🟠 P1 | ✅（N1 移除 hostIPC + bootstrap.sh 顯式 label baseline）|
| N10 | `nvidia-device-plugin.yaml` 未啟用 sharing/replicas，與 MPS 設計矛盾 | GPU | 🟠 P1 | ✅ |
| N11 | `ProctrackType=proctrack/linuxproc` 與 `TaskPlugin=task/cgroup` 不一致 | Slurm 設定 | 🟡 P2 | ⬜ |
| N12 | `verify-gpu.sh` 預設只跑 `--gres=gpu:rtx5070:1`，不驗證 MPS 路徑 | 驗證 | 🟡 P2 | ✅ |
| N13 | k3s feature gates `GangScheduling,GenericWorkload` 名稱未對 1.35 GA/Alpha 文件覆核 | 排程 | 🟡 P2 | ⬜ |

> **2026-04-27 修復批次：** N1 / N2 / N5 / N7 已 commit；N6（`AccountingStorageTRES`）與 N10（device-plugin sharing.mps）為 N1/N7 的連帶修正，一併標 ✅。
> **2026-04-27 第二批：** N4（`bootstrap.sh` / `bootstrap-monitoring.sh` 改以 `k3s ctr images import` 走 containerd 路徑）、N9（`bootstrap.sh` 顯式為 namespace 設 `pod-security.kubernetes.io/enforce=baseline`，N1 已移除 hostIPC）、N12（`verify-gpu.sh` 新增 step 6 驗證 `--gres=mps:25` 與 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 注入）。詳細修法見各章節下方的「✅ 已修」段落與 [§九 Checklist](#九k3s-第一次部署-checklistv3-新增) 表格更新。

**沿用前兩輪審核已解決項目（保留摘要）：**
StateSaveLocation PVC 持久化、縮容 drain、slurmdbd + MySQL accounting、kubectl→Python SDK、PDB、`MpiDefault=pmi2`、Lmod、`CHECKPOINT_PATH=""` 不再靜默失效、Checkpoint Grace Period、Worker preStop Hook、job output → `/shared/jobs/`、NetworkPolicy Ingress+Egress + 扇出 RPC 處理、Operator 熔斷器 / readinessProbe、Cooldown 持久化（StatefulSet annotation）、slurmctld IP cache 修正（`fix_node_addr()`）。

---

## 一、安全模型

### 1-A. `SlurmUser=root`（沿用 v2，仍未修）

slurmctld、slurmd、slurmrestd 全部以 `root` 跑。一個 munge / Slurm CVE 即等於完整節點控制。MPS migration 後 worker pod 還額外加上 `hostIPC: true`，scope 只擴大不縮小。

### 1-B. JWT lifespan = 10 年（沿用）

`render-core.py` line 330：`scontrol token username=root lifespan=315360000`。Operator 取得的 JWT 可永久操作 REST API，secret 洩漏即為災難。

### 1-C. `SLURMRESTD_SECURITY=disable_user_check`（沿用）

只靠 NetworkPolicy 把 6820 鎖在 namespace 內，但 NetworkPolicy 是 best-effort（取決於 CNI 是否實作）。k3s 預設 flannel 對 NetworkPolicy 支援是有的，但 IPv6 / hostNetwork 等邊角不一定生效。

### 1-D. munge.key 無輪換（沿用）

### ✅ 1-E. NetworkPolicy Egress（v2 修正）

⚠️ **v3 新增警告（→ N5）：** `allow-operator-egress` 只允許 TCP/443 出站到任意位址，這是給 Kind / hosted K8s API 用的設定。**k3s 預設 API server 在 6443**（除非以 `--https-listen-port=443` 重新設定），上 Linux 後 operator 完全打不到 K8s API，整個 reconcile loop 會持續熔斷 backoff。

修法二選一：
1. NetworkPolicy egress 加上 `port: 6443`；或
2. 啟動 k3s 時加 `--https-listen-port=443`（會與其他 443 服務衝突，較不建議）。

建議手段：在 `manifests/networking/network-policy.yaml` 對 operator 的 egress 改為同時允許 443 + 6443，避免 runtime 差異。

---

## 二、儲存與資料持久性

### ✅ 2-0. StateSaveLocation 與 Job Accounting（沿用）

### 2-A. NFS 是 DDP I/O 瓶頸（沿用）

註：README 動機四宣告「NFS PVC 讓結果跨節點持久化」，但若實際要跑模板 `04_finetune_lora.sh`（13 GB/ckpt）就會立刻撞 NFS 頻寬上限。建議在 `templates/` 文件中明示「checkpoint 寫 NFS 是會 stall 訓練的」，或將 finetune 的 ckpt 寫 hostPath。

### ✅ 2-B. Job 輸出 → `/shared/jobs/`（v2 已修）

### 2-C. MySQL 單點 + 無備份（沿用）

⚠️ **v3 強化：** README 動機提到「Fair-Share 排程」，slurmdbd 後端是其前提。MySQL PVC 損毀=> 全部 fairshare 歷史歸零。最低限度應加 `mysqldump` CronJob 到另一個 PVC。

### N6（新）：`AccountingStorageTRES` 未設，GPU/MPS 用量完全不會記帳

`render-core.py` 的 slurm.conf header 缺以下設定：

```ini
AccountingStorageTRES=gres/gpu,gres/mps
```

沒有這行，`sacct -X --format=AllocTRES` 不會回傳 GPU 用量；後續若要做 fairshare（`PriorityType=priority/multifactor`），GPU 工作的權重永遠是 0，CPU 與 GPU 工作會被當成等價計費，違背 README「Fair-Share 排程」前置。

**修法：** `build_slurm_conf()` 在偵測到任何 pool 含 gpu / mps GRES 時，自動把 `AccountingStorageTRES` 加進 header。

---

## 三、故障恢復與可靠性

### ✅ 3-0. 縮容 Drain / PDB / Cooldown 持久化 / IP cache（沿用）

### N8（新）：Scale-down 無 drain timeout，hang job 永久阻擋縮回 0

`operator/app.py::_do_scale_down`：

```python
all_idle = all(self.client.get_node_cpu_alloc(n) == 0 for n in draining)
if all_idle:
    self.actuator.patch_replicas(...)   # 真正縮容
else:
    # log "waiting_for_drain" 並 return
```

問題：只要任何一個 draining node 上仍有 alloc 中的 job（包含死 hang 不退出的 srun step、卡在 epilog），replicas 就**永遠不會降下來**。README 動機三宣告「沒有 job 時 worker pod 自動縮回 0」會被一個爛 job 整個破壞。

**修法：**
1. `PartitionConfig` 新增 `drain_timeout_seconds`（預設 1800s）；
2. `_draining_nodes` 改存 `dict[node_name, drain_started_at]`；
3. 超過 timeout 時：發 `scancel` 給節點上的 job、進 `state=DOWN` 後強制 patch replicas，並 emit `drain_timeout_force_kill` 事件供 Prometheus 告警。

### ✅ 3-A. Worker preStop Hook（沿用）
### ✅ 3-B.  Checkpoint Guard 兩個情境（沿用）
### 3-C. Controller SPOF（沿用）
### ✅ 3-D. Cooldown 持久化（沿用）
### ✅ 3-E. slurmctld IP cache（沿用）

⚠️ **v3 警告：** k3s 預設使用 `flannel`，pod IP 仍為 ephemeral，IP cache 問題在 k3s 一樣會發生。`fix_node_addr()` 是 verify 腳本的 workaround，正式 deploy 路徑（bootstrap.sh）並沒有自動執行；上 k3s 後第一次 worker pod 重啟一定會復現 COMPLETING 卡住問題。建議把 `fix_node_addr()` 從 verify-storage-e2e.sh 抽出成 `scripts/lib/fix-node-addr.sh`，由 operator 在 `_do_scale_up` 之後呼叫一次。

---

## 四、排程策略

### ✅ N7（新）：只有一個 `debug` partition，CPU job 可被排到 GPU node

`render-core.py`：

```python
part_line = f"PartitionName={part['name']} Nodes={','.join(partition_nodes)} Default=YES ..."
# partition_nodes 同時包含 cpu / rtx5070 / rtx4080 全部
```

後果：
- `sbatch hello.sh`（無 constraint）由 Slurm 在所有 5 個節點中挑一個，可能落到剛擴出來的 GPU node。
- 違反 README 動機二「不同類型的工作互不競爭」。
- Operator 看不到差別：CPU 池有 pending → 它擴 CPU，但 Slurm 已把 CPU job 派去 GPU；CPU 池一直被視為 idle。

**修法：** `worker-pools.json` 從單一 partition 改為三個：`cpu`、`gpu-rtx5070`、`gpu-rtx4080`，每個 partition 只包含對應 pool 的 NodeName，並設不同 `MaxTime`、`Priority`。`PARTITIONS_JSON` 同步調整 `partition` 欄位。template `01_preprocess.sh` / `02_batch_infer.sh` 的 `-p` 也要改。

### 4-A. 單 partition / 無 QoS / `MaxTime=INFINITE`（沿用）— 與 N7 同一根因，一起改。
### 4-B. 無 Fairshare（沿用，前置=N6）
### 4-C. 無 Preemption（沿用）
### 4-D. Gang Scheduling — 基礎設施就緒、Operator 整合待實作（沿用）

⚠️ **v3 N13：** `setup-linux-gpu.sh` line 92 對 k3s 啟用 `feature-gates=GangScheduling=true,GenericWorkload=true`。K8s 1.35 文件中 Workload API 的 feature gate 名稱仍在演進，Alpha 版本的 gate name 並非 100% 與 Operator 整合計畫對齊；上線前必須以 `kubectl get --raw='/api/v1' | grep scheduling` 與 `kubectl api-resources | grep workload` 真實確認 API 是否暴露。如果 gate 名稱錯，k3s 啟動仍會成功，但 Workload CRD 不會出現，整個 Phase 5+ Gang Scheduling 計畫會默默無效。

---

## 五、GPU 管理與 MPS 架構（**v3 重點**）

### ✅ 5-0. MpiDefault=pmi2 / Lmod（沿用）

### ✅ N1：MPS DaemonSet 與 Worker Pod 同時要 `nvidia.com/gpu: 1` — 互搶設備

這是本輪審核**最嚴重**的設計缺陷，在 Linux + 真實 GPU 上一定會直接死掉。

證據：

`manifests/gpu/mps-daemonset.yaml` line 81–85：
```yaml
resources:
  limits:
    nvidia.com/gpu: "1"
  requests:
    nvidia.com/gpu: "1"
```

`scripts/render-core.py` line 404–408（`--real-gpu` 路徑）：
```python
gpu_resources = (
    f"\n          resources:\n            limits:\n              nvidia.com/gpu: \"{gpu_count}\""
    f"\n            requests:\n              nvidia.com/gpu: \"{gpu_count}\""
    if is_gpu_pool and args.real_gpu else ""
)
```

`manifests/gpu/nvidia-device-plugin.yaml` 沒有 `sharing`/`timeSlicing`/`mps` 設定，預設行為是 **每張實體 GPU 對應 1 個 `nvidia.com/gpu` resource，且為獨佔**。

於是：
1. MPS DaemonSet 啟動 → 拿走 GPU node 上的 nvidia.com/gpu=1。
2. Operator 擴 rtx5070 worker → 申請 `nvidia.com/gpu: 1` → 整個 cluster 沒有可分配 GPU → **Pod 永久 Pending**。
3. 即使把 DaemonSet 移走，worker pod 內也**沒有 MPS client 連線管道**，因為 `/tmp/nvidia-mps` 是 node-level hostPath，沒有 daemon 寫 socket 進去。

**MPS 在 K8s 上要動起來，正確架構是 MPS-aware sharing：**

選項 A — NVIDIA k8s-device-plugin 0.15+ 內建 MPS sharing：

```yaml
# 透過 ConfigMap 設定 device-plugin
config:
  version: v1
  sharing:
    mps:
      resources:
      - name: nvidia.com/gpu
        replicas: 4   # 一張實體 GPU 暴露成 4 份 nvidia.com/gpu
```

→ 此模式下 device-plugin **自己** 跑 MPS daemon，DaemonSet 不需要、worker pod 也不再需要 hostIPC + /tmp/nvidia-mps 掛載；worker request `nvidia.com/gpu: 1` 拿到的就是「1/4 張 GPU 的 MPS slice」，CUDA_VISIBLE_DEVICES 由 device-plugin 注入。

選項 B — Time-slicing（不需要 MPS daemon、但無 SM% 隔離）：

```yaml
sharing:
  timeSlicing:
    resources:
    - name: nvidia.com/gpu
      replicas: 4
```

→ 適用於沒辦法跑 MPS 的情境（消費級 GPU、driver 版本受限），但無法兌現 README 對 MPS 的承諾。

**建議：放棄目前自架 MPS DaemonSet 的設計**，改用 device-plugin 內建 `sharing.mps`。`manifests/gpu/mps-daemonset.yaml` 和 worker pod 的 `hostIPC: true` + `/tmp/nvidia-mps` 掛載全部移除；`nvidia-device-plugin.yaml` 改為 helm install 並注入 sharing config。Slurm 端 gres.conf 的 `Name=mps Count=100` 仍然保留，作為 Slurm 排程語意；K8s 端的硬切片由 device-plugin 處理。

### ✅ N2：rtx4080 `devicePath=/dev/nvidia1` 是錯的

`worker-pools.json`：
```json
"name": "slurm-worker-gpu-rtx4080",
...
"devicePath": "/dev/nvidia1"
```

`render-core.py` line 67：
```python
device_file = pool.get("devicePath", "/dev/nvidia0") if real_gpu else "/dev/null"
```

進到 gres.conf：
```
NodeName=slurm-worker-gpu-rtx4080-0 Name=gpu Type=rtx4080 Count=1 File=/dev/nvidia1
```

**問題：** NVIDIA device-plugin 把分配到的 GPU 一律以 `/dev/nvidia0` 路徑掛進 container（只暴露被 allocate 的那張），**不論該 GPU 在 host 上原始 index 是 0 還是 1**。pod 內 `ls /dev/nvidia*` 永遠只看到 `nvidia0`。

於是 slurmd 啟動時 `gres/gpu` 的 `File=/dev/nvidia1` 找不到設備 → node 進入 `DRAIN reason=gres/gpu count too low` → operator scale 出來的 GPU node 永遠用不了。

**修法：兩種選項：**

1. **保留 devicePath 概念但內容改為 `/dev/nvidia0`**（兩個 pool 都是 0），靠 K8s 把實體 GPU 對應到誰。問題：Slurm 排程器看到兩個 pool 都宣稱同樣的 file，但實際指向不同 GPU；Slurm 並不在意 file path 本身，它只用 file 做存在性檢查 + cgroup device whitelist。所以這樣可行。
2. **完全移除 `devicePath`，強制 `--real-gpu` 都用 `/dev/nvidia0`**，刪掉 `worker-pools.json` 的 devicePath 欄位。簡單可靠。

推薦修法 2。

### 🟠 N3：`maxNodes=2` × 一張實體 GPU → 第二個 pod 永遠 Pending

`worker-pools.json` rtx5070 與 rtx4080 都 `maxNodes: 2`。但開發者只有 1 張 RTX 5070、1 張 RTX 4080。在沒有 device-plugin sharing 的情況下，每個 pool 可成功 schedule 的 pod 上限就是 1。

實作影響：
- `slurm.conf` 會宣告 4 個 GPU node（每 pool 兩個），其中 2 個永遠 Pending → sinfo 看到一半 DOWN/UNKNOWN。
- Operator 嘗試 scale 到 2 時 K8s 吐 `0/N nodes available: 1 Insufficient nvidia.com/gpu`，但 operator 沒有特殊處理 → 進 `_provisioning` 永遠等不到 ready → `_PROVISIONING_LATENCY` 不會記錄。

**修法（與 N1 整合）：** 啟用 device-plugin `sharing.mps replicas: 4` 後，rtx5070 pool maxNodes 可改為 4（一張卡切 4 份）；rtx4080 不啟用 sharing 則維持 1。

### 5-A. gres.conf File 真實 GPU（v2 提及）

`--real-gpu` 已改 `/dev/nvidia0`，本項在 v2 部分修復；但與 N1 / N2 / N3 連動，最終結論仍是要全面以 device-plugin 為準，gres.conf 只負責 Slurm 排程語意。

### 5-B. 缺乏 MIG 支援（沿用）— RTX 系列消費卡無 MIG，學術用途可忽略。

### 5-C. Lmod conflict / NCCL 模組（沿用）

### 5-D. DDP 雙網路（沿用）

⚠️ **v3 警告：** k3s 預設 CNI 是 flannel，不支援 Multus。`manifests/networking/dual-subnet-*.yaml` 在 k3s 上要先 `kubectl apply` Multus DaemonSet 才會有 NAD CRD。`scripts/setup-linux-gpu.sh --k3s` 完全沒提到這件事。如果 v3+ 想驗證雙網路，bootstrap-gpu.sh 要加一個 `--with-multus` 旗標。

### N10（新）：`nvidia-device-plugin.yaml` 沒設 sharing → 與 MPS 設計不相容

詳見 N1。manifest 要嘛升級到 helm chart 形式（v0.17+ 支援透過 ConfigMap 注入 sharing config），要嘛在 DaemonSet template 內掛 ConfigMap 並設 `--config-file=/config/config.yaml`。

### N11（新）：`ProctrackType=proctrack/linuxproc` 與 `task/cgroup` 不一致

`render-core.py` header：
```ini
ProctrackType=proctrack/linuxproc       # 不論 real_gpu 都這樣
TaskPlugin=task/cgroup                  # 只有 real_gpu 時
```

cgroup task plugin 預期搭配 `proctrack/cgroup`。混用 `linuxproc` 會造成 job 結束時 process tree 殺不乾淨（特別是 mpi 子進程），與 `KillOnBadExit` 設定組合下會出現孤兒進程。

**修法：** `--real-gpu` 時 ProctrackType 也切到 `proctrack/cgroup`。

### ✅ N12（新）：`verify-gpu.sh` 沒測 MPS

`scripts/verify-gpu.sh` 預設 `GPU_GRES=gpu:rtx5070:1`。整支腳本沒有 `--gres=mps:25` 的測項。本次 migration 主打 MPS，但 verify 不驗。

**修法：** 新增 step 6 ─ 提交一個 `--gres=mps:25` 的 sbatch，從 job 環境檢查 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25`、`CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps` 是否被 Slurm prolog 正確注入。

---

## 六、Kubernetes 整合

### 6-A. BestEffort QoS（沿用）

### ✅ 6-B. 熔斷器 / readinessProbe（沿用）

### 6-C. Static Pre-declared Nodes 代價（沿用）

⚠️ **v3 強化：** N3 把這個代價放大了——當 maxNodes 大於實體可調度數時，slurmctld log 會持續刷 `Node X not responding`（每 SlurmdTimeout=300s 一次），對 alertmanager 規則會誤觸 `flapping` 告警。

### 6-D. 無 CRD（沿用）

### ✅ N9（新）：k3s 預設 PSS=baseline，`hostIPC: true` 會被 admission 擋下

k3s 1.35 預設啟用 Pod Security Admission（PSS），namespace 沒 label 時是 `restricted` profile，**`restricted` 與 `baseline` 都禁止 `hostIPC`**。要等到把 namespace label 為 `pod-security.kubernetes.io/enforce=privileged` 才能跑 MPS worker / MPS DaemonSet。

`scripts/bootstrap.sh` 完全沒處理這件事。Linux 上第一次跑會看到 admission webhook 拒絕：

```
Error from server (Forbidden): error when creating ...:
pods "slurm-worker-gpu-rtx5070-0" is forbidden:
violates PodSecurity "baseline:latest": host namespaces (hostIPC=true)
```

**修法：** bootstrap.sh 在 `kubectl apply -f manifests/core/slurm-static.yaml` 前加：
```bash
kubectl label --overwrite namespace "$NAMESPACE" \
    pod-security.kubernetes.io/enforce=privileged \
    pod-security.kubernetes.io/warn=privileged
```

長期建議：N1 改成 device-plugin sharing 後就不再需要 hostIPC，namespace 可回到 baseline。

### N4（新）：bootstrap.sh 對 k3s runtime 仍呼叫 `kind load docker-image`

```bash
# bootstrap.sh line 282–285
docker build "${build_flags[@]}" -t slurm-elastic-operator:latest -f docker/operator/Dockerfile .

log "loading operator image to kind..."
kind load docker-image slurm-elastic-operator:latest --name "$CLUSTER_NAME"
```

對核心 image（line 169–175）有區分 k3s / kind 路徑，但 **operator image 完全沒區分**。k3s 跑這行會直接 fail（沒有 kind binary、也找不到 cluster name）。

**修法：複製核心 image 的判斷邏輯：**
```bash
if [[ "$K8S_RUNTIME" != "k3s" ]]; then
  kind load docker-image slurm-elastic-operator:latest --name "$CLUSTER_NAME"
else
  # k3s containerd: 用 ctr image import 或 k3s ctr
  docker save slurm-elastic-operator:latest | sudo k3s ctr images import -
fi
```

監控/exporter image 也要同樣處理（`scripts/bootstrap-monitoring.sh` 應自查）。

---

## 七、可觀測性

### ✅ 7-0. Prometheus + Grafana（沿用 v2 Phase 4）

### 7-A. 缺 GPU 指標（沿用）

⚠️ **v3 強化：** README 動機一宣稱「utilization 從 < 20% 提升至 70%+」，但目前**沒有任何 metric 在量這個比例**。要兌現這個敘述，DCGM Exporter + Grafana panel `DCGM_FI_DEV_GPU_UTIL{job=...}` 是不可省略的。Phase 4 的 dashboard 已有 a10/h100 panel 雛形（v3 改名為 rtx5070 / rtx4080），但 datasource 是 slurm-exporter 而非 DCGM；目前 panel 顯示的「GPU utilization」其實是 slurm 視角的「allocated GPU 數」，不是真正的 SM 使用率。Dashboard 文件需明示這點。

### 7-B. 無 per-job tracking（沿用）

---

## 八、Helm Chart 規劃 5-A 審查

`docs/note.md §5-A` 已採 Monolithic chart + `pools` 為有序 list 的設計。從 HPC + GitOps 角度的補充意見：

| 議題 | 建議 |
|-----|-----|
| `_helpers.tpl` 產生的 `slurm.conf` 是 ConfigMap 一個 key，**改一個 pool replicaMax 就會 rolling update 所有 worker** | 把 slurm.conf 拆成兩個 ConfigMap：`slurm-config-static`（ClusterName/Auth/Plugin）+ `slurm-config-nodes`（NodeName/PartitionName）；worker 只 mount 後者 |
| `pools` 為 list 在 Helm `--set` CLI 改起來很痛（`--set 'pools[1].maxReplicas=2'`） | 在 chart 加 `pools.json` 形式的 ConfigMap + initContainer 渲染，或提供 `values-dev.yaml` 預設模板讓使用者 fork |
| 沒提到 chart test (`helm test`) | `templates/tests/` 加一個 Pod 跑 `scontrol ping` + `sinfo`，整合進 CI |
| 沒提到 chart 版本與 Slurm 版本綁定 | `Chart.yaml` 的 `appVersion` 欄位寫 Slurm 版本（如 `23.11.7`），升 Slurm 時透過 chart upgrade 觸發 rolling restart |
| 兩個 values overlay (`values-k3s.yaml` / `values-dev.yaml`) 會與 GPU sharing config 強相關 | 建議把 GPU device-plugin sharing config 也納入 chart（subdir `templates/gpu/`），不要再用獨立 `manifests/gpu/` |

長期來看，`render-core.py` + `worker-pools.json` 應在 Helm 上線後**完整刪除**（含 bootstrap.sh 中所有 render 邏輯），避免兩條 source of truth。

---

## 九、k3s 第一次部署 Checklist（v3 新增）

把上述各項從 SRE 操作面整理成必修清單：

| # | 項目 | 來源 | 狀態 |
|---|------|------|:----:|
| 1 | 把 `slurm` namespace 標為 `pod-security.kubernetes.io/enforce=baseline`（N1 移除 hostIPC 後 baseline 已足夠）| N9 | ✅ |
| 2 | NetworkPolicy operator egress 加 6443 | N5 | ✅ |
| 3 | bootstrap.sh operator/exporter image 對 k3s 走 `k3s ctr images import` | N4 | ✅ |
| 4 | 改用 `nvidia-device-plugin` `sharing.mps` 模式，移除自建 MPS DaemonSet | N1 | ✅ |
| 5 | `worker-pools.json` 移除 `devicePath` 欄位（或全改 `/dev/nvidia0`）| N2 | ✅ |
| 6 | `maxNodes` 對齊 device-plugin 的 sharing replicas | N3 | ⬜ |
| 7 | slurm.conf 加 `AccountingStorageTRES=gres/gpu,gres/mps` | N6 | ✅ |
| 8 | `--real-gpu` 同時切 `ProctrackType=proctrack/cgroup` | N11 | ⬜ |
| 9 | `partition` 從單一 `debug` 拆成 `cpu` / `gpu-rtx5070` / `gpu-rtx4080` | N7 | ✅ |
| 10 | Operator scale-down 加 drain timeout | N8 | ⬜ |
| 11 | verify-gpu.sh 補 MPS 驗證 step | N12 | ✅ |
| 12 | 確認 K8s 1.35 Workload API feature gate 名稱 | N13 | ⬜ |
| 13 | `fix_node_addr()` 整合到 operator scale-up 路徑 | 3-E v3 強化 | ⬜ |

建議在 Linux 機器跑 `bash scripts/bootstrap.sh` 之前，**先把 1 / 2 / 3 / 9 改完**（這四項直接影響第一次部署能不能成功）；4 / 5 / 6 / 10 在 GPU 工作真正運行前必須改完；其餘項目可在後續驗證階段補。

---

## 十、業界比較（沿用 + 微調）

| 面向 | 本專案（mps-migration） | Volcano | OpenOnDemand+Slurm | AWS ParallelCluster |
|------|:---:|:---:|:---:|:---:|
| Gang Scheduling | ⚠️ 基礎設施就緒 | ✓ | ✗ | ✓ |
| GPU 資源感知（device-plugin sharing） | ❌（自建 MPS 設計衝突）| ✓ | ✓ | ✓ |
| MPS 整合 | ⚠️ 設定到位但 K8s 整合錯 | n/a | 手動 | ✓ |
| Fairshare | 前置缺（N6） | ✓ | ✓ | ✓ |
| 縮容前 Drain | ✓（無 timeout，N8） | n/a | ✓ | ✓ |
| HA Controller | ✗ | ✓ | ✓ | ✓ |
| 共享 FS | NFS（瓶頸） | StorageClass | Lustre/GPFS | FSx |
| Helm 化 | 規劃中（5-A）| ✓ | n/a | n/a |

---

## 改進優先順序總表（v3）

| 優先 | 項目 | 類別 | 難度 | 對應 README 動機 | 狀態 |
|:---:|------|-----|:---:|:---:|:---:|
| **P0** | N1：device-plugin `sharing.mps` 取代自建 MPS DaemonSet | GPU | 中 | 利用率 | ✅ |
| **P0** | N2：移除 / 統一 `devicePath=/dev/nvidia0` | GPU | 低 | 利用率 | ✅ |
| **P0** | N4：bootstrap.sh operator image 走 k3s 路徑 | k3s migration | 低 | — | ✅ |
| **P0** | N5：NetworkPolicy operator egress 加 6443 | k3s migration | 低 | — | ✅ |
| **P0** | N9：namespace label baseline（顯式設定） | k3s migration | 低 | — | ✅ |
| **P0** | 1-A：`SlurmUser=root` | 安全 | 低 | — | ⬜ |
| **P1** | N3：maxNodes 對齊 sharing replicas | GPU | 低 | 利用率 | ⬜ |
| **P1** | N6：`AccountingStorageTRES` | 排程 | 低 | Fair-Share | ✅ |
| **P1** | N7：partition 拆 cpu/gpu-rtx5070/gpu-rtx4080 | 排程 | 低 | 隔離性 | ✅ |
| **P1** | N8：drain timeout | 彈性 | 中 | 彈性 | ⬜ |
| **P1** | N10：device-plugin sharing config | GPU | 中 | 利用率 | ✅ |
| **P1** | 1-B：JWT lifespan 1 天 + 輪換 | 安全 | 中 | — |
| **P1** | 6-A：所有 Pod 加 resources.requests/limits | K8s 整合 | 低 | 容錯 |
| **P2** | N11：ProctrackType=proctrack/cgroup | Slurm 設定 | 低 | — |
| **P2** | N12：verify-gpu.sh 補 MPS step | 驗證 | 低 | 利用率 | ✅ |
| **P2** | N13：覆核 K8s 1.35 Gang feature gate 名 | 排程 | 低 | — |
| **P2** | 4-A/B/C：QoS / Preemption / Fairshare 啟用 | 排程 | 中 | Fair-Share |
| **P2** | 7-A：DCGM Exporter + Grafana | 可觀測性 | 中 | 利用率（驗證） |
| **P2** | 5-C：Lmod conflict + NCCL 模組 | HPC | 中 | — |
| **P3** | Helm chart 5-A 完整化 | 部署 | 高 | 易部署 |
| **P3** | 3-C：HA Backup Controller | 故障恢復 | 高 | 容錯 |
| **P3** | 2-C：MySQL 備份 CronJob | 儲存 | 低 | Fair-Share 持久 |
| **P3** | Lustre/BeeGFS 取代 NFS | 儲存 | 高 | 容錯（DDP I/O）|
| **P3** | 5-D：Cilium + Multus + SR-IOV | 網路 | 高 | DDP |

---

*v3 審核以 mps-migration 分支與 Linux + k3s 上線前的可預見風險為主軸。本文件為學習用途架構審查；上線後待新增 v4，補充實機觀測到的非預期行為。*
