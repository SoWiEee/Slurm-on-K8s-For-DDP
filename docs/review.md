# HPC / AI Infra 架構審查報告（v4 — Phase 5 完成後）

> **評估對象：** Phase 1–5 全部完成（Lmod + Helm chart cutover + GPU Operator MPS）；Phase 6/7 尚未開工
> **評估時間：** 2026-05-05（v4 — 本次）
> **評估視角：** HPC 叢集工程師 + K8s SRE + ML systems
> **本輪範圍：** 安全議題暫不審（依使用者要求）；v3 已修項目移除，只保留**仍在的議題 + 本輪新發現**
>
> 本輪審查的兩個重點：
> 1. **找出阻擋系統真實上線使用的工程缺口**（drain timeout / 監控盲點 / 升級路徑）
> 2. **找出能讓這套系統「出色」的差異化機會** — 對照 Slinky / SUNK / Slonk / AWS ParallelCluster，這個專案有什麼獨家賣點可以放大

---

## 0. 執行摘要

Phase 5 完成後，**部署可重複性（Helm cutover）和 GPU 共享（GPU Operator MPS）兩件事已經到位**。剩下的差距集中在三層：

| 層 | 差距 | 對應章節 |
|---|---|---|
| 維運可靠度 | drain timeout、單點 SPOF、image preload、ConfigMap reload、節點 label drift | §2 §3 §5 |
| 觀測縱深 | 沒有真實 GPU SM%/VRAM/PCIe 指標、沒有 per-job profile、沒有 trace | §6 |
| 工程體驗 | 沒有 chart artifact 發佈、沒有快照備份、沒有 chaos 測試 | §7 §8 |

對照 README 動機：

| 動機 | 兌現度（v4） | 主要剩餘缺口 |
|---|:---:|---|
| 利用率（MPS 70%+） | 🟢 90% | 缺 DCGM exporter，數字驗證不了 |
| 隔離性（CPU/GPU 池獨立） | 🟢 95% | partition 已拆，QoS / preempt 未啟 |
| 彈性（縮回 0 / 擴出） | 🟡 70% | drain timeout 未做、image pull 冷啟 30s+ 未優化 |
| 容錯（Checkpoint guard / NFS） | 🟡 65% | controller SPOF、MySQL 無備份、NFS 無 alt-path |

**本輪新發現（按 P0 → P3 排序，標 ★ 是「做了會出色」而非「不做會壞」）：**

| # | 議題 | 類別 | 嚴重度 | 性質 |
|---|---|---|:---:|:---:|
| R1 | Operator scale-down 仍無 drain timeout（v3 N8 沿用） | 彈性 | 🔴 P0 | bug |
| R2 | scale-up 冷啟 image pull 沒 preload，第一次擴 GPU pod 30–60s | 彈性 | 🟠 P1 | 性能 |
| R3 | slurm.conf 改變後沒有自動 `scontrol reconfigure`，需手動或 pod 重建 | 維運 | 🟠 P1 | bug |
| R4 | `nvidia.com/device-plugin.config` node label 由 post-install Job 一次性打，**節點重建即遺失** | GPU | 🟠 P1 | bug |
| R5 | 沒有 DCGM exporter，「GPU utilization」panel 顯示的是 Slurm allocated GPU 數而非真實 SM% | 觀測 | 🟠 P1 | 觀測盲點 |
| R6 | Operator 是 single replica + 沒 leader election（v3 3-C 沿用、變嚴重） | 可靠度 | 🟠 P1 | SPOF |
| R7 | Workers 沒設 `resources.requests/limits`（v3 6-A 沿用），對 cgroup accounting 失效 | K8s | 🟠 P1 | 沿用 |
| R8 | `gres.conf` 缺 `Cores=` 拓撲宣告，cons_tres 沒法做 GPU↔CPU 親和性 binding | 排程 | 🟡 P2 | 性能 |
| R9 | `ProctrackType=proctrack/linuxproc` 跟 `task/affinity` 在 GPU job 殺進程不乾淨（v3 N11 沿用） | Slurm 設定 | 🟡 P2 | bug |
| R10 | StatefulSet `RollingUpdate` 預設策略，chart upgrade 會中斷 running job | 維運 | 🟡 P2 | bug |
| R11 | Login pod 無 resource limit，使用者可 fork bomb 拖垮整個 node | K8s | 🟡 P2 | 沿用變體 |
| R12 | `slurmd -Dvvv`（DEBUG3）長期跑會把 log volume 灌爆 | 維運 | 🟡 P2 | 雜訊 |
| R13 | Slurm 21.08 cgroup v2 支援不完整 — Ubuntu 22.04 image 將是長期技術債 | 升級路徑 | 🟡 P2 | tech debt |
| R14 | NFS 沒有 `mountOptions: [hard, intr, rsize=1M, wsize=1M]`，DDP I/O 性能堪憂 | 儲存 | 🟡 P2 | 性能 |
| R15 | Checkpoint guard 只認單一 file path，rotation / 多檔案 ckpt 不認得 | 彈性 | 🟢 P3 | bug |
| R16 ★ | **缺 OTel job-lifecycle trace** — Phase 7 計畫，但這正是與 Slinky/SUNK 的差異化點 | 差異化 | ★ | feature |
| R17 ★ | **缺 score-based / ML-aware scheduling** — Phase 6 預留，是論文級貢獻 | 差異化 | ★ | feature |
| R18 ★ | **缺端到端 chaos / failure injection 測試** — 沒有任何 Slurm-on-K8s 開源方案做這個 | 差異化 | ★ | feature |
| R19 ★ | **缺 sbatch wrapper 自動填 `--time` / `--mem`**（接 §9 ML 預測） | 差異化 | ★ | feature |
| R20 ★ | **缺 GPU job profile dashboard**（per-job DCGM panel + 連結到 Grafana Tempo） | 差異化 | ★ | feature |

