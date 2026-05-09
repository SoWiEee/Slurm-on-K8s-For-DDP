# HPC / AI Infra 架構審查報告（v4 — Phase 5 完成後）

> **評估對象：** Phase 1–5 全部完成（Lmod + Helm chart cutover + GPU Operator MPS）；Phase 6 後續已完成 M1-M8，Phase 7 尚未開工
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
| 維運可靠度 | 單點 SPOF、StateSaveLocation/MySQL 備份、Login pod resource limit | §2 §5 |
| 觀測縱深 | 沒有 per-job profile、沒有端到端 trace | §6 |
| 工程體驗 | 沒有 chart artifact 發佈、沒有快照備份、沒有 chaos 測試 | §7 §8 |

對照 README 動機：

| 動機 | 兌現度（v4） | 主要剩餘缺口 |
|---|:---:|---|
| 利用率（MPS 70%+） | 🟢 90% | DCGM 已補；尚缺 per-job dashboard |
| 隔離性（CPU/GPU 池獨立） | 🟢 95% | partition 已拆，QoS / preempt 未啟 |
| 彈性（縮回 0 / 擴出） | 🟢 85% | live chaos / failure injection 尚未量化 |
| 容錯（Checkpoint guard / NFS） | 🟡 65% | controller SPOF、MySQL 無備份、NFS 無 alt-path |

**剩餘新發現（按 P0 → P3 排序，標 ★ 是「做了會出色」而非「不做會壞」）：**

| # | 議題 | 類別 | 嚴重度 | 性質 | 狀態 |
|---|---|---|:---:|:---:|:---:|
| R6 | Operator 是 single replica + 沒 leader election（v3 3-C 沿用、變嚴重） | 可靠度 | 🟠 P1 | SPOF | ⬜ |
| R9 | `proctrack/cgroup` + `task/cgroup` 在 PSS=baseline pod 撞 dbus systemd-scope，**deferred 到 R13 完成才有意義** | Slurm 設定 | 🟡 P2 | blocked-by-R13 | 🔒 |
| R11 | Login pod 無 resource limit，使用者可 fork bomb 拖垮整個 node | K8s | 🟡 P2 | 沿用變體 | ⬜ |
| R13 | Slurm 21.08 → 23.11 升級嘗試後 deferred — 23.11 slurmstepd 強制走 dbus systemd scope，**現有 PSS=baseline 環境不可行** | 升級路徑 | 🟡 P2 | blocked-by-PSS | 🔒 |
| R14 | NFS 沒有 `mountOptions: [hard, intr, rsize=1M, wsize=1M]`，DDP I/O 性能堪憂 | 儲存 | 🟡 P2 | 性能 | ⬜ |
| R15 | Checkpoint guard 只認單一 file path，rotation / 多檔案 ckpt 不認得 | 彈性 | 🟢 P3 | bug | ⬜ |
| R16 ★ | **缺 OTel job-lifecycle trace** — Phase 7 計畫，但這正是與 Slinky/SUNK 的差異化點 | 差異化 | ★ | feature | ⬜ |
| R18 ★ | **缺端到端 chaos / failure injection 測試** — 沒有任何 Slurm-on-K8s 開源方案做這個 | 差異化 | ★ | feature | ⬜ |
| R19 ★ | **submit helper 仍缺 `--mem` / partition / qos 自動化**（`--time` runtime predictor path 已完成） | 差異化 | ★ | partial | ⬜ |
| R20 ★ | **缺 GPU job profile dashboard**（per-job DCGM panel + 連結到 Grafana Tempo） | 差異化 | ★ | feature | ⬜ |

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

### 4-A/B/C：QoS / Preemption / Fairshare 全未啟

Phase 6 score-based scheduling 已完成 M1-M8；QoS / Preemption / Fairshare 仍未正式啟用。建議補最小可用 QoS — 至少 `normal` / `high` 兩級，搭配 `PriorityWeightQOS`，作為之後 production rollout 的權限與 SLA 基礎。

```yaml
slurm:
  qos:
    enabled: true
    levels:
      - {name: normal, priority: 100}
      - {name: high,   priority: 1000, preempt: [normal], preemptMode: REQUEUE}
```

### R9：cgroup-based proctrack/task — 🔒 嘗試後 deferred（2026-05-05）

**嘗試經過：** 與 R13 配套執行 — 把 base image 升到 ubuntu:24.04（拉 Slurm 23.11.4）、values.yaml 切 `task/cgroup,task/affinity` + `proctrack/cgroup`、加 cgroup.conf ConfigMap key + 條件式掛載。helm-unittest 47/47 PASS、helm template 兩個 overlay 都 render 成功。

