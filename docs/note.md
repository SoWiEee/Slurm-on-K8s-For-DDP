# Development Notes

1. [採坑紀錄](#debug-record)
2. 開發規劃：[監控系統](#phase-4-plan已完成)、[階段五計畫](#phase-5-plan)
3. [工作和硬體資源的分配關係](#工作和硬體資源的分配關係)
4. [GPU MPS 完整實作指南](#gpu-mps-完整實作指南)
5. [參考資料](#-參考來源)

## Debug Record

### 問題 1：Secret volume 唯讀，不能直接 chmod

觀察到：
```
chmod: changing permissions of '/etc/munge/munge.key': Read-only file system
```
原因在於 K8s Secret mount 是唯讀。

修正方式：

- 改掛到 `/slurm-secrets/munge.key`。
- 啟動時複製到 `/etc/munge/munge.key` 後再 `chown/chmod`。

### 問題 2：`SlurmctldHost` / DNS 解析錯誤

觀察到：
```
This host ... not a valid controller
NO NETWORK ADDRESS FOUND
```

修正方式：

- 在 `slurm.conf` 明確設定 `NodeAddr` / `NodeHostname`。
- controller 改用 `slurm-controller-0(slurm-controller-0....svc.cluster.local)`。

### 問題 3：`duplicate partition in config: debug`

觀察到 operator 啟動時直接 `ValueError: duplicate partition in config: debug`

原因是：原本 validation 把「partition 名稱重複」當成非法，但現在多 pool 共享同一個 Slurm partition 是設計需求，不是錯誤。

修正方法：

- validation 不能用 partition name 當唯一鍵。
- 要接受「同一 partition 對應多個 worker pool」。

### 問題 4：slurmdbd 啟動後 hostname 不符

**現象：** slurmdbd 啟動後立即 fatal exit：`This host not configured to run SlurmDBD (slurmdbd-xxx != slurmdbd)`。

**原因：** `slurmdbd.conf` 的 `DbdHost=slurmdbd`，但 Deployment pod 的 hostname 是 `slurmdbd-{replicaset}-{random}`（Kubernetes 預設行為）。slurmdbd 在啟動時會驗證 `DbdHost` 是否匹配當前 hostname。

**修法：** `slurm-accounting.yaml` 的 Deployment pod spec 加入 `hostname: slurmdbd`，讓 pod hostname 固定為 `slurmdbd`。

### 問題 5：slurmctld 首次啟動 fatal（TRES 缺失）

**現象：** 新叢集第一次啟動時，slurmctld fatal exit：`You are running with a database but for some reason we have no TRES from it`。

**原因：** `slurm.conf` 設定了 `AccountingStorageType=accounting_storage/slurmdbd`，slurmctld 啟動時需要從 slurmdbd 取得 TRES（Trackable RESources）定義。若 slurmdbd 尚未 ready（容器剛建立），且又沒有本地 state file，slurmctld 就會 fatal exit 而非等待。

**修法：** `scripts/render-core.py` 的 controller 啟動腳本加入 wait loop：偵測到 `AccountingStorageType=slurmdbd` 時，先用 bash TCP 連線確認 `slurmdbd.slurm.svc.cluster.local:6819` 可達，再 exec slurmctld。

```bash
if grep -q 'AccountingStorageType=accounting_storage/slurmdbd' /etc/slurm/slurm.conf; then
  until (echo >/dev/tcp/slurmdbd.slurm.svc.cluster.local/6819) 2>/dev/null; do sleep 3; done
fi
exec slurmctld -Dvvv
```

### 問題 6：PDB 與 StatefulSet 縮容的關係

常見誤解：*認為 PDB 的 `maxUnavailable: 1` 會阻止 operator 把 replicas 從 4 降到 0。

實際行為：
- StatefulSet `replicas` 調整是 **Desired State**，K8s controller 會逐步刪除 Pod（最高優先）
- PDB 保護的是 **Voluntary Disruption**（如 `kubectl drain node`、節點升級）
- operator 調整 replicas = K8s 內部操作，**不受 PDB 約束**
- 結論：PDB 與 drain-then-scale 並不衝突；PDB 保護的是基礎設施層面，drain 保護的是 job 層面


### 問題 7：`MpiDefault=pmi2` 與 `mpi_pmi2.so` plugin 位置

現象：改為 `MpiDefault=pmi2` 後，`srun --mpi=pmi2` job 可能出現 `srun: error: PMI2 not found`。

原因：Ubuntu 22.04 的 `slurmd` 套件把 MPI plugin 放在 `/usr/lib/x86_64-linux-gnu/slurm-wlm/`，路徑需在 `PluginDir` 中。

排查步驟：
```bash
# 在 worker pod 確認 pmi2 plugin 存在
kubectl -n slurm exec pod/slurm-worker-cpu-0 -- \
  find /usr/lib -name 'mpi_pmi2.so' 2>/dev/null

# 確認 PluginDir 設定（通常不用手動設）
kubectl -n slurm exec pod/slurm-controller-0 -- \
  scontrol show config | grep PluginDir
```

實際發現：Ubuntu 22.04 `slurmd`（Slurm 21.08）內建 PMI2，不需要額外安裝；plugin 會自動在 PluginDir 找到。

### 問題 8：`srun --mpi=pmi2` 在單節點多 task 的行為

確認：`--ntasks=2 --nodes=1` 加上 `srun --mpi=pmi2` 可以在同一個 worker pod 啟動兩個 MPI rank，`$SLURM_PROCID` 分別為 0 和 1。這對容器化 HPC 測試是最低門檻的 MPI 驗證方式，不需要 pod 間網路或 InfiniBand。


### 問題 9：`/etc/profile.d/slurm-modulepath.sh` 在 sbatch 裡不生效

現象：login pod 互動式 shell `module avail` 正常，但 sbatch job 內 `module load` 後 `MPI_HOME` 仍是 NOT_SET。

原因：
- `/etc/profile.d/*.sh` 只在 **login shell** 啟動時自動 source（`bash -l`）
- Slurm 以非互動、非 login 的 `/bin/bash` 執行 sbatch 腳本
- 因此 `/etc/profile.d/slurm-modulepath.sh` 完全沒被讀到，`MODULEPATH` 未設定
- Lmod 找不到 `/opt/modulefiles`，`module load openmpi/4.1` 靜默失敗

修法：改用 Lmod 官方機制，在 Dockerfile 寫入 `/etc/lmod/modulespath`：
```dockerfile
RUN mkdir -p /etc/lmod && echo '/opt/modulefiles' > /etc/lmod/modulespath
```
Lmod 在每次 `source /etc/profile.d/lmod.sh` 時都會讀這個檔案，不論 shell 類型。

### 問題 10：job output 在 worker pod，不在 login pod

現象：`cat /tmp/phase5-verify-$jid.out` 在 login pod 找不到檔案。

原因：Slurm 的 `--output` 路徑是在**執行 job 的 worker node** 上建立的。
沒有共享 filesystem（NFS/Lustre），output 不會自動傳回 login node。

在真實 HPC：所有節點共享 NFS，`/home/user/` 或 `/scratch/` 上的 output 到處都能讀。

最終解法（Lmod + NFS 整合）：`bootstrap-lmod.sh` 掛載 NFS 到所有 Pod，並確保 `/shared/jobs/` 目錄存在。job script 輸出路徑改為：
```bash
#SBATCH --output=/shared/jobs/phase5-verify-%j.out
#SBATCH --error=/shared/jobs/phase5-verify-%j.err
```
所有節點共享同一 NFS，login pod 可直接 `cat /shared/jobs/<outfile>` 取得輸出。


## 問題 11：NFS Server 的 export 沒涵蓋 LAN 介面 IP

**現象**

```
Warning  FailedMount  ...  MountVolume.SetUp failed for volume "nfs-client-root":
  mount failed: exit status 32
  mount.nfs: access denied by server while mounting 192.168.0.111:/srv/nfs/k8s
```

`nfs-subdir-external-provisioner` Pod 卡在 `ContainerCreating` 永遠無法啟動。

**根本原因**

`setup-nfs-server.sh` 只設定了：

```
/srv/nfs/k8s 10.0.0.0/8(rw,sync,no_subtree_check,no_root_squash,insecure)
```

k3s 以 native（非 VM）方式跑在主機上，Pod volume 的 NFS mount 是由 **kubelet（節點）** 執行，不是由 Pod 內部發起。節點的 LAN IP 是 `192.168.0.111`（`enp5s0`，在 `192.168.0.0/25`），不在 `10.0.0.0/8` 內。

**修法**

```bash
# /etc/exports 加上 LAN subnet：
/srv/nfs/k8s 10.0.0.0/8(rw,sync,...) 192.168.0.0/25(rw,sync,no_subtree_check,no_root_squash,insecure)
sudo exportfs -ra
```

## 問題 12：GPU job 卡在 COMPLETING 永不結束

**現象**

```
[21:34:15] job 16 state=COMPLETING
[21:35:33] job 16 state=COMPLETING
WARN: timed out waiting for GPU job 16
FAIL: GPU job did not complete (state=COMPLETING)
```

job 的 ExitCode 已經是 `0:0`（batch script 確實跑完），但 slurmctld 一直等 epilog-done 訊號。

**根本原因**

COMPLETING 在 Slurm 裡代表「batch script 結束，等 slurmstepd 回報 epilog 完成」。GPU worker 是 elastic pool（用完就縮容），operator 把 Pod evict 掉後，preStop hook 設了 `state=drain`，接著 kubelet 殺掉 Pod。slurmstepd 跟著消失，slurmctld 收不到 epilog-done，job 就永遠卡在 COMPLETING。

**修法**

在 `scripts/render-core.py` 的 `slurm.conf` 加一行：

```python
"CompleteWait=0",
```

`CompleteWait=0` 讓 slurmctld 在 batch script 結束後不等 epilog-done，直接把 job 轉到 COMPLETED。Worker Pod 沒有任何 epilog script，這個設定完全安全。

**復現方式驗證**

加入後，job 在 RUNNING → COMPLETED 只需幾秒，不再卡住。

---

## 問題 12：verify-gpu.sh 的 job output 讀不到

**現象**

```
(no output file on submit pod — no shared storage)
ExitCode=0:0  TresPerNode=gres:gpu:rtx4070:1
PASS: GPU job ExitCode=0:0 and GPU GRES allocated ...
```

job 有跑完，但看不到 `nvidia-smi` 的實際輸出。

**根本原因**

原本的 sbatch script 是：

```bash
#SBATCH --output=/tmp/gpu-verify-%j.out
```

output 寫在 GPU worker Pod 的 `/tmp`。GPU worker Pod 跑完 job 後被 operator 縮容，Pod 消失，login pod 去讀這個 `/tmp` 路徑當然讀不到。

**修法**

改把 output 寫到 NFS 共享的 `/shared`：

```bash
JOB_OUT_DIR="${SHARED_DIR}/gpu-verify-$$"   # $$=verify腳本的PID，唯一目錄
# 在 sbatch submission 前先在 login pod 建立該目錄
mkdir -p '${JOB_OUT_DIR}'
# sbatch 腳本內：
#SBATCH --output=${JOB_OUT_DIR}/%j.out
```

所有 Pod（controller、worker、login）都 mount 同一個 `slurm-shared-rwx` PVC，job output 寫進去後 login pod 一定讀得到。

---

## 問題 13：verify.sh GPU job 丟到 cpu partition 導致立刻 fail

觀察到：

```
sbatch: error: Batch job submission failed: Requested node configuration is not available
```

**根本原因**

`verify.sh` 有：

```bash
PARTITION=${PARTITION:-cpu}
```

GPU job 的 sbatch script 用了 `#SBATCH -p ${PARTITION}`，結果是 `-p cpu`，cpu partition 根本沒有 `--gres=gpu:rtx4070:1` 的節點，Slurm 立刻拒絕。

**修法**

加一個獨立的 `GPU_PARTITION` 變數，GPU job 改用它：

```bash
GPU_PARTITION=${GPU_PARTITION:-gpu-rtx4070}
# sbatch 腳本改為：
#SBATCH -p ${GPU_PARTITION}
```

## 問題 14：DRAIN 狀態導致 sbatch 立刻被拒

觀察到：

```
sbatch: error: Batch job submission failed: Requested node configuration is not available
```

**根本原因**

preStop hook（`scontrol update nodename=$(hostname) state=drain reason=k8s-eviction`）在每次 Pod 縮容時都會被觸發，DRAIN 狀態會在 slurmctld 的 StateSaveLocation 裡持久化。

下一次 sbatch 提交時：
- GPU worker Pod 已縮容，replicas=0
- slurmctld 裡所有 GPU node 都是 `drained*`
- Slurm 直接拒絕投遞（DRAINED ≠ DOWN，不會排到 queue 等節點恢復）

Worker Pod 啟動時雖然有 `scontrol update nodename=$(hostname) state=resume`（坑 6 的修法），但 Pod 還沒啟動就已經 sbatch 失敗了。

**修法**

在 verify.sh GPU job 提交前，先 resume 所有 drained 的節點：

```bash
login_exec "sinfo -t drain,drained -N --noheader -o '%N' 2>/dev/null \
  | xargs -r -I{} scontrol update nodename={} state=resume 2>/dev/null" || true
```

這樣 GPU 節點變成 `idle*`（slurmd 未連線但不是 DRAIN），sbatch 成功送出 PENDING，operator 看到 PENDING job 後 scale up，Pod 啟動後 slurmd 重新 register，job 才能 dispatch。

**為何不用 `awk` 解析 `sinfo --Format=NodeList,StateLong`？**

`sinfo --Format` 的欄位是固定寬度，節點名稱過長時會截斷，截斷後與 state 欄位黏在一起，awk `$2` 抓不到正確 state。改用 `-o '%N'` 配合 `-t drain,drained` 過濾才能拿到完整節點名稱。

## 問題 15：MPS job 拿不到 `CUDA_MPS_PIPE_DIRECTORY`，CUDA 跳過 MPS 直連 GPU

`scripts/verify-gpu.sh` 的 step 6 過去長期出現：

```
PASS: CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25 injected by Slurm prolog
WARN: CUDA_MPS_PIPE_DIRECTORY missing or unset — device-plugin sharing.mps may not be active
```

實機觀察：4 顆 allocatable `nvidia.com/gpu`、`nvidia-cuda-mps-control` daemon 在 `gpu-operator` namespace 跑得好好的、worker pod 容器 env 也有 `CUDA_MPS_PIPE_DIRECTORY=/mps/nvidia.com/gpu/pipe`，但 sbatch 進來的 job 看不到這個變數。代表 MPS 基礎設施 OK，但 Slurm job 沒走過 daemon — CUDA runtime 缺了 socket 路徑就 fallback 直連 GPU，Slurm 宣告的 `mps:25` 只變成排程上的「不超賣」聲明，沒有實際 SM% 限流。

**根本原因（兩層）**

1. **slurmd 的容器 env 不會自動傳給 job step。** 從 login pod 提交的 sbatch，job step 的環境是「使用者 sbatch 當下的 env」+ Slurm 注入的 SLURM_*，**不是** worker 容器的 env。雖然 worker 容器啟動時 NVIDIA container-toolkit hook 已經注入 `CUDA_MPS_PIPE_DIRECTORY`，但只有 slurmd PID 1 看得到，slurmstepd fork 出 user task 前已經把 env 清乾淨。
2. **`TaskPlugin=task/none` 會讓 `TaskProlog` 完全不執行。** `task/none` 是個 no-op 插件，連 prolog hook 都不掛。我們起初設成 `task/none` 是為了避開 Slurm 21.08 的 cgroup v2 不完整支援，但代價是 TaskProlog 直接被略過。

**修法（chart/templates/configmap-task-prolog.yaml + values.yaml）**

1. `slurm.taskPlugin` 從 `task/none` 改成 `task/affinity`（不需要 cgroup，只用 `sched_setaffinity`，Slurm 21.08 OK）。
2. 新增 `slurm-task-prolog` ConfigMap，掛到所有 worker pod 的 `/etc/slurm/prolog.d/10-mps-env.sh`。
3. 在 `slurm.conf` header 加上 `TaskProlog=/etc/slurm/prolog.d/10-mps-env.sh`，由 `slurm.taskProlog` value 控制。
4. **prolog 必須讀 `/proc/1/environ`**，不能直接 `echo "export X=$X"`。slurmstepd exec TaskProlog 之前會把繼承的 env 砍到只剩 `CUDA_VISIBLE_DEVICES` + `SLURM_*`，所以要從 slurmd（PID 1）的 environ 讀容器層級的 `CUDA_MPS_PIPE_DIRECTORY`：

```sh
read_pid1_env() {
  tr '\0' '\n' < /proc/1/environ 2>/dev/null \
    | awk -F= -v k="$1" '$1==k {sub("^[^=]+=",""); print; exit}'
}
for var in CUDA_MPS_PIPE_DIRECTORY CUDA_MPS_LOG_DIRECTORY NVIDIA_VISIBLE_DEVICES; do
  val=$(read_pid1_env "$var")
  [ -n "$val" ] && echo "export ${var}=${val}"
done
```

Slurm 會解析 prolog stdout 的 `export X=Y` 行，把這些變數注入 job step env。

**驗證**

修法後 verify-gpu.sh step 6：

```
MPS job stdout:
  CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25
  CUDA_MPS_PIPE_DIRECTORY=/mps/nvidia.com/gpu/pipe
  CUDA_VISIBLE_DEVICES=0
  SLURM_JOB_GPUS=0
  NVIDIA GeForce RTX 4070
PASS: CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25 injected by Slurm prolog
PASS: CUDA_MPS_PIPE_DIRECTORY set (MPS daemon socket reachable)
```

batch step 與 srun step 都拿到 socket 路徑，CUDA runtime 真的會走 MPS daemon，多 job 並行才會被 SM% throttle。

**踩坑插曲（驗證階段）**

1. 一開始用 sbatch 直接接 `--gres=gpu:rtx4070:1,mps:25` 會被拒（`Invalid gres specification`）— 拆成兩段、或單獨 `--gres=mps:25` 都會失敗。改成 `--gres=gpu:rtx4070:1` 單獨 sbatch，再讓 verify-gpu.sh 自己組合 GRES 才正常（這是 Slurm 21.08 + cons_tres 對混合 GRES 的解析限制）。
2. 替換 ConfigMap 後 kubelet 大約要 60 秒才把 symlink 切到新內容，期間 prolog 還是舊版。要 `until grep -q <new-marker> /etc/slurm/prolog.d/10-mps-env.sh; do sleep 5; done` 才能確認生效。
3. operator 預設 cooldown 60s + preStop 會 DRAIN，導致驗證 job 一直 NODE_FAIL。debug 期間先 `kubectl scale deploy slurm-elastic-operator --replicas=0` 把 operator 停掉，避免它在驗證中途縮容。驗證完記得 scale 回 1。

---

# Phase 4 Plan（已完成）

## Prometheus + Grafana 監控

詳細規格見 `docs/monitoring.md`。核心是三層 metrics：

| 來源 | 取得方式 | 關鍵指標 |
|------|---------|---------|
| slurm-exporter | scrape slurmrestd（REST API） | queue_pending, nodes_idle, nodes_alloc |
| kube-state-metrics | K8s 原生 | StatefulSet replicas, Pod ready |
| operator 自定義 | prometheus_client HTTP server（port 8000） | scale_up/down_total, guard_blocks, poll_duration |

已經部署 Prometheus + Grafana + slurm-exporter + kube-state-metrics。Grafana 提供三個看板：
- **Bridge Overview**：視覺化 Slurm queue depth 與 K8s StatefulSet replicas 的聯動關係
- **Slurm Cluster State**：node states 圓餅圖、各 partition queue depth 時序
- **K8s Operator**：scale event timeline、poll duration histogram、guard block 計數

部署指令：`bash scripts/bootstrap-monitoring.sh`  
驗證指令：`bash scripts/verify-monitoring.sh`

---

# Phase 5 Plan

Phase 5 的目標是讓這套系統從「可運作的基礎設施原型」演進成「使用者能直接提交各種 AI 批次工作的運算平台」。

目前以**單一使用者**情境為主，系統核心優先做到：**部署可重複 → job 生命週期可視化 → 工作負載開箱即用 → 真實 SSH 登入**。多租戶（Fair-Share 帳號配額）留待後期疊加，不影響前四項。

開發順序：**Helm（5-A）→ OpenTelemetry（5-B）→ 工作負載模板（5-C）→ SSH Login（5-D）**

---

## 5-A：Helm Chart 封裝

> **修訂版 5（2026-04-29，Stage F 完成 — Phase 5-A 收尾）：** 真實環境驗證通過後，正式 cutover 到 helm。本表「廢棄的檔案」段所列的 12 個檔案全部移除（`scripts/{render-core.py,bootstrap.sh,bootstrap-gpu.sh,bootstrap-monitoring.sh}`、`manifests/core/{slurm-static.yaml,worker-pools.json,slurm-ddp-runtime.yaml}`、`manifests/gpu/{nvidia-device-plugin.yaml,mps-daemonset.yaml}`）；`scripts/{bootstrap-storage.sh,bootstrap-lmod.sh}` 與 `manifests/core/lmod-modulefiles.yaml` 不在原始廢棄表內因此保留。`verify-helm.sh` 移除 legacy parity diff 區段（`manifests/core/slurm-static.yaml` 已不存在）。`chart/tests/` 加入 6 個 `*_test.yaml` 共 28 條 helm-unittest 案例，覆蓋 slurm.conf / workers / operator / gpu / monitoring / storage。README 的「🚀 Getting Started」整段重寫為 helm install 流程；`manifests/core/slurm-accounting.yaml`（mysql + slurmdbd）標註為 chart 之外的 prerequisite。
>
> **修訂版 4（2026-04-29，Stage E 完成）：** monitoring + storage 進 chart，`enabled` flag 控制。Monitoring stack（Prometheus / Alertmanager / Grafana / kube-state-metrics + slurm-exporter）拆到 `templates/monitoring/`；Grafana dashboards 從 `chart/dashboards/*.json` 用 `Files.Glob.AsConfig` 灌進 ConfigMap。Storage stack（NFS subdir external provisioner + StorageClass + RWX PVC）放在 `templates/storage.yaml`，`storage.enabled=true` 但缺 `nfsServer` 直接 `fail` 終止 render；`storageClassName` 與 `provisionerName` 跟 legacy `manifests/storage/*.yaml` 完全一致（provisioner 名 `k8s-sigs.io/slurm-nfs-subdir-external-provisioner` 是 immutable，必須對齊現有 cluster）。Cross-namespace 流量由 `network-policy.yaml` 在 `monitoring.enabled=true` 時額外加三條：`allow-prometheus-scrape-operator`、`allow-prometheus-scrape-exporter`、`allow-slurm-exporter-egress`。verify-helm.sh 加 14 條 Stage E spot-checks；k3s 1.34 server-side dry-run validate 75 個 resources（default 38 個）全綠。
>
> **修訂版 3（2026-04-28，Stage D 中）：** 嘗試把 `gpu-operator` 加成 chart dependency 後發現它把所有 DaemonSet hardcode 在 `Release.Namespace`（沒有 namespaceOverride 機制），且需要該 namespace PSS=`privileged` 才能 mount hostPath（`/dev/nvidia*`、`/run/nvidia/mps`、driver libs）。我們的 slurm namespace 走 PSS=`baseline`（NetworkPolicy + secret projection 都依賴此），兩者放同一個 namespace 不乾淨——dropping 到 privileged 會放鬆 slurm pod 的整體安全姿態。
>
> **改採分離安裝**：`gpu-operator` 不再是 subchart，由 `scripts/install-gpu-operator.sh` 獨立 `helm install` 到自己的 `gpu-operator` namespace（PSS=privileged）。本 chart 只負責放 `device-plugin-config` ConfigMap 進該 namespace + cluster-wide 的 node-labeler Job。`Chart.yaml` 移除 dependencies block；`charts/`、`Chart.lock`、`*.tgz` 都不進 git。部署流程從「一條 helm install」變成「一條 setup-linux-gpu.sh + 一條 install-gpu-operator.sh + 一條 helm install slurm-platform」。
>
> **修訂版 2（2026-04-28）：** Linux+k3s+RTX4070 路徑驗證後（commit `3eec54f`），確認 `nvidia-device-plugin` 內建 `sharing.mps` 在 v0.15–v0.17.x 全系列因 upstream `cmd.Exec("nvidia-cuda-mps-control", "-d")` daemonize spawn race 而無法啟動（見 [`docs/migration.md`](migration.md)）。本版改為以 GPU Operator 為 GPU 子系統的目標方案——它把 MPS daemon 拆成獨立 `mps-control-daemon` DaemonSet 用前景模式跑，繞過 spawn race。〔修訂版 3 把它從 dependency 改成獨立安裝。〕
>
> **修訂版 1（2026-04-27）：** 本節原稿寫於 N1 / N7 修復前，`mps.enabled` flag、`partition: debug`、rtx4080 `devicePath: /dev/nvidia1` 已隨 `mps-migration` 分支上線而過時。先對齊：sharing.mps（N1 / N10）、三 partition 拆分（N7）、`/dev/nvidia0` 一律（N2）、`AccountingStorageTRES`（N6）、namespace PSS=baseline（N9）、k3s `ctr images import`（N4）、NetworkPolicy 6443（N5）。

### 設計方向

**Monolithic chart for slurm 子系統 + NVIDIA GPU Operator 獨立安裝 + slurm.conf 拆兩個 ConfigMap。**

- 主體不拆 subchart；monitoring / storage / gpu 用 `enabled` flag 控制。
- `render-core.py` 廢棄，`slurm.conf` / `gres.conf` 改由 `_helpers.tpl` 從 `values.yaml` 的 `pools` 列表產生。
- **GPU 子系統用 NVIDIA GPU Operator，但獨立安裝（不是 chart dependency）**——見上面修訂版 3 banner。本 chart 只在 `gpu-operator` namespace 放 device-plugin-config ConfigMap + 一個 cluster-wide 的 node-labeler Job 把 GPU 節點標上 `nvidia.com/device-plugin.config=<key>`。GPU Operator 由 `scripts/install-gpu-operator.sh` 用 `helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace --set driver.enabled=false --set toolkit.enabled=false` 裝進自己的 PSS=privileged namespace。Operator 內建的 `mps-control-daemon` DaemonSet 解決 v0.15–v0.17.x device-plugin 內建 MPS 的 spawn race。
- **GPU Operator 的 driver / toolkit 子模組關掉**：host 已用 `apt install nvidia-driver-535` + `nvidia-container-toolkit` 裝好，重複裝會撞。Operator 只負責 device-plugin、MPS daemon、DCGM exporter（可選）、gpu-feature-discovery、node-feature-discovery。
- 自寫的 `manifests/gpu/nvidia-device-plugin.yaml` + `manifests/gpu/mps-daemonset.yaml` 廢棄。
- `slurm.conf` ConfigMap 拆成 **`slurm-config-static`**（ClusterName / Auth / Plugin / AccountingStorageTRES，幾乎不變）+ **`slurm-config-nodes`**（NodeName / PartitionName，每次 pool 變動都重產）。worker 只 mount 後者 → 改一個 pool 的 `maxReplicas` 不會 rolling restart 全部 worker。
- secret（munge.key / slurm-jwt-key）**不由 chart 產生**，install 前要先跑 `scripts/create-secrets.sh`（chart 用 `helm.sh/hook-pre-install` 檢查存在性即可）。

### Chart 目錄結構

```
chart/
  Chart.yaml                ← appVersion = Slurm 版本（如 23.11.7）；無 dependencies
  values.yaml               ← 預設值（Kind 開發環境基準）
  values-dev.yaml           ← Kind override（無 GPU，File=/dev/null）
  values-k3s.yaml           ← k3s override（real GPU、namespace baseline label）
  templates/
    _helpers.tpl            ← label 函數 + slurmConf / gresConf / partitionsJson 產生函數
    namespace.yaml          ← slurm Namespace，含 pod-security.kubernetes.io/enforce=baseline label
    configmap-static.yaml   ← slurm-config-static（ClusterName / Auth / Plugin / AccountingStorageTRES）
    configmap-nodes.yaml    ← slurm-config-nodes（NodeName / PartitionName / gres.conf）
    controller.yaml         ← controller StatefulSet + PDB
    workers.yaml            ← range pools → 每 pool 的 StatefulSet + PDB
    operator.yaml           ← operator SA + Role + Binding + Deployment + PDB + Service
    services.yaml           ← controller / restapi / 每 pool 的 worker service
    pvc.yaml                ← ctld-state PVC
    login.yaml              ← login Deployment + Service + PDB + slurm-ddp-runtime ConfigMap
    network-policy.yaml     ← 11 條 NetworkPolicy（operator → K8s API egress 443+6443 等）
    gpu/
      gpu-operator-namespace.yaml  ← {{- if .Values.gpu.enabled }} 建 gpu-operator namespace（PSS=privileged）
      device-plugin-config.yaml    ← {{- if .Values.gpu.enabled }} ConfigMap 進 gpu-operator namespace
                                   ← GPU Operator 的 device-plugin 透過 nvidia.com/device-plugin.config
                                   ←   label 從這個 ConfigMap 讀取 sharing 設定
      node-labeler-job.yaml        ← {{- if .Values.gpu.autoLabel }} Job 自動對符合條件的節點打 label
                                   ←   含 ServiceAccount + ClusterRole + ClusterRoleBinding 給 Job 用
    monitoring/             ← {{- if .Values.monitoring.enabled }} Prometheus + Grafana + slurm-exporter
    storage.yaml            ← {{- if .Values.storage.enabled }} NFS subdir provisioner
    tests/
      test-scontrol-ping.yaml    ← helm test 用，跑 scontrol ping + sinfo
      test-mps-job.yaml          ← gpu.enabled=true 時跑 --gres=mps:25 sbatch
```

### Chart.yaml（不含 dependencies）

```yaml
apiVersion: v2
name: slurm-platform
appVersion: "23.11.7"        # Slurm 版本，升 Slurm 透過 helm upgrade 觸發 rolling restart
version: 0.1.0
# 沒有 dependencies。GPU Operator 由 scripts/install-gpu-operator.sh 獨立安裝。
```

> [!WARNING]
> GPU Operator 預設會把 driver / toolkit / DCGM exporter 一起裝，跟 host 已裝的 `nvidia-driver-535` + `nvidia-container-toolkit` 衝突——`install-gpu-operator.sh` 用 `--set driver.enabled=false --set toolkit.enabled=false` 把這兩個子模組關掉。

### values.yaml 結構（對齊 mps-migration）

`pools` 用有序 **list**（而非 map），保證 `slurm.conf` 的 `NodeName` 順序與 `PartitionName Nodes=` 順序一致。

```yaml
cluster:
  name: slurm-lab
  namespace: slurm
  runtime: kind              # kind | k3s（影響 TaskPlugin、ProctrackType、device file 路徑）
  podSecurity: baseline      # baseline | restricted | privileged（labels namespace）

image:
  controller: slurm-controller:latest
  worker: slurm-worker:latest
  operator: slurm-elastic-operator:latest
  pullPolicy: IfNotPresent

# 每個 pool 同時帶自己的 partition 名稱（N7：partition split）
partitions:
  - name: cpu
    default: true
    maxTime: INFINITE
  - name: gpu-rtx4070
    default: false
    maxTime: 24:00:00
  - name: gpu-rtx4080
    default: false
    maxTime: 24:00:00

pools:
  - id: cpu
    statefulset: slurm-worker-cpu
    partition: cpu                       # ← 對應 partitions[].name（N7）
    minReplicas: 1
    maxReplicas: 4
    scaleCooldownSeconds: 60
    cpus: 4
    realMemory: 3500
    sockets: 1
    coresPerSocket: 2
    threadsPerCore: 2
    maxNodes: 4
    features: [cpu]
    gres: []
    fallback: true

  - id: gpu-rtx4070
    statefulset: slurm-worker-gpu-rtx4070
    partition: gpu-rtx4070               # ← N7
    minReplicas: 0
    maxReplicas: 2
    scaleCooldownSeconds: 60
    cpus: 4
    realMemory: 3500
    sockets: 1
    coresPerSocket: 2
    threadsPerCore: 2
    maxNodes: 4                          # = sharing.mps replicas，避免 Pending（N3）
    features: [gpu, gpu-rtx4070]
    gres:
      - name: gpu
        type: rtx4070
        count: 1
      - name: mps
        count: 100
    matchGres: [gpu:rtx4070, mps]
    devicePluginConfig: rtx4070-mps      # ← 對應 gpu.deviceConfigs.* key（chart 自動打 node label）

  - id: gpu-rtx4080
    statefulset: slurm-worker-gpu-rtx4080
    partition: gpu-rtx4080               # ← N7
    minReplicas: 0
    maxReplicas: 2
    scaleCooldownSeconds: 60
    cpus: 4
    realMemory: 3500
    sockets: 1
    coresPerSocket: 2
    threadsPerCore: 2
    maxNodes: 1
    features: [gpu, gpu-rtx4080]
    gres:
      - name: gpu
        type: rtx4080
        count: 1
    matchGres: [gpu:rtx4080]
    devicePluginConfig: rtx4080-exclusive

# 注意：不再有頂層 mps.enabled 旗標。MPS 由 device-plugin sharing.mps 提供（N1）；
# 是否啟用對某個 pool 而言，僅由它的 gres 是否含 mps + devicePluginConfig 是否
# 指到 *-mps 決定。worker pod 不需要 hostIPC 或 /tmp/nvidia-mps mount。

gpu:
  enabled: false                         # true 時 gpu-operator dependency 啟用
  autoLabel: true                        # true 時 chart post-install Job 自動對節點打 device-plugin.config label
  # 我們自己的 ConfigMap，由 templates/gpu/device-plugin-config.yaml 渲染。
  # GPU Operator 會掛載這個 ConfigMap 到 device-plugin pod，並依
  # nvidia.com/device-plugin.config label 選用對應 key。
  deviceConfigs:
    default:
      version: v1
    rtx4070-mps:
      version: v1
      sharing:
        mps:
          resources:
            - name: nvidia.com/gpu
              replicas: 4
    rtx4080-exclusive:
      version: v1                        # 獨佔，無 sharing
  # 對映「節點 selector → 要套哪個 config key」。autoLabel Job 用這個。
  nodeAssignments:
    - selector:
        # gpu-host-class 是使用者自己事先打的 label，例如 'rtx4070'
        gpu-host-class: rtx4070
      config: rtx4070-mps
    - selector:
        gpu-host-class: rtx4080
      config: rtx4080-exclusive

# GPU Operator subchart override（key 必須與 Chart.yaml dependency 的 name 一致）。
# 只在 gpu.enabled=true 時生效。host 已自裝 nvidia-driver-535 + nvidia-container-toolkit，
# 所以關掉 driver / toolkit；只讓 Operator 接管 device-plugin、MPS daemon、（可選）DCGM。
gpu-operator:
  driver:
    enabled: false                       # 用 host apt 裝的 nvidia-driver-535
  toolkit:
    enabled: false                       # 用 host apt 裝的 nvidia-container-toolkit
  devicePlugin:
    enabled: true
    config:
      # 指向我們自己的 ConfigMap（templates/gpu/device-plugin-config.yaml）
      name: slurm-on-k8s-device-plugin-config
      default: default
  mps:
    root: /run/nvidia/mps                # 與 host hostPath 對齊
  dcgmExporter:
    enabled: false                       # 等 5-B 觀測性再開
  gfd:                                   # gpu-feature-discovery
    enabled: true                        # 自動補 nvidia.com/gpu.product 等 label
  nodeStatusExporter:
    enabled: false
  migManager:
    enabled: false                       # RTX 4070/4080 不支援 MIG
  validator:
    plugin:
      env:
        - name: WITH_WORKLOAD            # 把 validator 的 cuda-vector-add workload 開起來
          value: "true"

slurm:
  # 這些以前散在 render-core.py header 裡，現在抽出來給 values override：
  authType: auth/munge
  credType: cred/munge
  selectType: select/cons_tres
  taskPlugin:
    kind: task/cgroup                    # k3s 預設；values-dev.yaml 覆寫成 task/none
  proctrack: proctrack/cgroup            # N11：與 task/cgroup 一致
  accounting:
    enabled: true                        # 開 slurmdbd
    storageTres: [gres/gpu, gres/mps]    # N6
  jwt:
    lifespanSeconds: 86400               # 1 day（v3 1-B 建議；舊值 10 年）
  fairshare:
    enabled: false                       # P1 之後再啟用

operator:
  pollIntervalSeconds: 15
  scaleDownCooldownSeconds: 60
  drainTimeoutSeconds: 1800              # N8
  checkpointGuard:
    enabled: true
    maxAgeSeconds: 600
    graceSeconds: 300

networkPolicy:
  enabled: true
  apiServerPorts: [443, 6443]            # N5：同時允許兩個 port

monitoring:
  enabled: true
  grafana:
    adminPassword: admin
  alertmanager:
    slack:
      webhookUrl: ""

storage:
  enabled: false                         # true → NFS subdir provisioner
  nfsServer: ""
  nfsPath: /shared

# 預先建立的 secret 名稱（chart 不產生，只引用）
secrets:
  munge: slurm-munge
  jwt: slurm-jwt
  ssh: slurm-ssh
```

### `workers.yaml`：pool StatefulSet 迴圈（無 hostIPC）

```yaml
{{- range .Values.pools }}
{{- $pool := . }}
{{- $isGpu := gt (len $pool.gres) 0 }}
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: {{ $pool.statefulset }}
spec:
  replicas: {{ $pool.minReplicas }}
  ...
    spec:
      # 注意：不需要 hostIPC，sharing.mps 由 device-plugin DaemonSet 處理
      containers:
        - name: slurm-worker
          {{- if and $isGpu (eq $.Values.cluster.runtime "k3s") }}
          resources:
            limits:
              nvidia.com/gpu: "1"           # 拿一個 sharing.mps 切片
          {{- end }}
          # 兩個 ConfigMap 分別 mount，pool 變動只影響 nodes ConfigMap
          volumeMounts:
            - name: slurm-config-static
              mountPath: /etc/slurm/slurm.conf
              subPath: slurm.conf
            - name: slurm-config-nodes
              mountPath: /etc/slurm/slurm.nodes.conf
              subPath: slurm.nodes.conf
            - name: slurm-config-nodes
              mountPath: /etc/slurm/gres.conf
              subPath: gres.conf
{{- end }}
```

### `operator.yaml`：`PARTITIONS_JSON` 從 values 產生

```yaml
- name: PARTITIONS_JSON
  value: |
    [
    {{- range $i, $pool := .Values.pools }}
    {{- if $i }},{{ end }}
    {"partition":"{{ $pool.partition }}",
     "worker_statefulset":"{{ $pool.statefulset }}",
     "min_replicas":{{ $pool.minReplicas }},
     "max_replicas":{{ $pool.maxReplicas }},
     "scale_up_step":1,"scale_down_step":1,
     "scale_down_cooldown":{{ $pool.scaleCooldownSeconds }},
     "drain_timeout_seconds":{{ $.Values.operator.drainTimeoutSeconds }},
     "match_features":{{ $pool.features | toJson }},
     {{- if $pool.matchGres }}"match_gres":{{ $pool.matchGres | toJson }},{{ end }}
     "fallback":{{ default false $pool.fallback }}}
    {{- end }}
    ]
```

使用者只需要事先對節點打一個語義化 label（如 `gpu-host-class=rtx4070`），chart 就會自動把對應 device-plugin config 的 label 套上去。

### values overlay 策略（移除 `mps.enabled`）

| 檔案 | 用途 | 關鍵差異 |
|------|------|---------|
| `values.yaml` | 基準（Kind 開發） | `runtime: kind`、`gpu.enabled: false`、`slurm.taskPlugin.kind: task/none`、`storage/monitoring: enabled` 看情況 |
| `values-k3s.yaml` | Linux + 真實 GPU + MPS | `runtime: k3s`、`gpu.enabled: true`、`gpu.autoLabel: true` |
| `values-dev.yaml` | CI / 無 GPU 環境 | `gpu.enabled: false`、`monitoring.enabled: false`、`storage.enabled: false`、`pools` 只留 cpu |

### Helm test

`templates/tests/test-scontrol-ping.yaml`：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: "{{ .Release.Name }}-test-scontrol"
  annotations:
    "helm.sh/hook": test
spec:
  restartPolicy: Never
  containers:
    - name: test
      image: {{ .Values.image.controller }}
      command: [sh, -c, "scontrol ping && sinfo -h"]
```

`gpu.enabled=true` 時加 `test-mps-job.yaml`，跑 `--gres=mps:25` sbatch 並確認 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25` 出現在 job env（呼應 `verify-gpu.sh` step 6）。

### 漸進實作順序（6 個 PR，避免 big-bang 重寫）

| 階段 | 內容 | 完成標準 | 風險 |
|:---:|---|---|:---:|
| **A** | `chart/` scaffold：`Chart.yaml`、`values.yaml`、`_helpers.tpl` 把 `render-core.py::build_slurm_conf` 翻成 Go template；其他 templates 為空 | `helm template chart/ -f values-k3s.yaml \| diff -u manifests/core/slurm-static.yaml`，差異收斂到只剩格式空白 | 中 |
| **B** | 加 `templates/configmap-static.yaml` + `configmap-nodes.yaml` + `controller.yaml` + `workers.yaml` + `pvc.yaml` + `namespace.yaml`（PSS baseline label） | `helm install` 後 `slurmctld` 起得來、`sinfo` 看到所有 pool 的 node | 低 |
| **C** | 加 `operator.yaml` + `network-policy.yaml`（443+6443）+ `login.yaml` | operator 擴／縮 worker 正常、`scale_action` metric 有資料、`scontrol ping` from login pod 成功 | 低 |
| **D** | 加 `gpu/` 子目錄（`gpu-operator-namespace.yaml` + `device-plugin-config.yaml` ConfigMap + `node-labeler-job.yaml`）；新增 `scripts/install-gpu-operator.sh` 把 NVIDIA GPU Operator 獨立裝到 `gpu-operator` namespace（`--set driver.enabled=false --set toolkit.enabled=false`）；移除自寫 `manifests/gpu/nvidia-device-plugin.yaml` | `verify-gpu.sh` 全綠（**含 step 6 MPS**——這是 Phase 5-A 的核心 milestone，因為 GPU Operator 才能讓 `--gres=mps:N` 成立）| 中-高 |
| **E** | 把 monitoring / storage 收進 chart `templates/monitoring/` `storage.yaml`，`enabled` flag 控制 | `helm install --set monitoring.enabled=true` 一次帶起 Prometheus + Grafana + slurm-exporter | 低 |
| **F** | 砍掉 `render-core.py`、`scripts/bootstrap*.sh` 大部分內容（保留 `setup-linux-gpu.sh` 與 `create-secrets.sh`）、`manifests/core/slurm-static.yaml`、`worker-pools.json`；README / migration.md 改寫為 `helm install` | bootstrap 時間 < 5 分鐘；`docs/migration.md` 從多步腳本改為 4 行 helm 指令 | 高 |

> 階段 A–C 可在 Windows + Kind 上做完並驗證；階段 D 需要 Linux + 真實 GPU 才能整合測試。階段 E 與目前 `bootstrap-monitoring.sh` 行為對齊，最低風險。階段 F 是 cutoff，需要 README、CI、文件大量同步更新。

### 廢棄的檔案（Phase F 後）

| 檔案 | 取代者 |
|------|--------|
| `manifests/core/worker-pools.json` | `chart/values.yaml::pools` |
| `scripts/render-core.py` | `chart/templates/_helpers.tpl` |
| `scripts/bootstrap.sh` 大部分 | `helm install` |
| `scripts/bootstrap-gpu.sh` | `scripts/install-gpu-operator.sh` + chart 內 node-labeler Job |
| `scripts/bootstrap-monitoring.sh` | `helm install --set monitoring.enabled=true` |
| `manifests/core/slurm-static.yaml` | `helm template chart/` 動態產生 |
| `manifests/gpu/nvidia-device-plugin.yaml` | install-gpu-operator.sh + chart 的 device-plugin-config ConfigMap（GPU Operator 內建 device-plugin DaemonSet）|
| `manifests/gpu/mps-daemonset.yaml`（已是 stub） | 直接刪除（GPU Operator 內建 `mps-control-daemon` DaemonSet）|

保留：`scripts/setup-linux-gpu.sh`（host 層 NVIDIA toolkit + k3s 安裝，本來就不該進 chart）、`scripts/create-secrets.sh`（chart 之外的 prerequisite）、`scripts/verify*.sh`（Helm test 之外的 e2e 驗證）。

### 不在 5-A 範圍但需要決定的事

1. **secret 怎麼生**：起步用 `scripts/create-secrets.sh` 預先建立；長期可改 External Secrets / Sealed Secrets。
2. **CI 怎麼跑**：`helm lint` + `helm template -f values-dev.yaml \| kubectl apply --dry-run=server -f -` + `helm-unittest` 一支 GitHub Actions workflow。
3. **GitOps 接入點**：等 chart 進 OCI registry 或 GitHub Pages 後，ArgoCD `Application` 指向那個 repo + values overlay，不需要動 chart 本體。

---

## 5-B：OpenTelemetry 分散式追蹤

### 為什麼 metrics 不夠
Prometheus 告訴你「現在 p95 provisioning latency 是 45 秒」，但不告訴你：
- 這 45 秒是花在 K8s 排程（pending pod）、image pull、還是 Slurm node registration？
- 是某個特定 job 特別慢，還是系統性問題？

OpenTelemetry trace 回答的是「**這一次** job J42 為什麼比較慢」。

### Trace 結構設計

```
TraceID: job-{SLURM_JOB_ID}
│
├── [Span] job_submit
│     attributes: job_id, partition, gres, requested_cpus
│
├── [Span] queue_wait  (start=submit_time, end=scale_up_decision_time)
│     attributes: pending_jobs_at_submit, pool
│
├── [Span] scale_up_decision  (Operator)
│     attributes: from_replicas, to_replicas, reason
│
├── [Span] k8s_provisioning  (start=patch_time, end=pods_ready_time)
│     attributes: pool, target_replicas
│     → 已有資料來源：slurm_operator_provisioning_latency_seconds histogram
│
├── [Span] slurm_node_registration
│     attributes: node_name, registered_at
│
├── [Span] job_running  (start=start_time, end=end_time)
│     attributes: nodes, cpus, gres
│
└── [Span] checkpoint_write  (fine-tuning job 專用，SIGTERM handler 觸發)
      attributes: checkpoint_path, file_size_bytes
```

### 實作路徑
1. **Operator 加 OTel SDK**：`opentelemetry-sdk` + `opentelemetry-exporter-otlp`，在 `scale_action`、`loop_observation` 決策處建立 span。
2. **Exporter 加 Span**：每次 slurmrestd REST 呼叫包在 span 裡，紀錄 HTTP latency。
3. **OTel Collector**：monitoring namespace 部署 `otel/opentelemetry-collector`，接收 OTLP，轉發到 Grafana Tempo。
4. **Grafana Tempo 整合**：加 Tempo datasource + exemplar 連結，讓 Prometheus histogram spike 可以直接跳到對應 trace。

### Exemplar 連結（差異化觀測點）
在 Provisioning Latency p95 圖上，spike 對應的那個 TraceID 可以直接點進去看整條鏈。這是目前所有 Slurm-on-K8s 開源方案（SUNK、Slinky、Slonk）都沒有做到的端到端觀測視角。

---

## 5-C：工作負載模板

### 目標
NFS `/shared/templates/` 預放 5 支對應平台典型使用情境的 sbatch 模板，使用者登入後 cp 過去修改參數即可提交，不需要自己研究 GRES 語法。

| 模板 | GRES 設定 | 展示的系統能力 |
|------|----------|--------------|
| `01_preprocess.sh` | `--cpus-per-task=8` | CPU pool autoscale，與 GPU job 完全並行 |
| `02_batch_infer.sh` | `--gres=mps:25` | MPS 多工，4 個推論 job 共用同一張 GPU |
| `03_hpo_array.sh` | `--array=1-8 --gres=mps:25` | Job array + MPS，8 組超參數實驗並行 |
| `04_finetune_lora.sh` | `--gres=gpu:rtx4080:1` | 整卡獨佔 + checkpoint guard 縮容保護 |
| `05_ddp_2gpu.sh` | `--nodes=2 --gres=gpu:1` | 跨 worker pod 的 2-GPU DDP 訓練 |

### 實作路徑
- 新增 `templates/` 目錄，每支腳本含詳細行內註解（GRES 含義、資源選擇理由）。
- `bootstrap-lmod.sh` 結尾加一步：`cp -r templates/ /shared/templates/`。
- Login pod `Dockerfile` 加入 `/etc/motd`，顯示平台簡介與模板路徑。

---

## 5-D：SSH Login

### 問題
目前進入 login node 需要 `kubectl exec -it deploy/slurm-login -- bash`，使用者必須安裝 kubectl 並持有 kubeconfig，這不是「共用 AI 計算平台」應有的使用體驗。

### 目標
```
ssh -p 2222 user@<k3s-host-ip>
       ↓
NodePort :2222 → slurm-login pod
                   ├── sbatch / squeue / sinfo（Slurm 指令即開即用）
                   └── /shared/（NFS 掛載，模型 + 輸出 + 模板共用）
```

### 實作路徑
- `docker/login/Dockerfile` 加入 `openssh-server`，SSH key 認證（禁用密碼登入）。
- `slurm-login` Service 改為 `NodePort`，固定 port 2222。
- `scripts/bootstrap.sh` 加入 SSH host key 初始化（`ssh-keygen -A`）。
- 後續（多租戶時）：`scripts/add-user.sh` 同時呼叫 `useradd` 和 `sacctmgr add user`。

---

## FAQ

**為什麼 Slurm node 要預先全部宣告？**
所有節點在 `slurm.conf` 裡預先定義到 `maxNodes`，Operator 只調整 StatefulSet 的 replica 數，而不重寫 Slurm 設定檔。這避免了每次擴縮時 `slurmctld` 重新解析所有 DNS 造成的連鎖延遲。

**為什麼 Operator 不用 Kopf 或 CRD？**
刻意保持輕量。Operator 是純 Python，沒有自訂 CRD、沒有 webhook，部署門檻低，邏輯一眼就能看懂。Slurm 狀態查詢（queue、job、node）透過 slurmrestd REST API 進行；StatefulSet replica 調整仍透過 `kubectl patch`。

**Checkpoint Guard + Drain-then-Scale 是什麼？**
縮容分兩個階段保護執行中的 AI 訓練任務：

1. **Checkpoint Guard**：若 checkpoint 檔案不存在或超過 `MAX_CHECKPOINT_AGE_SECONDS`（預設 10 分鐘），縮容決策會被阻擋。`CHECKPOINT_PATH=""` 時自動停用（避免靜默 block）；`CHECKPOINT_GRACE_SECONDS` 允許 job 啟動初期尚未寫出 checkpoint 時仍可縮容（grace period 內不阻擋）。
2. **Drain-then-Scale**：通過 Guard 後，Operator 先呼叫 `scontrol update State=DRAIN` 將目標節點標記為不接受新 job，等到 `CPUAlloc == 0`（節點上所有 job 都跑完）才真正減少 StatefulSet replica，避免執行中的訓練被強制中斷。若在 drain 等待期間有新的 scale-up 需求，drain 會自動取消（`State=RESUME`）。

**Operator Cooldown 如何在 Pod 重啟後存活？**
每次 scale-up 成功後，Operator 把時間戳寫入 StatefulSet annotation `slurm.k8s/last-scale-up-at`。Pod 重啟時從 annotation 還原 cooldown 計時，避免重啟後立即觸發錯誤的縮容。

**為什麼 slurmctld 不怕 pod 重啟？**
`StateSaveLocation`（`/var/spool/slurmctld`）掛載了獨立的 PVC（`slurm-ctld-state`）。controller pod 重啟後，job queue、node 狀態、及會計紀錄指標都會從磁碟還原，不需要重新提交任務。

**slurmdbd 提供什麼？**
slurmdbd 搭配 MySQL 後端，把每個 job 的 CPU-hours、使用者、帳戶等資訊持久化。`sacct` 可以查詢歷史 job 統計，也是 Phase 5 Fair-Share 多租戶排程的前置條件。

# 工作和硬體資源的分配關係

1. [資源模型概覽](#-資源模型概覽)
2. [CPU Job 分配](#cpu-job-分配)
3. [GPU Job 分配](#gpu-job-分配)

## 📦 資源模型概覽

本專案採用 **Slurm-on-K8s 雙層架構**，每個 worker node 是一個 K8s Pod，Slurm 把整個 Pod 視為一台 node：

```
Slurm Layer  ←─ 排程、配置記帳（CR_Core、GRES）
     ↓
K8s Layer    ←─ 容器資源請求（requests/limits）、device plugin
     ↓
Hardware     ←─ 實體 CPU cores、GPU SM + VRAM
```

關鍵設定（`slurm.conf`）：

| 參數 | 值 | 意義 |
|------|----|------|
| `SelectType` | `select/cons_tres` | CPU/GPU 皆以 consumable resource 模式分配 |
| `SelectTypeParameters` | `CR_Core` | Slurm 以 core 為單位追蹤 CPU 消耗 |
| `TaskPlugin` | `task/none` | **無 cgroup/CPU binding**，排程計帳但不強制隔離 |
| `GresTypes` | `gpu` | GPU 宣告為 GRES |

每台 worker 宣告：CPU worker → `CPUs=4`；GPU worker → `CPUs=4` + `Gres=gpu:<type>:1`

---

## 📍 部署環境規格：Linux + k3s + RTX 4070 + RTX 4080（雙 GPU）

> 本節以實際遷移目標環境為例，說明硬體資源如何對應到各層宣告。

### 主機硬體

| 項目 | 規格 | 說明 |
|------|------|------|
| CPU | 12 cores（如 Intel i7-12700 / Ryzen 9 5900X） | k3s 單節點，所有 pod 共用 |
| RAM | 32 GB DDR5 | 各 worker pod 依 `RealMemory` 宣告分配帳本 |
| GPU 0 | NVIDIA RTX 4070（Blackwell GB203） | `/dev/nvidia0`，主 GPU |
| GPU 0 VRAM | 12 GB GDDR7 | 單一作業可用全部 12 GB |
| GPU 0 SM | 48 SM | MPS 下 `mps:25` ≈ 12 SM |
| GPU 0 CUDA Cores | 6144 | 記憶體頻寬 ~672 GB/s |
| GPU 1 | NVIDIA RTX 4080（Ada Lovelace AD103） | `/dev/nvidia1`，附加 GPU |
| GPU 1 VRAM | 16 GB GDDR6X | 單一作業可用全部 16 GB |
| GPU 1 SM | 76 SM | MPS 下 `mps:25` ≈ 19 SM |
| GPU 1 CUDA Cores | 9728 | 記憶體頻寬 ~717 GB/s |
| OS / Runtime | Ubuntu 22.04 + containerd + k3s | `K8S_RUNTIME=k3s` |

### Worker Pod 資源宣告對照

在 k3s 單節點上，所有 worker pod 實際跑在同一台實體主機。Slurm 帳本追蹤的是「pod 宣告量」，OS 層透過 **cgroup v2**（`TaskPlugin=task/cgroup`，`REAL_GPU=true`）做實際隔離。

| Worker 類型 | StatefulSet 名稱 | Slurm CPUs | Slurm RealMemory | GRES | K8s resource.limits |
|------------|----------------|-----------|-----------------|------|---------------------|
| CPU worker | `slurm-worker-cpu` | 4 cores | 3500 MB (~3.4 GB) | 無 | cpu: 4, memory: 3500Mi |
| GPU worker (RTX 4070) | `slurm-worker-gpu-rtx4070` | 4 cores | 3500 MB | gpu:rtx4070:1, mps:100 | cpu: 4, memory: 3500Mi, nvidia.com/gpu: 1 |
| GPU worker (RTX 4080) | `slurm-worker-gpu-rtx4080` | 4 cores | 3500 MB | gpu:rtx4080:1, mps:100 | cpu: 4, memory: 3500Mi, nvidia.com/gpu: 1 |

> 兩張 GPU 各為獨立 StatefulSet，`maxNodes=1`（各只能開 1 個 GPU pod）。K8s device plugin 透過 `CUDA_VISIBLE_DEVICES` 決定 pod 拿到哪張 GPU（`0` = RTX 4070，`1` = RTX 4080）。CPU worker pool 最多可開 4 個 pod。

### 工作類型與資源分配

以下五種工作類型對應平台的典型 AI 使用情境，每種類型都由不同的系統能力撐起。

#### Type 1：資料前處理（Preprocessing）

使用情境：在跑推論或訓練之前，先把原始文本 tokenize、格式轉換、資料清洗。

```bash
#!/bin/bash
#SBATCH --job-name=preprocess
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8    # 純 CPU，多 thread 平行處理
#SBATCH --mem=8G
#SBATCH --constraint=cpu
#SBATCH --output=/shared/jobs/%j-preprocess.out

python tokenize_dataset.py \
  --input  /shared/data/raw/ \
  --output /shared/data/tokenized/ \
  --workers 8
```

**分配流程：**
```
提交 → slurmctld 掃描 cpu-worker 帳本
     → slurm-worker-cpu-0 空閒，分配 8 cores
     → cgroup v2 將 pid 綁定到指定 cpuset
     → 與同時段的 GPU job 完全並行，互不干擾
     → 完成後輸出寫入 /shared/data/（NFS，所有 pod 可讀）
```

| 資源 | 宣告量 | 強制方式 |
|------|--------|---------|
| CPU | 8 cores | cgroup cpuset |
| RAM | 8 GB | cgroup memory |
| GPU | 無 | — |

> 展示的系統能力：CPU pool 獨立 autoscale；CPU job 與 GPU job 不競爭資源；NFS 讓輸出跨節點共享。

---

#### Type 2：批次文字推論（Batch Inference with MPS）

使用情境：對一批文件（1000 筆）跑模型推論，取得分類結果、摘要、或向量表示。多個推論 job 透過 MPS 共用同一張 GPU 的 SM。

```bash
#!/bin/bash
#SBATCH --job-name=batch-infer
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --gres=mps:25              # 請求 25% SM（RTX 4070 上 ≈ 12 SM）
#SBATCH --constraint=gpu-rtx4070
#SBATCH --output=/shared/jobs/%j-infer.out

python infer_batch.py \
  --model  /shared/models/bert-base/ \
  --input  /shared/data/tokenized/ \
  --output /shared/results/infer-$SLURM_JOB_ID/
```

**MPS 分配流程（4 個 job 同時跑）：**
```
Job 1（mps:25）+ Job 2（mps:25）+ Job 3（mps:25）+ Job 4（mps:25）
     ↓
RTX 4070 MPS Daemon（/tmp/nvidia-mps-0）
     ↓
48 SM 被 4 個 process 共享，每個各佔 12 SM
GPU SM utilization：4 × 12 = 48 SM（100% 滿載）
對比無 MPS 的串行：同樣 4 個 job 要花 4× 時間
```

| 資源 | 每個 job | 4 個 job 合計 |
|------|---------|-------------|
| GPU SM | 12 SM（25%） | 48 SM（100%） |
| GPU VRAM | 共享 12 GB（無隔離） | — |
| CPU | 2 cores | 8 cores |

> 展示的系統能力：MPS 細粒度 GPU 分配；GPU utilization 從 ~20%（串行）提升到 ~80–100%（並行）；Operator 偵測 queue 後自動開啟 GPU worker。

---

#### Type 3：超參數搜尋（HPO with Job Array）

使用情境：對同一個模型嘗試 8 組不同的 learning rate / batch size 組合，找出最佳設定。每組實驗是獨立的 sbatch job，透過 `--array` 並行提交。

```bash
#!/bin/bash
#SBATCH --job-name=hpo
#SBATCH --array=1-8               # 8 組實驗，Task ID 1–8
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --gres=mps:25             # 每個實驗佔 25% SM
#SBATCH --constraint=gpu-rtx4070
#SBATCH --output=/shared/jobs/%A-%a-hpo.out

# 從 Task ID 選參數
LR_LIST=(1e-5 2e-5 5e-5 1e-4 2e-4 5e-4 1e-3 2e-3)
LR=${LR_LIST[$((SLURM_ARRAY_TASK_ID - 1))]}

python train_experiment.py \
  --lr $LR \
  --output /shared/results/hpo-$SLURM_ARRAY_JOB_ID-$SLURM_ARRAY_TASK_ID/
```

**資源分配流程：**
```
提交 --array=1-8
     ↓
slurmctld 看到 8 個 pending job，各需要 mps:25
     ↓
Operator 偵測 pending count > 0 → 開啟 GPU worker pod
     ↓
RTX 4070 最多同時跑 4 個（4 × mps:25 = 100% SM）
     ↓
4 個跑完釋放 → 另外 4 個接著跑
     ↓
總時間 ≈ 2 輪 × 20 分鐘 = 40 分鐘
對比串行：8 × 20 分鐘 = 160 分鐘（縮短 4×）
```

> 展示的系統能力：Job array 批次提交；MPS 讓多組實驗並行；Operator autoscale 依 queue 深度動態開 worker。

---

#### Type 4：LoRA Fine-tuning（GPU 獨佔 + Checkpoint 保護）

使用情境：對預訓練模型做領域適應（fine-tuning），需要整張 GPU VRAM 和長時間運算，途中定期寫 checkpoint，確保意外中斷可以續跑。

```bash
#!/bin/bash
#SBATCH --job-name=finetune-lora
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=14G
#SBATCH --gres=gpu:rtx4080:1      # 獨佔整張 RTX 4080（16 GB VRAM）
#SBATCH --constraint=gpu-rtx4080
#SBATCH --output=/shared/jobs/%j-finetune.out

python finetune_lora.py \
  --model     /shared/models/llama-3.1-8b/ \
  --data      /shared/data/train.jsonl \
  --output    /shared/checkpoints/run-$SLURM_JOB_ID/ \
  --ckpt-every 500   # 每 500 steps 寫一次 checkpoint
```

**分配流程：**
```
提交 → slurmctld 查 GRES 帳本：gpu:rtx4080
     → slurm-worker-gpu-rtx4080-0 空閒，整卡分配
     → K8s device plugin 把 /dev/nvidia1 綁入 pod
     → CUDA_VISIBLE_DEVICES=0（pod 內）

縮容保護流程：
Operator 決策要縮容 GPU worker
     ↓
Checkpoint Guard：檢查 /shared/checkpoints/run-$JID/latest.ckpt
     → 若不存在或超過 MAX_CHECKPOINT_AGE（10 分鐘）→ 阻擋縮容
     → 存在且新鮮 → 允許縮容（等 job 完成）
```

| 資源 | 宣告量 | 說明 |
|------|--------|------|
| GPU VRAM | 16 GB（整卡） | 可跑 Llama 3.1 8B FP16 |
| GPU SM | 76 SM（100%） | 不與其他 job 共用 |
| CPU / RAM | 4 cores / 14 GB | 資料 loading / preprocessing |

> 展示的系統能力：整卡獨佔 GRES 分配；Checkpoint-aware 縮容保護；NFS PVC 讓 checkpoint 跨 pod 持久化。

---

#### Type 5：雙 GPU DDP 訓練（Optional）

使用情境：模型或 batch size 太大，單卡放不下，需要跨兩個 GPU worker 做梯度同步。每個 worker pod 各持一張 GPU，NCCL AllReduce 走 K8s pod 網路。

```bash
#!/bin/bash
#SBATCH --job-name=ddp-2gpu
#SBATCH --nodes=2                  # 2 個 Slurm worker pod
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=12G
#SBATCH --gres=gpu:1               # 不指定型號，各給 1 張
#SBATCH --output=/shared/jobs/%j-ddp.out

torchrun \
  --nproc_per_node=1 \
  --nnodes=2 \
  --node_rank=$SLURM_NODEID \
  --master_addr=$(scontrol show hostnames "$SLURM_NODELIST" | head -1) \
  --master_port=29500 \
  train_ddp.py \
    --data       /shared/data/tokenized/ \
    --checkpoint /shared/checkpoints/ddp-$SLURM_JOB_ID/
```

**分配流程：**
```
提交 --nodes=2 --gres=gpu:1
     ↓
slurmctld 找 2 台空閒 GPU worker
     → rank 0：slurm-worker-gpu-rtx4070-0（/dev/nvidia0，12 GB VRAM）
     → rank 1：slurm-worker-gpu-rtx4080-0（/dev/nvidia1，16 GB VRAM）
     ↓
torchrun 啟動，NCCL 透過 K8s pod network 建立 rendezvous
     ↓
AllReduce gradient 在兩個 rank 間同步（TCP backend）
     ↓
兩個 worker 同時寫 checkpoint 到 NFS /shared/checkpoints/
```

| 資源 | Rank 0（RTX 4070） | Rank 1（RTX 4080） |
|------|-------------------|-------------------|
| GPU VRAM | 12 GB | 16 GB |
| GPU SM | 48 SM | 76 SM |
| 通訊 | NCCL over TCP（K8s pod network） | 同左 |

> 展示的系統能力：跨 worker pod 的多節點 Slurm 排程；NFS 讓兩個 worker 共讀 dataset；Checkpoint guard 保護長時間訓練不被縮容打斷。

> [!WARNING]
> 混合 GPU 型號：batch size 以較小的 RTX 4070（12 GB）為準；RTX 4070 的 SM 數也是速度瓶頸。 

### 資源帳本總覽（雙 GPU 單節點部署）

```
實體主機 (Linux + k3s)
├── CPU: 12 physical cores, 32 GB RAM
│   ├── slurm-controller-0         (系統服務 pod)
│   ├── slurm-worker-cpu-0         → Slurm 宣告 CPUs=4, Mem=3.4G
│   ├── slurm-worker-cpu-1         → 同上（operator 依需求開啟）
│   ├── slurm-worker-cpu-2         → 同上
│   ├── slurm-worker-cpu-3         → 同上
│   ├── slurm-worker-gpu-rtx4070-0 → CPUs=4, Mem=3.4G, gpu:rtx4070:1, mps:100
│   └── slurm-worker-gpu-rtx4080-0 → CPUs=4, Mem=3.4G, gpu:rtx4080:1, mps:100
│
├── GPU 0: RTX 4070 /dev/nvidia0  (12 GB VRAM, 48 SM)
│   ├── 整卡模式：slurm-worker-gpu-rtx4070-0 獨占
│   └── MPS 模式：GPU Operator 的 nvidia-mps-control-daemon DS 管理
│                 device-plugin-config key: rtx4070-mps (replicas=4)
│                 mps:N 分配 N% SM；socket 由 Operator 自動掛載到 worker pod
│
└── GPU 1: RTX 4080 /dev/nvidia0  (16 GB VRAM, 76 SM；pod 視角永遠是 /dev/nvidia0)
    ├── 整卡模式：slurm-worker-gpu-rtx4080-0 獨占
    └── device-plugin-config key: rtx4080-exclusive（不切，整卡）
```

---

## CPU Job 分配

### 排程語意：同一 worker 可容納多個 job

`cons_tres` + `CR_Core` 使 Slurm 以 core 為單位消耗 CPU slot。同一台 node 的 CPU slot 可由多個 job 分攤，只要總需求不超過宣告量。這是 HPC 常見的 **bin-packing** 行為，無需 `OverSubscribe`。

> [!NOTE]
> `OverSubscribe` 是讓多個 job「共用同一批 CPU」（超賣）；bin-packing 是不同 job 用不同 CPU slot，兩個概念不同。

### 分配範例（CPUs=4 的 cpu-worker）

| Job | 請求 | 分配在同一 worker？ | 說明 |
|-----|------|-------------------|------|
| A `--cpus-per-task=2` | 2 cores | ✅ | 佔用 slot 0-1 |
| B `--cpus-per-task=2` | 2 cores | ✅ 與 A 共存 | 佔用 slot 2-3，worker 滿載 |
| C `--cpus-per-task=1` | 1 core | ❌ 排到其他 worker | worker 已滿，Slurm 等待或排第二台 |

更複雜的例子——4 個 job 競搶 2 台 worker（各 CPUs=4）：

```
Worker-0 [4 slots]         Worker-1 [4 slots]
┌─────────────────┐        ┌─────────────────┐
│ Job A (2 cores) │        │ Job C (3 cores) │
│ Job B (2 cores) │        │ Job D (1 core)  │
└─────────────────┘        └─────────────────┘
  bin-packed: 100%            bin-packed: 100%
```

Job A、B 同時跑在 Worker-0；Job C、D 同時跑在 Worker-1。Slurm 的 backfill scheduler 會盡量填滿 slot。

### 邊界：隔離層缺失

`TaskPlugin=task/none` + `ProctrackType=proctrack/linuxproc` 表示：

- Slurm **記帳**說分給 Job A 2 cores，但 OS 不會強制 Job A 只跑在那 2 顆上
- 若應用程式內部開 `OMP_NUM_THREADS=16`，它可能吃掉 Worker 上所有 CPU，影響同 worker 的 Job B
- **效能隔離在 Kind 環境是虛的**，僅排程語意層面有效

真實 HPC 環境通常改用 `TaskPlugin=task/cgroup`，搭配 `CgroupAutomount=yes`，OS 層級強制 CPU pinning。

---

## GPU Job 分配

### 預設模型：整卡獨占

每台 GPU worker 宣告 1 個 GRES slot（如 `Gres=gpu:rtx4070:1`）。GRES 是整數消耗，無分數分配：

```
# Linux + k3s + RTX 4070 環境（maxNodes=1，只有 1 台 GPU worker）
Job A: --gres=gpu:rtx4070:1  →  佔用整張 RTX 4070，該 worker GRES=0
Job B: --gres=gpu:rtx4070:1  →  Pending，等 Job A 釋放（因為只有 1 台 GPU worker）
```

### 分配範例

**場景 A：Linux + k3s + 1 台 RTX 4070 worker（實際部署）**

| Job | 請求 | 分配結果 |
|-----|------|---------|
| A `--gres=gpu:rtx4070:1 -N 1` | 1× RTX 4070 | → worker-gpu-rtx4070-0，整張 12 GB 獨占 |
| B `--gres=gpu:rtx4070:1 -N 1` | 1× RTX 4070 | → Pending，只有 1 台 GPU worker，等 A 結束 |

**場景 B：假設未來多張 GPU 機器（可參考設計）**

| Job | 請求 | 分配結果 |
|-----|------|---------|
| A `--gres=gpu:rtx4070:1 -N 1` | 1× GPU | → worker-gpu-rtx4070-0，整張獨占 |
| B `--gres=gpu:rtx4070:1 -N 1` | 1× GPU | → worker-gpu-rtx4070-1，整張獨占 |
| C `--gres=gpu:rtx4070:1 -N 1` | 1× GPU | → Pending，等 A 或 B 釋放 |

多節點 DDP job（多 GPU worker 情境）：

```bash
#SBATCH --gres=gpu:rtx4070:1
#SBATCH -N 4          # 需要 4 台 GPU worker 同時空閒
#SBATCH --ntasks-per-node=1
```

Slurm 要求 4 台 `gpu-rtx4070` worker 同時空閒。這正是 Gang Scheduling 解決的問題——若只有 3 台空閒，K8s 1.35 原生 `GangScheduling` 會讓 4 個 worker Pod 要嘛全部調度，要嘛全不調度，避免佔著資源等人。

### GPU 共用機制（進階）

若要讓多個 job 共用同一張 GPU，需要額外機制：

#### Time-Slicing（時間切片）

CUDA context 輪流使用 GPU，類似 CPU 分時多工。

- 適用：所有 NVIDIA GPU
- 隔離：無記憶體隔離（所有 context 共享 VRAM），context switch 有開銷
- K8s 設定：GPU Operator ConfigMap 把 1 張 GPU 虛擬成 N 份 `nvidia.com/gpu`

```yaml
# ConfigMap：1 張 RTX 4070 虛擬成 4 份（需 GPU Operator，本架構未使用）
sharing:
  timeSlicing:
    resources:
    - name: nvidia.com/gpu
      replicas: 4
```

```ini
# gres.conf（Slurm 端）
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 Count=4 File=/dev/nvidia0
```

4 個 job 可同時各請求 `--gres=gpu:rtx4070:1`，時間輪流使用同一張 GPU。**不適合 DDP 訓練**（延遲不可預測、無記憶體保護）。

#### MPS（Multi-Process Service）

多個 CUDA process 合併進同一 CUDA context，共享 command queue 和 SM，減少 context switch 開銷。SM 可設 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 比例。適合延遲敏感的小型推論，但無完整記憶體隔離。

---

# GPU MPS 完整實作指南

本架構環境預計使用 Linux + k3s + RTX 4070 + RTX 4080（雙 GPU），目前在單一主機 RTX 4070 開發，之後會嘗試用雙顯卡。

| 項目 | GPU 0：RTX 4070 | GPU 1：RTX 4080 |
|------|----------------|----------------|
| 架構 | Blackwell GB203 | Ada Lovelace AD103 |
| SM 數量 | 48 SM | 76 SM |
| VRAM | 12 GB GDDR7 | 16 GB GDDR6X |
| 裝置路徑 | `/dev/nvidia0` | `/dev/nvidia1` |
| MPS socket 目錄 | `/tmp/nvidia-mps-0` | `/tmp/nvidia-mps-1` |
| MPS 支援 | ✅（Volta+） | ✅（Volta+） |
| MIG 支援 | ❌ | ❌ |
| K8s Runtime | k3s + containerd + NVIDIA Container Toolkit（共用） | ← |

## MPS 運作原理 vs Time-Slicing

| 維度 | Time-Slicing | MPS | MIG |
|------|-------------|-----|-----|
| 機制 | 多個 CUDA context 輪流使用 GPU（OS 時間片） | 所有 process 共用**同一個** CUDA context，SM 並行執行 | 硬體切分 GPU 為獨立 instance |
| 並行度 | 無（序列執行） | **高**（多 process 真正同時跑在 SM 上） | 有（instance 間獨立） |
| 記憶體隔離 | ❌（VRAM 共享） | ❌（一個 OOM 可能拖垮其他 process） | ✅（各 instance VRAM 隔離） |
| context switch overhead | 高（µs 級別） | **極低**（無 context switch） | 無（instance 獨立） |
| SM 配額控制 | ❌ | ✅ `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` | ✅（profile 固定） |
| 支援 GPU | 全部 NVIDIA | **Volta+（含 RTX 4070）** | A100/H100/A30 only |
| RTX 4070 適用 | ✅ | **✅ 本架構採用** | ❌ 不支援 |
| 最佳場景 | 開發/測試 | **推論服務多 replica 共用 GPU** | 多租戶需記憶體隔離 |

✔️ 雙 GPU + MPS 適合的場景：
- 多個小型推論 server 並排（LLM serving、image classifier），兩張 GPU 分流不同服務
- 批次推論（offline batch inference），task 間共享 SM bandwidth
- 教授展示：同一張 GPU 跑多個 AI 服務而不互相等待；兩張 GPU 同時跑不同服務

❌ 不適合的場景：
- PyTorch DDP 訓練（DDP 本身已充分利用整張 GPU，加 MPS 只增加風險；DDP 請用整卡模式）
- 需要記憶體隔離的多租戶（兩張卡均無 MIG，MPS 沒有 VRAM 隔離）
- 不同 Linux 用戶共用同一張 GPU（每個 Linux 用戶只能有一個 MPS server）

## 實作架構：GPU Operator + MPS on k3s

> 2026-04-29 update（Phase 5-A 完成）：使用 NVIDIA **GPU Operator**的 `mps-control-daemon` 自動管理 MPS daemon（前景模式跑，繞過 v0.15–v0.17.x device-plugin 的 spawn race，見 [`docs/migration.md`](migration.md)）；GPU sharing 策略由 device-plugin-config ConfigMap 表達；節點透過 `nvidia.com/device-plugin.config=<key>` label 匹配自己的 sharing 配置。本節描述**目前的實作**。

```
Linux Host（k3s 單節點）
┌────────────────────────────────────────────────────────────────────────────┐
│  namespace: gpu-operator (PSS=privileged，scripts/install-gpu-operator.sh)  │
│  ┌─────────────────────────────┐  ┌───────────────────────────────────┐    │
│  │ nvidia-device-plugin DS     │  │ nvidia-mps-control-daemon DS      │    │
│  │  讀 ConfigMap:               │  │  per-GPU MPS server（前景模式）   │    │
│  │  slurm-platform-device-      │  │  socket 目錄由 Operator 管：     │    │
│  │  plugin-config（rtx4070-mps  │  │  /run/nvidia/mps/{gpu-uuid}/      │    │
│  │  / rtx4080-exclusive / ...） │  │  以 hostPath mount 進 worker pod  │    │
│  └─────────────────────────────┘  └───────────────────────────────────┘    │
│                                                                            │
│  namespace: slurm (PSS=baseline, helm install slurm-platform)              │
│  ┌─────────────────────────────┐  ┌───────────────────────────────────┐    │
│  │ slurm-worker-gpu-rtx4070    │  │ slurm-worker-gpu-rtx4080          │    │
│  │  node label:                 │  │  node label:                       │    │
│  │   nvidia.com/device-plugin.  │  │   nvidia.com/device-plugin.        │    │
│  │   config=rtx4070-mps         │  │   config=rtx4080-exclusive         │    │
│  │  Job A (mps:50=24SM)         │  │  Job D 整卡訓練                   │    │
│  │  Job B (mps:25=12SM)         │  │   (76 SM, 16 GB VRAM)              │    │
│  │  Job C (mps:25=12SM)         │  │                                   │    │
│  │  --gres=mps:N → SM%          │  │  --gres=gpu:rtx4080:1              │    │
│  └─────────────────────────────┘  └───────────────────────────────────┘    │
│                                                                            │
│  /dev/nvidia0  RTX 4070 (12 GB, 48 SM)   分成 4 個 MPS slot（replicas=4）  │
│  /dev/nvidia1  RTX 4080 (16 GB, 76 SM)   不分割（exclusive）              │
└────────────────────────────────────────────────────────────────────────────┘
```

- sharing 策略宣告式：每張卡的 MPS replicas / time-slicing / exclusive 由 ConfigMap 定義，不是腳本邏輯
- per-pool 配置：rtx4070 與 rtx4080 走不同的 ConfigMap key，藉 node label 自動匹配
- chart 不擁有 GPU Operator：`scripts/install-gpu-operator.sh` 把它獨立裝到 PSS=privileged 的 `gpu-operator` namespace；`slurm-platform` chart 只貢獻 device-plugin-config ConfigMap 與 node-labeler Job

### 實作步驟（Linux + k3s + RTX 4070 + RTX 4080）

完整流程見 [README.md → Getting Started 路徑 A](../README.md#a-linux--k3s--real-gpu)。本節聚焦 **MPS / GPU sharing 部份**。

---

**步驟一：確認 NVIDIA 環境**

```bash
nvidia-smi --list-gpus
# GPU 0: NVIDIA GeForce RTX 4070
# GPU 1: NVIDIA GeForce RTX 4080
```

主機驅動可以是 535 / 595（Phase 5-A 都驗過）。Container Toolkit 由 `scripts/setup-linux-gpu.sh` 裝。

---

**步驟二：device-plugin-config ConfigMap（chart 提供）**

`chart/values.yaml::gpu.deviceConfigs` 渲染成單一 ConfigMap `slurm-platform-device-plugin-config`（在 `gpu-operator` namespace）：

```yaml
# chart/values.yaml 對應段落（k3s overlay 啟用）
gpu:
  enabled: true
  targetNamespace: gpu-operator
  deviceConfigs:
    default:
      version: v1
    rtx4070-mps:
      version: v1
      sharing:
        mps:
          resources:
            - name: nvidia.com/gpu
              replicas: 4              # 1 張 RTX4070 → 4 個 MPS slot
    rtx4080-exclusive:
      version: v1                       # 不切，整卡 exclusive
  nodeAssignments:
    - selector: { gpu-host-class: rtx4070 }
      config: rtx4070-mps
    - selector: { gpu-host-class: rtx4080 }
      config: rtx4080-exclusive
```

`helm install` 把 ConfigMap 落在 `gpu-operator` namespace（不是 slurm namespace），這是因為 GPU Operator 的 device-plugin DaemonSet 只讀自己 namespace 的 ConfigMap。

---

**步驟三：安裝 GPU Operator（獨立 helm release）**

```bash
bash scripts/install-gpu-operator.sh
```

腳本實質是：

```bash
helm install gpu-operator nvidia/gpu-operator \
  -n gpu-operator --create-namespace \
  --version v26.3.1 \
  --set driver.enabled=false \              # host 已用 setup-linux-gpu.sh 裝好
  --set toolkit.enabled=false \
  --set devicePlugin.config.name=slurm-platform-device-plugin-config \
  --set devicePlugin.config.default=default \
  --set mps.root=/run/nvidia/mps
```

GPU Operator 起來後，會看到三條主要 DaemonSet：

```bash
kubectl -n gpu-operator get ds
# nvidia-device-plugin-daemonset
# nvidia-mps-control-daemon-...        ← 只有當有 sharing.mps active 才出現
# gpu-feature-discovery-...
```

---

**步驟四：node-labeler Job 自動貼 label（chart 提供）**

`chart/templates/gpu/node-labeler-job.yaml` 是個 `helm.sh/hook=post-install,post-upgrade` 的 Job，按 `gpu.nodeAssignments` 把節點貼上 `nvidia.com/device-plugin.config=<key>`，device-plugin DaemonSet 重啟後就會用對應的 sharing 策略。

如果是單節點，需要手動先標 `gpu-host-class`：

```bash
kubectl label node <node-name> gpu-host-class=rtx4070 --overwrite
helm upgrade slurm-platform ./chart -f chart/values-k3s.yaml -n slurm   # 重跑 labeler Job
```

---

**步驟五：gres.conf 由 chart 自動生成**

`chart/templates/_helpers.tpl::gresConf` 根據 `pools[*].gres` 產生：

```ini
# k3s overlay：RTX 4070 + RTX 4080 各一張
NodeName=slurm-worker-gpu-rtx4070-0 Name=gpu Type=rtx4070 Count=1 File=/dev/nvidia0
NodeName=slurm-worker-gpu-rtx4070-0 Name=mps Count=100
NodeName=slurm-worker-gpu-rtx4080-0 Name=gpu Type=rtx4080 Count=1 File=/dev/nvidia0
```

注意：兩張卡的 `File=` 都是 `/dev/nvidia0`，因為 Slurm 是從 worker pod 的視角看裝置——每個 worker pod 只看到自己被分配的那張 GPU（kubelet + libnvidia-container 注入），所以 pod 內永遠是 `/dev/nvidia0`。

`mps:100` 代表整張卡的 SM 100%。`--gres=mps:25` 拿 25%（RTX4070 ~12 SM、RTX4080 ~19 SM）：

| 請求 + constraint | RTX 4070 分配 SM | RTX 4080 分配 SM | 適合工作 |
|-----------------|----------------|----------------|---------|
| `--gres=mps:50 --constraint=gpu-rtx4070` | ~24 SM (50%) | — | 中型推論（7B LLM serving） |
| `--gres=mps:25 --constraint=gpu-rtx4070` | ~12 SM (25%) | — | 小型推論（image classifier） |
| `--gres=gpu:rtx4070:1` | 48 SM（整卡） | — | 訓練（≤12 GB VRAM）|
| `--gres=gpu:rtx4080:1` | — | 76 SM（整卡） | 訓練（≤16 GB VRAM）|
| `-N 2 --gres=gpu:1` | 48 SM | 76 SM | **2-GPU DDP** |

---

**步驟六：Job 提交語法（不變）**

```bash
# MPS 推論
#SBATCH --gres=mps:25
#SBATCH --constraint=gpu-rtx4070
python infer.py

# 整卡訓練
#SBATCH --gres=gpu:rtx4080:1
torchrun --nproc_per_node=1 train.py

# 2-GPU DDP
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
torchrun --nproc_per_node=1 --nnodes=2 --node_rank=$SLURM_NODEID \
  --master_addr=$(scontrol show hostnames "$SLURM_NODELIST" | head -1) \
  --master_port=29500 train.py
```

---

**步驟七：驗證**

```bash
bash scripts/verify-helm.sh                         # 渲染 + dry-run + helm-unittest
bash scripts/verify-gpu.sh                          # device plugin、nvidia-smi、Slurm GRES
# 手動：直接看 GPU Operator 的 MPS daemon 狀態
mps_pod=$(kubectl -n gpu-operator get pod -l app=nvidia-mps-control-daemon -o jsonpath='{.items[0].metadata.name}')
kubectl -n gpu-operator exec "$mps_pod" -- bash -c '
  echo get_server_list | nvidia-cuda-mps-control
  echo get_client_list | nvidia-cuda-mps-control
'
nvidia-smi dmon -s u -d 2     # 多個 MPS process 應同時佔 SM
```

---

## ✴️ 本架構實作建議

| 部署環境 | 推薦做法 | 備註 |
|---------|---------|------|
| **Linux + k3s + RTX 4070 + RTX 4080（本架構）** | **GPU Operator + chart 的 device-plugin-config** | sharing 由 ConfigMap 表達，per-pool 配置 |
| Kind（Windows 開發） | `gpu.enabled=false`（預設） | Kind 無真實 GPU，純驗證排程邏輯 |
| 多節點 GPU 叢集 | 同上 + 每節點貼 `gpu-host-class` label | node-labeler Job 自動把 device-plugin config 對齊到節點 |
| 大型 HPC 叢集（多租戶精細 SM 控制） | 上述 + Slurm Prolog/Epilog 動態調 SM% | `set_active_thread_percentage` via `nvidia-cuda-mps-control` |
| 混合推論+訓練叢集（雙卡） | RTX4080 走 `rtx4080-exclusive`，RTX4070 走 `rtx4070-mps` | 已是 chart 預設 nodeAssignments |

> [!NOTE]
> `gres.conf` 與 `slurm.conf` 都由 chart helper 渲染。新增 GPU 型號只要在 `values.yaml::pools` 加一個 entry + 在 `gpu.deviceConfigs` 加對應 sharing 策略 + 在 `gpu.nodeAssignments` 加 selector 即可。

---

## 監控 MPS 使用狀況

```bash
# 在 MPS Daemon 所在 pod 或 host 執行
echo "get_server_list" | nvidia-cuda-mps-control    # 查看活躍 server
echo "get_client_list" | nvidia-cuda-mps-control    # 查看連線的 client

# 查看 GPU 利用率（多個 MPS process 應能同時看到 SM 使用）
nvidia-smi dmon -s u

# 用 DCGM 追蹤 MPS 下的 per-process GPU 使用（需 DCGM Exporter）
dcgmi group -c mps-jobs --default
dcgmi dmon -e 203,204,1002   # SM Active, SM Occupancy, Memory Active
```

---

## ⚠️ 核心限制：為何不能跨 GPU 分割 SM？

用戶場景：GPU0 剩 4 SM 閒置、GPU1 剩 4 SM 閒置，Job C 需要 8 SM，能否合用？

**直接答案：不可能。** 硬體架構根本限制：

```
  GPU0 [SM0..SM107]                  GPU1 [SM0..SM107]
┌──────────────────┐               ┌──────────────────┐
│  SM0  SM1  ...   │               │  SM0  SM1  ...   │
│  L2 Cache        │               │  L2 Cache        │
│  HBM (80 GB)     │<─PCIe/NVLink─>│  HBM (80 GB)     │
└──────────────────┘               └──────────────────┘
```

1. 無跨 GPU 共享記憶體：CUDA thread block 必須在同一張 GPU 的 SM 上存取 Shared Memory / L1 cache；跨 GPU 只能用 P2P copy（NVLink/PCIe），延遲是 SM 內 shared memory 的 100 倍以上
2. CUDA 程式模型不支援：沒有 API 可讓一個 kernel 跑在「GPU0 SM 0-3 + GPU1 SM 0-3」上
3. MIG / Time-Slicing / MPS 都是**單一 GPU 內**的分割，無法橫跨兩張

「用完碎片化 GPU 資源」的正確方法：

| 場景 | 解法 | 支援 GPU |
|------|------|---------|
| GPU0 有 25% 閒置，想跑小 job | Time-Slicing 或 MIG `1g.10gb` | 全部 / 僅 A100,H100 |
| 多推論任務共用一張 GPU | MPS 或 time-slicing | Volta+ |
| 多租戶需記憶體隔離 | MIG | 僅 A100,H100,A30 |
| DDP 大型訓練 | 整張 GPU（1 job per GPU）| 全部 |
| 跨 GPU SM 碎片整合 | ❌ 硬體不支援 | 無 |

### Summary

| 問題 | 答案 |
|------|------|
| Job 能指定 CPU 數量？ | ✅ `--cpus-per-task`、`--ntasks` |
| Job 能指定 GPU 型號和數量？ | ✅ `--gres=gpu:a10:1` |
| 同一 CPU worker 能跑多個 job？ | ✅ 排程層（`CR_Core` bin-packing） |
| CPU 有實體隔離？ | ❌ `task/none`，無 binding/cgroup（Kind 限制） |
| 同一張 GPU 能讓多個 job 共用？ | ✅ 可透過 time-slicing 或 MIG（需真實 GPU + 額外設定） |
| GPU core 能跨多張 GPU 分割給同一 job？ | ❌ CUDA 硬體架構根本限制 |
| Kind 環境的 GPU GRES 是真實硬體？ | ❌ `File=/dev/null`，純排程模擬 |

---

# 📖 參考來源

- [Slurm GRES MPS 官方文件](https://slurm.schedmd.com/gres.html) — `GresTypes=gpu,mps` 設定與 Prolog 行為
- [GPU Slicing in CycleCloud Slurm with CUDA MPS](https://techcommunity.microsoft.com/blog/azurehighperformancecomputingblog/gpu-slicing-in-cyclecloud-slurm-with-cuda-multi-process-service-mps/4365999) — Microsoft Azure HPC，Slurm Prolog/Epilog 實戰
- [GKE MPS 實作](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nvidia-mps-gpus) — `hostIPC: true` 要求和 Google 自訂 GPU stack
- [SURF MPS for Slurm GitHub](https://github.com/basvandervlies/surf_slurm_mps) — 荷蘭國家超算中心的 MPS Prolog/Epilog 完整實作
- [NVIDIA GPU Operator MPS 文件](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html) — ClusterPolicy ConfigMap `sharing.mps` 設定
- [MIG vs Time-Slicing vs MPS 比較](https://www.kubenatives.com/p/mig-vs-time-slicing-vs-mps-which) — 三種機制的適用場景分析