---

## 1. 儲存與 I/O

### 2-A. NFS DDP I/O 瓶頸

問題不變：Phase 3 把 NFS RWX PVC 暴露給所有 pod，DDP checkpoint（13 GB / 次）寫進 NFS 會 stall。**目前沒有任何 templates / 文件警告使用者這件事。**

最低限度修法：
1. `chart/values.yaml` 加 `storage.fastLocalPath`（hostPath 或 emptyDir.medium=Memory）給 ckpt 用
2. README 在 templates 章節加警告：「checkpoint 寫 NFS 會 stall 訓練」
3. 提供 `04_finetune_lora.sh` 模板（如果 Phase 7 要重做工作負載模板）示範 hot ckpt 寫 local + 冷封存到 NFS 的兩段策略

### R14：NFS mountOptions 沒調

`chart/templates/storage.yaml` 的 PVC 沒設 `mountOptions`。NFS subdir provisioner 預設是 `vers=4.1, rsize=8K, wsize=8K`，對 DDP 大 ckpt 寫入是災難（每次 8K 一個 RPC round-trip）。

**修法：** StorageClass 加 `mountOptions: [nfsvers=4.1, rsize=1048576, wsize=1048576, hard, intr, timeo=600, retrans=2]`。光這一行對 ckpt 寫入吞吐有 5–10× 改善（NFS 實測）。

### 2-C. MySQL 單點 + 無備份

`slurmdbd` 後端是 fairshare 與長期 accounting 的唯一 source of truth。掛了就全部歷史歸零。最低限度：CronJob 跑 `mysqldump` 到 NFS 或外部 PVC，每天 1 次，保留 7 份。

```yaml
# chart/templates/accounting-backup-cronjob.yaml（新）
apiVersion: batch/v1
kind: CronJob
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: dump
              image: mysql:8
              command: ["sh","-c","mysqldump -h slurmdbd-mysql -u root -p$PW slurm_acct_db | gzip > /backup/$(date +%F).sql.gz"]
```

---

## 2. 故障恢復與可靠性

### R1：Operator scale-down 仍無 drain timeout

`operator/app.py` 仍是「等所有 draining node `cpu_alloc=0` 才縮」。一個 hang 的 srun step 會把整個 pool 永遠卡在 max replicas。

**修法（具體 patch 點）：**
- `operator/models.py::PoolState` 加 `draining_started: dict[str, float]`（node → epoch ts）
- `operator/policy.py` 在判斷 scale-down 時，若 `now - draining_started[n] > drain_timeout` 則 emit `force_scale_down` decision
- `operator/app.py` 收到 `force_scale_down` 時：對每個 stuck node 呼叫 `scancel --nodelist=$n`、`scontrol update state=DOWN reason="drain timeout"`，然後 patch replicas
- `chart/values.yaml` 加 `operator.drainTimeoutSeconds: 1800`（已存在但未實作）
- 觸發時 emit metric `slurm_operator_drain_timeout_total{pool, node}` + 結構化 log `drain_timeout_force_kill` → 接 Alertmanager