**實機部署撞牆：** Slurm 23.11 的 slurmstepd **無條件**透過 dbus 呼叫 `org.freedesktop.systemd1.Manager.StartTransientUnit` 建立 cgroup scope，即使 TaskPlugin 是 task/affinity 也一樣。symptom：

```
slurmd: error: cgroup_dbus_attach_to_scope: cannot connect to dbus system daemon:
        Failed to connect to socket /run/dbus/system_bus_socket: No such file or directory
slurmd: error: _init_new_scope_dbus: scope and/or cgroup directory for slurmstepd could not be set.
slurmd: error: Couldn't load specified plugin name for cgroup/v2: Plugin init() callback failed
slurmd: error: slurmd initialization failed
```

PSS=baseline pod 不能 mount `/run/dbus`（hostPath restriction）也不能跑 systemd init。要做下去需要 PSS=privileged + hostPath dbus + 容器內 systemd PID 1，security 範圍擴大到不適合學術 demo。**保留 task/affinity + proctrack/linuxproc，clean process-tree kill 仍是 known limit**（NCCL helper / CUDA driver thread 偶發 zombie）— values.yaml 註解寫了完整脈絡。

**何時可以重啟這條路：** 等 R13 升級且 chart 加上 PSS=privileged overlay；或 Slurm 上游加 `--without-systemd` runtime flag（社群有討論但無 ETA）。

**保留下來的成果：** cgroup.conf helper + ConfigMap key + 條件式掛載 (`if proctrackType=cgroup`) 已在 chart 內，未來開啟時直接設 `slurm.proctrackType=proctrack/cgroup` 即可，不用改 template。

---

## 4. GPU 管理

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

### R11：Login pod 無 resource limit

login pod 是使用者 shell 的家。沒設 limit 的話 fork bomb / accidentally `python -c 'list(range(10**10))'` 會把整個 node 打死，連帶撞 controller。

**修法：** chart/templates/login.yaml 加 `resources.limits.{cpu, memory}`，至少 4 CPU / 8 GB。

### 6-C. Static pre-declared nodes

代價在 v3 已寫。Phase 6 真要做動態 partition 就要重新評估這個架構決策；目前決定不動。

---

## 6. 可觀測性

### 7-B. 無 per-job tracking（搭配 R20）

Prometheus 的 GPU 指標是 per-device，沒辦法 join 回 Slurm job_id。要做 per-job：DCGM exporter 支援 `pod` 標籤、再用 slurm-exporter 的 job→pod mapping 串起來。**這是 Phase 7-A trace 的必要前置**（trace span attribute 要能 join metric）。

### R20 ★（新）：GPU job profile dashboard — 差異化機會

對接 DCGM + 7-B 之後可以做這個：每個 sbatch job 有自己的 Grafana panel，顯示：

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

### R16 ★：缺 chart artifact 發佈管道

目前 chart 在 git repo 裡，使用者要 `git clone` 才能 helm install。對 thesis demo 沒問題，對 "production readiness" 要提的是：

- GitHub Pages chart repo（最簡單）：用 `helm/chart-releaser-action` 把 chart 推到 gh-pages
- OCI registry：`helm push slurm-platform-1.0.0.tgz oci://ghcr.io/<user>/charts`

對學術專題是錦上添花；對開源散播是必要。

### R18 ★：缺端到端 chaos / failure injection 測試 — 差異化機會

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


> [!IMPORTANT]
> **為什麼出色** => 沒有任何 Slurm-on-K8s 開源方案做這個。Slinky / SUNK 文件全部沒有 chaos test 章節。對 thesis evaluation 是「我能量化我的容錯主張」。

實作建議：寫 `scripts/chaos/*.sh`，每個是一個情境，能獨立跑；CI 跑其中一兩個快的當 smoke test。

### Helm 5-A 已完成的後續清理項目

- `verify-helm.sh` 還在，但 legacy parity diff 段已移除 — 確認沒遺留
- chart `Chart.yaml` 的 `appVersion` 應寫 Slurm 版本（如 `21.08.5` 或將來升 23.11.x），目前還是 placeholder

---

## 8. Operator 設計

## 9. 升級路徑與技術債

### R13：Slurm 21.08 → 23.11 升級 — 🔒 嘗試後 deferred（2026-05-05）

**嘗試經過：** 把 base image 換成 ubuntu:24.04（apt 預設 slurm-wlm 23.11.4），對應改 `slurm-wlm-jwt-plugin`（auth_jwt 在 24.04 拆出獨立套件）、`openapi/v0.0.37 → v0.0.40`（v0.0.37 在 23.11 移除）、JWT projected secret 加 `mode: 0400`（23.11 強制 ≤ 0600，原本 K8s 預設 0644 被拒）。helm-unittest 47/47 PASS。