### R2：scale-up 冷啟 image pull 沒 preload

第一次擴 GPU pod 時，`slurm-worker:latest` 從 local registry pull 約 30–60 秒（image 含 CUDA + OpenMPI + Lmod，輕鬆超過 4GB）。Operator scale-up 完到 slurmd ready 的端到端 latency 主要被 image pull 吃掉。

**修法（兩條路）：**
1. **DaemonSet image-puller**（推薦）：chart 加一個 `image-puller-daemonset.yaml`，nodeSelector 挑 GPU node，container 是 `pause` 但 image 設成 `slurm-worker:latest`。kubelet 會幫你 pull 一次，之後 `IfNotPresent` 直接用 local cache。
2. 用 `kubectl debug node` / `crictl pull` 在 cluster bootstrap 時手動 pull 到所有 node。

差異：第一次擴 60s → 第一次擴 5s。對 README 動機三「彈性」是直接的兌現。

### R3：slurm.conf 改變後沒有自動 reconfigure

`chart/templates/configmap-nodes.yaml` 的 ConfigMap 變動會被 kubelet 同步（~60s 延遲），但**slurmctld 不會自動讀新檔**。要嘛重啟 controller pod，要嘛手動 `scontrol reconfigure`。

helm upgrade 改 `pools[].replicas` / `maxNodes` / partition 設定後，行為是「controller 還用舊設定、worker 已經拿新 NodeName」— mismatch，scale 行為錯亂。

**修法選項：**
- A：controller pod 跑 sidecar，watch ConfigMap mtime，變動時 `scontrol reconfigure`
- B：chart 用 helm hook `post-upgrade` 跑一個 Job 呼叫 `scontrol reconfigure`
- C：給 ConfigMap 加 annotation `checksum/config: {{ include "..." . | sha256sum }}`，讓 controller pod 在 ConfigMap 變動時 rolling restart（最簡單）

推薦 C，一行 annotation 解決。

### 3-C. Controller SPOF（搭配 R6 升級）

slurmctld single replica + StateSaveLocation 在 PVC。短期（Phase 5 後）可以接受；長期要做 backup controller（Slurm 內建 `BackupController` + `BackupAddr`）。

**最低限度可以先做的：** chart 加 `slurmctld-state-snapshot` CronJob，每小時 `tar` 一份 StateSaveLocation 到 NFS。controller 重建時可以 restore，把 RTO 從「全部 job 重提」降到「< 1 小時」。

### R6：Operator single replica + 無 leader election

`chart/templates/operator.yaml` Deployment replicas=1。Operator 重啟期間（pod restart / image upgrade / node 故障）所有擴縮決策停擺。雖然有 `slurm.k8s/last-scale-up-at` annotation 救 cooldown，但**整個 polling loop 中斷期間 pending job 不會被服務**。

**修法（兩條路，按工程量）：**
- 短期：保持單副本，但加 `terminationGracePeriodSeconds: 60` + 健康的 readinessProbe，把重啟 RTO 控制在 30s 內
- 長期：Active-passive — 兩副本 + `coordination.k8s.io/Lease` leader election（Python `kubernetes-client` 有 `leaderelection` module）。Slurm 操作必須 single-writer 所以一定是 active-passive 不是 active-active

對學術 demo 短期那條夠用，但 thesis defense 時被問「operator 掛了會怎樣」要有答案。

### R15：Checkpoint guard 只認單一 file path

`operator/policy.py` 的 checkpoint guard 用 `os.stat(checkpoint_path).st_mtime` 判斷 freshness。問題：

- PyTorch 常見模式是 `ckpt-{step}.pt`（rotation）
- DeepSpeed 是整個目錄 `global_step{N}/`
- 多 ckpt 並存（best.pt + latest.pt）

目前實作對這些都不認得。**修法：** 改成 glob pattern 支援 + 取目錄/匹配中最新的 mtime。

```python
# operator/policy.py
import glob
def latest_ckpt_age(pattern: str) -> float | None:
    matches = glob.glob(pattern)
    if not matches:
        return None
    return time.time() - max(os.path.getmtime(m) for m in matches)
```

`chart/values.yaml` 的 checkpoint pattern 從 string 改 list：

```yaml
operator:
  checkpointGuard:
    patterns:
      - /shared/jobs/*/checkpoints/*.pt
      - /shared/jobs/*/global_step*/
```

---

## 3. 排程策略

### R8：gres.conf 缺 Cores= 拓撲宣告

`chart/templates/_helpers.tpl` 產生的 gres.conf 沒寫 `Cores=`：

```
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 File=/dev/nvidia0
```

**問題：** cons_tres 在分配 GRES 時會嘗試把 GPU 與 CPU 綁同一 NUMA / socket，但**前提是 gres.conf 宣告了 GPU 對應的 CPU core**。沒有 `Cores=` 就退化成隨機綁 CPU。對單 GPU + DDP collective 影響大（NCCL 的 CPU helper thread 跨 NUMA 會掉 ~10–20% 通訊頻寬）。

**修法：**

```
# gres.conf
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 File=/dev/nvidia0 Cores=0-3
```

對單 socket 4 core worker，Cores=0-3 等於整 pod。對未來多 GPU node 才是真實效益，但**現在加進 helper 的成本是 0**，未來免遷移。

### 4-A/B/C：QoS / Preemption / Fairshare 全未啟

Phase 6 真的開工前，建議先做最小可用 QoS — 至少 `normal` / `high` 兩級，搭配 `PriorityWeightQOS`。這跟 Phase 6 的自訂排程不衝突，是它的前置。

```yaml
slurm:
  qos:
    enabled: true
    levels:
      - {name: normal, priority: 100}
      - {name: high,   priority: 1000, preempt: [normal], preemptMode: REQUEUE}
```

### R9：ProctrackType=linuxproc + TaskPlugin=task/affinity 不一致

values.yaml 註解寫了 Slurm 21.08 cgroup v2 支援不完整所以避開 task/cgroup。**但 proctrack/linuxproc + task/affinity 的組合在 GPU job 殺進程仍然不乾淨**（特別是 NCCL helper thread / CUDA driver thread）。

長期解：升 Slurm 到 23.11+（cgroup v2 完整）+ 切 proctrack/cgroup + task/cgroup。但這要升 base image。短期能做的就是 documentation：在 `note.md` 開個「known limits」段落明示「kill 不乾淨會看到 zombie」是預期行為。

---

## 4. GPU 管理

### R4：node label drift — `nvidia.com/device-plugin.config` 重建即遺失

`chart/templates/gpu/node-labeler-job.yaml` 是一次性 Job，部署時把 `nvidia.com/device-plugin.config=rtx4070-mps` 打到 node。**Node 重建（k3s 重灌、節點重新加入、雲商重置）後 label 消失**，GPU Operator 退到 default 配置（無 sharing），rtx4070 變成 1 slot。

**修法（按工程量）：**
- 短期：把 Job 改成 CronJob（每小時 reconcile 一次）
- 中期：用 `node-feature-discovery` 的 NodeFeatureRule，根據 hardware feature（PCI VID `0x10de` + Device ID）自動打 label
- 長期：寫個小 mutating webhook 看到新 node 加入就根據 hostname pattern 打 label

**對學術專題：** 短期方案夠了，但 thesis 要寫 "production readiness" 要提到。

### 5-C. Lmod conflict / NCCL 模組

modulefiles ConfigMap 沒 `conflict` directive — 同時 `module load cuda/11.8 cuda/12.1` 不會被擋。對單使用者影響小，但 thesis 上 demo 會被問。

NCCL 也沒有獨立 modulefile：CUDA 包進去算了，但分開更教科書。

**修法：** 加 `chart/templates/lmod-modulefiles.yaml`（或加 keys 到既有 ConfigMap）：

```lua
-- /opt/modulefiles/cuda/12.1
conflict("cuda")
prepend_path("PATH", "/usr/local/cuda-12.1/bin")
prepend_path("LD_LIBRARY_PATH", "/usr/local/cuda-12.1/lib64")
setenv("CUDA_HOME", "/usr/local/cuda-12.1")
```

### 5-D. 雙網路（移除）

k3s flannel 不支援 Multus。`docs/note.md` 與 chart 都沒提到 Phase 5 之後是否要驗證。**現實：對單機 2 GPU 場景沒意義，DDP collective 走 loopback / 共享記憶體，根本不出 host**。建議：把 5-D 從 roadmap 移除（或標記為「multi-host 才做」），減少視覺雜訊。

---

## 5. Kubernetes 整合

### R7：Workers 沒設 resources.requests/limits

`chart/templates/workers.yaml` 對 GPU pool 有設 `nvidia.com/gpu: 1`，但 **CPU / memory 完全沒設 requests 或 limits**。