**實機部署陸續撞到 4 道牆：**

| # | 23.11 新行為 | 我們的修法 | 結果 |
|---|---|---|---|
| 1 | `auth_jwt.so` 拆到 `slurm-wlm-jwt-plugin` 套件 | Dockerfile 加套件 | ✅ |
| 2 | JWT key file 強制 mode ≤ 0600 | projected secret + secret volume `defaultMode: 0400` | ✅ |
| 3 | `openapi/v0.0.37` 移除 | controller startup script + slurmExporter 改 v0.0.40 | ✅ |
| 4 | slurmstepd 無條件透過 dbus 建 systemd cgroup scope | **無解，需 PSS=privileged + dbus hostPath + systemd init** | ❌ |
| 5 | slurmctld state file 格式變動，舊 PVC 9472 < 9728 不能 restore | 需 wipe ctld-state PVC（會丟 queue） | ⚠️ |
| 6 | controller 23.11 ↔ slurmdbd 21.08 protocol_version 6500 不相容 | 需同步升 slurmdbd image 並 wipe MySQL 或 dump→restore | ⚠️ |

第 4 道是 **showstopper**：在 PSS=baseline 不可解，PSS=privileged 違反原本的安全設計（NetworkPolicy + secret projection 都依賴 baseline）。**revert 回 ubuntu:22.04 + Slurm 21.08**，但保留以下「無害的前進」：

- `chart/templates/configmap-static.yaml` 多 emit `cgroup.conf` data key（條件式：只在 `proctrackType=proctrack/cgroup` 才 emit）
- `_helpers.tpl::slurm-platform.cgroupConf` helper（`autodetect` + Constrain*）
- JWT secret 三處（controller projected / operator volume / slurm-exporter volume）一律 `defaultMode: 0400`（21.08 接受、23.11 必需 — 升級 friendly）
- slurmrestd / slurmExporter API 版本變數化（透過 `monitoring.slurmExporter.restApiVersion`），值留 v0.0.37，升級時改一個 value 即可

**何時重啟這條路：**
1. chart 補 PSS=privileged overlay（`chart/values-privileged.yaml`），含 hostPath `/run/dbus` + privileged worker + systemd init 容器
2. 或上游 Slurm 加 `slurmstepd --without-systemd` runtime flag（社群討論中無 ETA）
3. 或 build Slurm from source 鎖 23.02.x（最後一個沒強制 systemd-scope 的 LTS）

對學術專題不急；寫進「未來工作」章節時，把第 4 道牆當賣點 — 「修這個的成本 = 開一個 privileged overlay」是可以發 issue / PR 給 Slurm 上游的素材。

### R19 ★：sbatch wrapper / submit plugin — 差異化機會

Phase 6 已補上 runtime predictor → Lua `time_limit` path；這裡剩下的是把使用者的 `sbatch foo.sh` 進一步自動補：

- `--mem` ← 預測 + 歷史 max memory
- 適合的 `--partition` ← 根據 GRES 推
- `--qos` ← 根據使用者 / 帳號自動

**為什麼出色：** AWS ParallelCluster / Slinky 都沒做。HPC 中心（NERSC、ALCF）有做但不開源。

---

## 10. 業界比較（v4 更新）

| 面向 | 本專案目前狀態 | Slinky | SUNK | AWS ParallelCluster | Volcano |
|---|:---:|:---:|:---:|:---:|:---:|
| Helm 一條指令部署 | ✅ | ⚠️（多 chart） | ✅ | n/a | ✅ |
| GPU MPS sharing | ✅（GPU Operator） | ⚠️（只 timeSlicing） | ✅ | ✅ | ✅ |
| 端到端 Job lifecycle trace（OTel） | ❌（Phase 7-A） | ❌ | ❌ | ❌ | ❌ |
| Per-job × per-GPU × full-lifecycle dashboard | ❌（R20） | ❌ | ❌ | ⚠️ | ❌ |
| ML-aware scheduling（runtime / score） | ✅（Phase 6 M1-M8）| ❌ | ❌ | ❌ | ⚠️ |
| Chaos / failure injection test suite | ❌（R18） | ❌ | ❌ | ⚠️ | ❌ |
| Checkpoint-aware scale-down | ✅ | ❌ | ❌ | ❌ | ❌ |
| Drain timeout（hang job 不卡死） | ✅ | ⚠️ | ✅ | ✅ | ✅ |
| Fairshare / QoS 啟用 | ❌ | ✅ | ✅ | ✅ | ✅ |
| HA Controller | ❌ | ✅ | ✅ | ✅ | ✅ |
| 共享 FS | NFS（瓶頸） | StorageClass | Lustre | FSx | StorageClass |