後果：
- Pod QoS 是 `BestEffort`，node memory pressure 時最先被 evict
- cgroup `memory.max` = node 全量，OOM 殺手鎖定的是整個 node 而非 pod
- Slurm 視角的 `RealMemory=3500` 跟 K8s cgroup 沒對齊，job memory 超用 K8s 不會擋
- HPA / VPA 完全失效（不過我們用 Slurm 不用 HPA）

**修法：** workers.yaml 從 `pool.cpus` / `pool.realMemory` 自動推導 K8s requests/limits，且要 ≥ Slurm 宣告值（避免 cgroup 比 Slurm 嚴）。

```yaml
resources:
  requests:
    cpu: {{ $pool.cpus }}
    memory: {{ printf "%dMi" (mul $pool.realMemory 1) }}
  limits:
    cpu: {{ $pool.cpus }}
    memory: {{ printf "%dMi" (mul $pool.realMemory 2) }}  # 2x burst headroom
```

### R10：StatefulSet RollingUpdate 預設策略中斷 running job

```yaml
# chart/templates/workers.yaml — 沒設 updateStrategy
spec:
  serviceName: ...
```

預設是 `RollingUpdate`，partition=0 — `helm upgrade` 改 worker image / template 會**從最高 ordinal 往下逐個重建 pod**，running job 全部 NODE_FAIL。

**修法：**

```yaml
spec:
  updateStrategy:
    type: OnDelete   # operator 控制重建時機
```

配套：operator 加新指令 `rolling_upgrade_pool(pool_id)`：drain → scale-down by 1 → wait → delete pod → wait ready → scale-up → drain 下一個。chart upgrade 不做就好（user 自己決定何時 rolling）。

### R11：Login pod 無 resource limit

login pod 是使用者 shell 的家。沒設 limit 的話 fork bomb / accidentally `python -c 'list(range(10**10))'` 會把整個 node 打死，連帶撞 controller。

**修法：** chart/templates/login.yaml 加 `resources.limits.{cpu, memory}`，至少 4 CPU / 8 GB。

### R12：`slurmd -Dvvv` 太吵

`chart/templates/workers.yaml` 啟動 slurmd 用 `-Dvvv`（DEBUG3）。長期跑會把 kubelet 的 log volume 灌爆（每天每 worker ~500MB）。debug 期 OK，跑久了要關。

**修法：** chart/values.yaml 加 `slurm.daemonLogLevel: info`，預設 info，debug mode 才開 verbose。

### 6-C. Static pre-declared nodes

代價在 v3 已寫。Phase 6 真要做動態 partition 就要重新評估這個架構決策；目前決定不動。

---

## 6. 可觀測性

### R5：缺真實 GPU 指標 — DCGM exporter

目前 Grafana panel "GPU Utilization" 顯示的是 slurm-exporter 看到的 `allocated_gpu` 數字（Slurm 視角），**不是 NVIDIA SM%、VRAM 使用、PCIe bandwidth**。

對 README 動機一「利用率 70%+」的兌現完全失效 — 你目前無法回答「這張 GPU 真的在算嗎、還是空轉？」

**修法：部署 NVIDIA DCGM exporter**

GPU Operator 內建選項可開 dcgm-exporter（`--set dcgm.enabled=true`），會起一個 DaemonSet 吐：
- `DCGM_FI_DEV_GPU_UTIL` — SM 使用率（真實）
- `DCGM_FI_DEV_FB_USED` — VRAM 已用
- `DCGM_FI_DEV_POWER_USAGE` — 功耗
- `DCGM_FI_DEV_PCIE_TX/RX_THROUGHPUT` — PCIe 吞吐
- `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` — Tensor Core 使用率（最接近 ML workload 真實使用率）

**chart 整合：**
- `scripts/install-gpu-operator.sh` 加 `--set dcgm.enabled=true`
- `chart/templates/monitoring/prometheus-config.yaml` 加 DCGM scrape job
- Grafana 加 DCGM dashboard（NVIDIA 官方 ID `12239`）

### 7-B. 無 per-job tracking（搭配 R20）

Prometheus 的 GPU 指標是 per-device，沒辦法 join 回 Slurm job_id。要做 per-job：DCGM exporter 支援 `pod` 標籤、再用 slurm-exporter 的 job→pod mapping 串起來。**這是 Phase 7-A trace 的必要前置**（trace span attribute 要能 join metric）。

### R20 ★（新）：GPU job profile dashboard — 差異化機會

對接 R5 + 7-B 之後可以做這個：每個 sbatch job 有自己的 Grafana panel，顯示：

- 該 job 整個 lifetime 的 SM% / VRAM / PCIe / Tensor Core 使用率時序
- 該 job 的 Slurm 排隊時間 / scale-up latency / 實際執行時間（從 OTel trace pull）
- 該 job 的 ckpt I/O bandwidth（從 NFS metrics pull）

**為什麼出色：**

| 對比 | 是否有 | 備註 |
|---|---|---|
| Slinky | ❌ | 只有 cluster-level dashboard |
| SUNK | ❌ | 同上 |
| Slonk | ❌ | 沒釋出 dashboard |
| AWS ParallelCluster | ⚠️ | 有 CloudWatch per-instance，但沒 join 到 job_id |
| 本專案（如果做） | ✅ | per-job × per-GPU × full-lifecycle |

對 thesis 是「實作 + 評估章節最強的單一視覺化證據」。

---

## 7. Helm Chart 與部署

### R16 ★（新）：缺 chart artifact 發佈管道

目前 chart 在 git repo 裡，使用者要 `git clone` 才能 helm install。對 thesis demo 沒問題，對 "production readiness" 要提的是：

- **GitHub Pages chart repo**（最簡單）：用 `helm/chart-releaser-action` 把 chart 推到 gh-pages
- **OCI registry**：`helm push slurm-platform-1.0.0.tgz oci://ghcr.io/<user>/charts`

對學術專題是錦上添花；對開源散播是必要。

### R18 ★（新）：缺端到端 chaos / failure injection 測試 — 差異化機會

`chart/tests/` 有 28 條 helm-unittest，但全部是「render output 對不對」。**沒有任何 e2e 測試在驗證「故障 → 恢復」**。

可以加的（按 thesis 價值排序）：

| 場景 | 怎麼觸發 | 期望行為 |
|---|---|---|
| Worker pod 中途被 K8s evict（preStop hook 是否生效） | `kubectl delete pod` mid-job | Operator 偵測、scale-up 補位、job 自動 requeue |
| Controller pod restart | `kubectl delete pod slurm-controller-0` | StateSaveLocation PVC restore、restart < 30s、queue 不丟 |
| NFS server 短暫離線 | `kubectl scale deploy nfs-server --replicas=0; sleep 60; scale=1` | 寫 ckpt 的 job hang 但不 fail、恢復後續寫成功 |
| Operator pod restart 期間提交 job | `kubectl delete pod slurm-operator-0; sbatch ...` | 重啟後 cooldown 從 annotation 還原、第一個 poll 把 pending 處理掉 |
| 同時提交 50 個 mps:25 job | 壓力測試 | bin-packing 正確、所有 job 能在合理時間內完成 |
| Image pull 失敗（模擬 registry 掛） | `kubectl drain` + 改 image tag | Operator 不會把 pool 永遠卡在 provisioning |

**為什麼出色：** 沒有任何 Slurm-on-K8s 開源方案做這個。Slinky / SUNK 文件全部沒有 chaos test 章節。對 thesis evaluation 是「我能量化我的容錯主張」。

實作建議：寫 `scripts/chaos/*.sh`，每個是一個情境，能獨立跑；CI 跑其中一兩個快的當 smoke test。

### Helm 5-A 已完成的後續清理項目

- `verify-helm.sh` 還在，但 legacy parity diff 段已移除 — 確認沒遺留
- chart `Chart.yaml` 的 `appVersion` 應寫 Slurm 版本（如 `21.08.5` 或將來升 23.11.x），目前還是 placeholder

---

## 8. Operator 設計

### Operator polling vs K8s watch — 性能盲點

`operator/app.py` 是 polling-based（15s 一輪）。對 cluster < 50 pod 完全 OK，但**任何 K8s 事件（pod evicted、node NotReady）都要等下一輪 poll 才被看到**。Phase 7 真要做 OTel trace 端到端 latency 量測時，這 15s 會變成最大誤差源。

**改進方向（不是 P0，但 thesis 寫 systems contribution 可以提）：**

- 用 `kubernetes.client.watch.Watch` watch StatefulSet + Pod events，event-driven 處理 + 定時 reconcile（k8s controller 標準模式）
- Polling 從 15s 降到 60s（reconcile 兜底），event-driven 處理瞬時事件