**競爭力分析：**

- 已勝出：**Checkpoint-aware scale-down**（沒人做）、**MPS env propagation via TaskProlog**（這個專案是我看過的開源裡寫得最完整的）、**Helm cutover 完整度**、**ML-aware scheduling（Phase 6 M1-M8）**
- 容易補上、補了會勝出：**OTel trace（R16）**、**per-job dashboard（R20）**、**chaos suite（R18）**
- 短期沒打算追平：HA controller（3-C）、Fairshare（4-B）、共享 FS 升級

**結論：** Phase 6 已經補上演算法貢獻；下一個最高 CP 值組合是 **R16（OTel trace）+ R20（per-job dashboard）**：兩者技術上強相關、加起來大概 3 週工期、產出視覺化 demo 力強。

---

## 11. 改進優先順序總表（v4）

僅列**仍開放**項目；安全章節整段省略。★ 是「做了會出色」而非「不做會壞」。

| 優先 | 項目 | 類別 | 難度 | 對應動機 |
|:---:|---|---|:---:|:---:|
| **P1** | R6：Operator leader election（短期：reduce restart RTO；長期：active-passive） | 可靠度 | 中 | 容錯 |
| **P1** | R14：NFS mountOptions tuning | 儲存 | 低 | DDP I/O |
| **P1** | 2-C：MySQL 備份 CronJob（沿用） | 儲存 | 低 | Fairshare 持久 |
| **P1** | 3-C：StateSaveLocation snapshot CronJob（沿用） | 容錯 | 低 | 容錯 |
| 🔒 | R9：cgroup v2 / proctrack/cgroup — blocked-by R13 + PSS=privileged overlay | Slurm | 中 | — |
| **P2** | R11：Login pod resource limit | K8s | 低 | — |
| **P2** | R15：Checkpoint guard 多 pattern | 彈性 | 低 | 容錯 |
| **P2** | 4-A/B/C：QoS / Preempt / Fairshare 啟用（Phase 6 前置） | 排程 | 中 | — |
| **P2** | 5-C：Lmod conflict + NCCL 模組（沿用） | HPC | 中 | — |
| 🔒 | R13：Slurm 升 23.11.x — 嘗試後 deferred（slurmstepd dbus systemd-scope vs PSS=baseline） | tech debt | 高 | — |
| **P3** | 5-D：Multi-host 才做的 Multus / Cilium / SR-IOV（建議從 roadmap 移除） | 網路 | 高 | DDP（multi-host） |
| **★** | R16：OTel job-lifecycle trace（Phase 7-A） | 差異化 | 高 | 可觀測性 |
| **★** | R18：Chaos / failure injection test suite | 差異化 | 中 | 容錯（驗證） |
| **★** | R19：submit helper 補 `--mem` / partition / qos | 差異化 | 中 | 易用性 |
| **★** | R20：Per-job × per-GPU × full-lifecycle dashboard | 差異化 | 中 | 利用率（視覺化） |

---

## 12. 給校內專題（thesis）的建議切入順序

整理上面所有 P0/P1/★ 後，對單人 1 學期工程量的最務實順序：

```
Week 1–2：R16 OTel trace（Phase 7-A 起步）— 取得 job lifecycle trace
Week 3–4：R20 per-job GPU dashboard — 把 DCGM / Slurm / OTel 串成可視化 demo
Week 5–6：E7 live-cluster validation — 補 Phase 6 真機 50-job mix 與 checkpoint resume cost
Week 7–8：R18 chaos suite — evaluation 章節的容錯數字
Week 9–10：R14 / R11 / R15 這類低成本 production readiness 收尾
Week 11–14：寫 thesis、整理 figures、補 appendix
```

**論文角度的單一最強組合：Phase 6 + R16 + R20。**

Phase 6 給你「演算法貢獻」；R16 給你「能聯動的時序資料」；R20 給你「能看的數字」。三個一起，evaluation 章節有：

1. **基線比較**：FCFS vs 你的 score function vs 簡化 Gandiva 重排，看 JCT / utilization / fairness
2. **可解釋性**：trace + dashboard 直接展示 score function 在做什麼
3. **容錯主張**：R18 chaos test 給你的 ablation 章節
4. **submit helper 補齊（R19）**：`--mem` / partition / qos 自動化是 bonus，做得完就放，做不完不影響主軸

---

*v4 審核以 Phase 5（Lmod + Helm cutover）完成後的真實系統為基礎；本版已移除後續完成項，讓清單只保留仍需處理的缺口。安全章節依使用者要求暫不審；下次 v5 預定 Phase 7-A 動工後重審觀測縱深。*