工程量約 1 週，但**這個改造會讓 trace 的 scale-up latency 從 15s 誤差降到 < 1s**，對 R16 的 trace 品質有質的提升。

---

## 9. 升級路徑與技術債

### R13：Slurm 21.08 是長期技術債

我們鎖在 21.08 是因為 Ubuntu 22.04 base image 預設 apt 版本就是它。但：

| 議題 | 21.08 行為 | 23.11+ 行為 |
|---|---|---|
| cgroup v2 | 不完整支援、所以走 linuxproc | 完整支援、能切 task/cgroup |
| `--gres=gpu:rtx4070:1,mps:25` 混合 GRES | ❌ 拒絕（cons_tres 不支援組合） | ✅ 支援 |
| slurmrestd v0.0.40+ | 沒有 | 有，scheduler 操作更豐富 |
| AccountingStorageTRES 對 mps 的 reporting | 部分 buggy | fixed |

**升級路徑：**
- `docker/controller/Dockerfile` 與 `docker/worker/Dockerfile` 換 base 到 Ubuntu 24.04（apt 預設 23.11.x）
- 或 build from source（已有 lmod 範例可參考），鎖 23.11.10
- helm-unittest 加 regression 測試確保 slurm.conf 對新版本還合法

對 thesis 不急，但寫進「未來工作」章節有分量。

### R19 ★（新）：sbatch wrapper / submit plugin — 差異化機會

接 §9 ML runtime predictor + §8 Lua submit plugin。把使用者的 `sbatch foo.sh` 自動補：

- `--time` ← runtime predictor
- `--mem` ← 預測 + 歷史 max memory
- 適合的 `--partition` ← 根據 GRES 推
- `--qos` ← 根據使用者 / 帳號自動

**為什麼出色：** AWS ParallelCluster / Slinky 都沒做。HPC 中心（NERSC、ALCF）有做但不開源。

---

## 10. 業界比較（v4 更新）

| 面向 | 本專案 v4（Phase 5 完成） | Slinky | SUNK | AWS ParallelCluster | Volcano |
|---|:---:|:---:|:---:|:---:|:---:|
| Helm 一條指令部署 | ✅ | ⚠️（多 chart） | ✅ | n/a | ✅ |
| GPU MPS sharing | ✅（GPU Operator） | ⚠️（只 timeSlicing） | ✅ | ✅ | ✅ |
| 端到端 Job lifecycle trace（OTel） | ❌（Phase 7-A） | ❌ | ❌ | ❌ | ❌ |
| Per-job × per-GPU × full-lifecycle dashboard | ❌（R20） | ❌ | ❌ | ⚠️ | ❌ |
| ML-aware scheduling（runtime / score） | ❌（Phase 6 / R17）| ❌ | ❌ | ❌ | ⚠️ |
| Chaos / failure injection test suite | ❌（R18） | ❌ | ❌ | ⚠️ | ❌ |
| Checkpoint-aware scale-down | ✅ | ❌ | ❌ | ❌ | ❌ |
| Drain timeout（hang job 不卡死） | ❌（R1） | ⚠️ | ✅ | ✅ | ✅ |
| Fairshare / QoS 啟用 | ❌ | ✅ | ✅ | ✅ | ✅ |
| HA Controller | ❌ | ✅ | ✅ | ✅ | ✅ |
| 共享 FS | NFS（瓶頸） | StorageClass | Lustre | FSx | StorageClass |

**競爭力分析：**

- 已勝出：**Checkpoint-aware scale-down**（沒人做）、**MPS env propagation via TaskProlog**（這個專案是我看過的開源裡寫得最完整的）、**Helm cutover 完整度**
- 容易補上、補了會勝出：**OTel trace（R16）**、**per-job dashboard（R20）**、**chaos suite（R18）**、**ML-aware scheduling（R17 / R19）**
- 短期沒打算追平：HA controller（3-C）、Fairshare（4-B）、共享 FS 升級

**結論：** 把 R16-R20 五個 ★ 項目做掉一兩個，這個專案就有獨家賣點足以發學術論文 / 上社群分享。具體最高 CP 值組合是 **R16（OTel trace）+ R20（per-job dashboard）**：兩者技術上強相關、加起來大概 3 週工期、產出視覺化 demo 力強。

---

## 11. 改進優先順序總表（v4）

僅列**仍開放**項目；安全章節整段省略。★ 是「做了會出色」而非「不做會壞」。

| 優先 | 項目 | 類別 | 難度 | 對應動機 |
|:---:|---|---|:---:|:---:|
| **P0** | R1：Operator drain timeout（v3 N8 沿用） | 彈性 | 中 | 彈性 |
| **P0** | R3：ConfigMap checksum annotation → controller rolling restart | 維運 | 低 | — |
| **P0** | R4：node label drift（CronJob reconcile） | GPU | 低 | 利用率 |
| **P1** | R2：image-puller DaemonSet | 彈性 | 低 | 彈性 |
| **P1** | R5：DCGM exporter + Grafana panel | 觀測 | 中 | 利用率（驗證） |
| **P1** | R6：Operator leader election（短期：reduce restart RTO；長期：active-passive） | 可靠度 | 中 | 容錯 |
| **P1** | R7：Workers 加 resources.requests/limits | K8s | 低 | 容錯 |
| **P1** | R10：StatefulSet OnDelete + operator rolling | 維運 | 中 | 彈性 |
| **P1** | R14：NFS mountOptions tuning | 儲存 | 低 | DDP I/O |
| **P1** | 2-C：MySQL 備份 CronJob（沿用） | 儲存 | 低 | Fairshare 持久 |
| **P1** | 3-C：StateSaveLocation snapshot CronJob（沿用） | 容錯 | 低 | 容錯 |
| **P2** | R8：gres.conf Cores= | 排程 | 低 | NCCL 親和性 |
| **P2** | R9：cgroup v2 / proctrack/cgroup（前置 R13） | Slurm | 中 | — |
| **P2** | R11：Login pod resource limit | K8s | 低 | — |
| **P2** | R12：slurmd log level | 維運 | 低 | — |
| **P2** | R15：Checkpoint guard 多 pattern | 彈性 | 低 | 容錯 |
| **P2** | 4-A/B/C：QoS / Preempt / Fairshare 啟用（Phase 6 前置） | 排程 | 中 | — |
| **P2** | 5-C：Lmod conflict + NCCL 模組（沿用） | HPC | 中 | — |
| **P3** | R13：Slurm 升 23.11.x（換 base image） | tech debt | 高 | — |
| **P3** | 5-D：Multi-host 才做的 Multus / Cilium / SR-IOV（建議從 roadmap 移除） | 網路 | 高 | DDP（multi-host） |
| **★** | R16：OTel job-lifecycle trace（Phase 7-A） | 差異化 | 高 | 可觀測性 |
| **★** | R17：Score-based / ML-aware scheduling（Phase 6） | 差異化 | 高 | 利用率 / 排程 |
| **★** | R18：Chaos / failure injection test suite | 差異化 | 中 | 容錯（驗證） |
| **★** | R19：sbatch wrapper 自動填 --time / --mem | 差異化 | 中 | 易用性 |
| **★** | R20：Per-job × per-GPU × full-lifecycle dashboard | 差異化 | 中 | 利用率（視覺化） |

---

## 12. 給校內專題（thesis）的建議切入順序

整理上面所有 P0/P1/★ 後，對單人 1 學期工程量的最務實順序：

```
Week 1–2：把 P0 全做完（R1 / R3 / R4）— 系統穩定性基線
Week 3–4：R5 + R20 — DCGM + per-job dashboard，視覺化「能講故事」
Week 5–7：R16 OTel trace（Phase 7-A 起步）— 取得 trace 資料
Week 8–10：R17 score-based scheduling（Phase 6 起步）— 用 trace 資料當輸入
Week 11–12：R18 chaos suite — evaluation 章節的容錯數字
Week 13–14：寫 thesis、跑 trace replay、整理數字
```

**論文角度的單一最強組合：R5 + R20 + R16 + R17。**

R5/R20 給你「能看的數字」；R16 給你「能聯動的時序資料」；R17 給你「演算法貢獻」。四個一起，evaluation 章節有：

1. **基線比較**：FCFS vs 你的 score function vs 簡化 Gandiva 重排，看 JCT / utilization / fairness
2. **可解釋性**：trace + dashboard 直接展示 score function 在做什麼
3. **容錯主張**：R18 chaos test 給你的 ablation 章節
4. **runtime 預測（R19）**：bonus，做得完就放，做不完不影響主軸

---

*v4 審核以 Phase 5（Lmod + Helm cutover）完成後的真實系統為基礎。安全章節依使用者要求暫不審；下次 v5 預定 Phase 7-A 動工後重審觀測縱深。*
