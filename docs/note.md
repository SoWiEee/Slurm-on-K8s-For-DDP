# Development Notes

1. [採坑紀錄](#debug-record)
2. 開發規劃：[階段五計畫](#phase-5-plan--完成)、[階段六計畫](#phase-6-plan)、[階段七計畫](#phase-7-plan)
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

### 問題 3：PDB 與 StatefulSet 縮容的關係

常見誤解：*認為 PDB 的 `maxUnavailable: 1` 會阻止 operator 把 replicas 從 4 降到 0。

實際行為：
- StatefulSet `replicas` 調整是 **Desired State**，K8s controller 會逐步刪除 Pod（最高優先）
- PDB 保護的是 **Voluntary Disruption**（如 `kubectl drain node`、節點升級）
- operator 調整 replicas = K8s 內部操作，**不受 PDB 約束**
- 結論：PDB 與 drain-then-scale 並不衝突；PDB 保護的是基礎設施層面，drain 保護的是 job 層面


### 問題 4：`MpiDefault=pmi2` 與 `mpi_pmi2.so` plugin 位置

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

### 問題 5：`srun --mpi=pmi2` 在單節點多 task 的行為

確認：`--ntasks=2 --nodes=1` 加上 `srun --mpi=pmi2` 可以在同一個 worker pod 啟動兩個 MPI rank，`$SLURM_PROCID` 分別為 0 和 1。這對容器化 HPC 測試是最低門檻的 MPI 驗證方式，不需要 pod 間網路或 InfiniBand。


### 問題 6：`/etc/profile.d/slurm-modulepath.sh` 在 sbatch 裡不生效

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

### 問題 7：job output 在 worker pod，不在 login pod

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


## 問題 8：NFS Server 的 export 沒涵蓋 LAN 介面 IP

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

## 問題 9：GPU job 卡在 COMPLETING 永不結束

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

## 問題 10：verify-gpu.sh 的 job output 讀不到

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

## 問題 11：verify.sh GPU job 丟到 cpu partition 導致立刻 fail

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

## 問題 12：DRAIN 狀態導致 sbatch 立刻被拒

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

## 問題 13：MPS job 拿不到 `CUDA_MPS_PIPE_DIRECTORY`，CUDA 跳過 MPS 直連 GPU

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

## 問題 14：M7 fragmentation 在 live cluster 上 `requeue_decision` 永遠不觸發

Phase 6 M7 加入的 `FragmentationReconciler` 在 unit test 全綠、daemon thread 也有正常啟動（log 看得到 `fragmentation_started`），但實際在 k3s 上跑 small1 (mps:25 RUNNING) + bigfit (mps:80 PENDING priority 9999) 場景，`requeue_decision` JSON log 一直不出現 — operator 只持續輸出 `loop_observation`，fragmentation 那條 thread 形同沒在動。

**根因（三層）**

`operator/fragmentation.py` 的 `nodes_from_slurm_rest` adapter 從 slurmrestd 解析節點 MPS 容量的方式跟 live cluster 真實的欄位 schema 對不起來，導致 detector 把 fragmentation 當成沒發生：

1. **`gres_used` 不是 slot 數，是裝置數。**  unit test fixture 用的是簡化字串 `"mps:25"`，能被 `mps[:=](\d+)` regex 命中；但 slurmrestd 實際輸出是 `mps:rtx4070:1(IDX:0)` —「rtx4070 type 的 mps 裝置 1 顆，index 0」，那個 `1` 跟 slot 數完全無關。canonical 的 slot 配置實際上躺在 `tres_used` 欄位裡，格式為 `cpu=4,gres/mps=50`。adapter 一直讀錯欄位，於是把已經被佔掉 25 slot 的節點當成 100 全 free。
2. **CPU-only 節點被誤判為「能塞下 MPS job」。** detector 的 `_fits_anywhere` 是把所有節點當同質池檢查；`slurm-worker-cpu-0` 完全沒有 mps gres，但 adapter 預設 `total_mps=mps_per_node=100` + `used=0` → free=100，造成 bigfit「在 cpu node 上可以塞」→ blocked 為空 → no-fragmentation。
3. **DRAIN/DOWN 節點先前未被排除。** node-1 被 drain 起來當作「永遠塞不下 bigfit」的條件，但原本的 adapter 沒看 `state`，照樣回報 free=100，等於 bigfit 永遠「可在 node-1 上跑」，又回到 no-fragmentation。

**修法**（`operator/fragmentation.py`）

新增三個 helper，把 adapter 換成從正確欄位取資料：

```python
_UNAVAILABLE_NODE_STATES = frozenset({
    "drain","drained","draining","down","fail","failing","maint",
    "reserved","powering_down","powered_down","future",
    "not_responding","no_respond",
})

def _node_is_available(node):  # state 可能是 list / "DOWN+NOT_RESPONDING" / "ALLOCATED"
    raw = node.get("state","")
    states = ([str(s).lower() for s in raw] if isinstance(raw, list)
              else [s.strip().lower() for s in str(raw).replace("+", ",").split(",") if s.strip()])
    return not any(s in _UNAVAILABLE_NODE_STATES for s in states)

def _parse_tres_mps(tres_used):       # "cpu=4,gres/mps=50" → 50
    m = re.search(r"gres/mps=(\d+)", tres_used or "")
    return int(m.group(1)) if m else None

def _parse_node_total_mps(gres):      # "gpu:rtx4070:1,mps:rtx4070:100" → 100
    m = re.search(r"(?:^|,)\s*mps(?::[A-Za-z0-9_-]+)?:(\d+)", gres or "")
    return int(m.group(1)) if m else None
```

`nodes_from_slurm_rest` 改為：

- `total_mps` 優先讀節點配置 `gres`；找不到 mps token 就回 0（CPU-only 節點 → 0/0），只有完全沒有 `gres` key 的舊 fixture 才 fallback 到 `mps_per_node` 預設值。
- `used` 優先讀 `tres_used` 的 `gres/mps=N`；fallback 才看 `gres_used`，維持舊 unit test 的相容性。
- `_node_is_available(node)==False` 時直接把 `free=0`，確保 drained / down 節點不會被當作可放置目標。

新增三條 unit test 把這三條路徑都鎖住（`test_unavailable_nodes_have_free_mps_zeroed`、`test_tres_used_is_preferred_over_gres_used`、`test_cpu_only_node_without_mps_gres_reports_zero_capacity`），現在 19/19 綠。

**Live 驗證**

修完 rebuild 進 k3s containerd → 重啟 operator → 維持原情境（small1 prio 1039 mps:25 RUNNING on gpu-rtx4070-0、bigfit prio 9999 mps:80 PENDING、gpu-rtx4070-1 drained），每 15 秒 fragmentation tick 都吐：

```
event_type=requeue_decision  score=1.0  blocked=["86"]
reason="unblock 86 (priority 9999, mps_req 80) on slurm-worker-gpu-rtx4070-0:
        requeue 1 job(s) freeing ~25 slots"
shadow=true  target_jobs=["85"]  unblocks=["86"]  requeued_jobs=[]
```

`shadow=true` 因為 `FRAGMENTATION_SHADOW_MODE=true`，actuator 沒被叫到，`requeued_jobs=[]` 對應正確；plumbing 全通，等切到非 shadow 模式就會發 `scontrol requeue 85`。

**踩坑插曲（live debug 過程）**

1. 一開始把 gpu-rtx4070 max_replicas 留 2，operator 看到高優先級 PENDING 就把 pool 從 1 sclae 到 2，slurmd 一註冊 node-1 就清掉 drain marker，bigfit 直接跑到 node-1 上，根本沒有 fragmentation 可偵測。要把 `PARTITIONS_JSON` 裡 gpu-rtx4070.max_replicas 改成 1，operator 才不會跟我們搶 node-1。
2. 期間因為反覆 down/up node，slurmctld 的 AllocTRES 卡在 `gres/mps=100` (zombie state)，新 sbatch 一直 `Reason=Resources` 起不來。`scontrol reconfigure` 一次就沖掉。
3. 該節點記憶體只有 3500Mi → 一個 small job 就吃滿全部 RealMemory，原計畫的「兩個 small + 一個 bigfit」其實只能塞一個 small；但 fragmentation detector 是看 MPS slot 不看 memory，所以「一個 small (25 slot) + 一個 bigfit (80 slot)」一樣形成 fragmentation（free=75 < 80），場景照樣成立。

---

# Phase 5 Plan ✅ 完成

## 問題 15：M8 evaluation 主結論被「JCT 重設 bug」與「單一 seed」放大成假象

**症狀（2026-05-11）：**
M8 完成時主表寫 E5（M7 fragmentation）mean JCT = 2.621h，vs E2 multifactor 改善 28.6%。看起來是強烈正結果，準備寫進 thesis。實際上這個數字是兩個方法論問題疊在一起的 artefact。

**根因：**
1. `sim/runner.py::try_fragmentation_reconcile` 把 victim requeue 時做了 `metrics.records[jid].submit_ts = now`。Victim 的 JCT 公式是 `end_ts - submit_ts`，這行讓 JCT 只計算「最後一次重排隊到完成」的時間，**完全沒算原本排隊與重做的成本**。M7 越激進、被踢的 job 越多，這個低估就越大 — E5 的 1856 次 requeue 等於把 ~19% 的 jobs 的 JCT 重設過。
2. E1..E5 各只跑一次 deterministic Philly subsample，沒有任何 cross-seed variance estimate。無法分辨「真改善」vs「sample artefact」。
3. Sim 不收 checkpoint reload cost，1856 次 requeue 完全免費。

**修法：**
1. `try_fragmentation_reconcile` 刪掉那行 `submit_ts = now`（保留原始 submit_ts），只重設 `start_ts/end_ts`。`vj` 給 scheduler 看的 submit_ts 仍是 `now`（fairness 應該如此），但 metrics 看的是原始 submit。
2. 新增 `--ckpt-reload-cost`（default 60s，僅 fragmentation 模式生效）；用 `cost_pending` dict 在下次 allocate 時把成本加進 end_ts。`metrics.requeue_cost_total` 報告整體開銷。
3. `eval/scripts/run_all.sh` 改用 `SEEDS="42..46"` 跑 5 個 synthetic Philly-like traces（同 generator 不同 seed，per-seed 1000 jobs）。
4. 新增 `eval/scripts/aggregate_seeds.py` 計算 mean ± std + Student-t 95% CI；`print_summary.py` 加 paired same-seed diff（CI 比 unpaired 緊一個量級）。

**修正後的真結果：**
| 比較 | Δ mean JCT | 95% CI | 顯著性 |
|---|---:|---:|---|
| E4 (predictor) vs E2 (vendor) | −20.1% | ±11.95% | ✅ significant |
| E5 (M7) vs E4 (predictor) | **+33.1%** | ±5.22% | ❌ regression（significant） |
| E5 vs E2 | +6.3% | ±15.42% | not significant |
| E5b (no ckpt cost) vs E5 | −5.8% | ±8.83% | ckpt cost 僅佔 ~6% |

**踩坑插曲：**
- 第一次跑 SEEDS="42 43" SYNTH_JOBS=200 smoke 結果所有 exp 完全相同 — 因為 200 jobs × 16 GPU × 100 MPS = 1600 slots，util 只有 0.20，沒有 contention 讓 scheduler 表現差異。換回 1000 jobs 後立即看到 paired CIs。
- E5b 是新增的 control：原本只想用來算 ckpt cost 占比，意外發現「即使 ckpt cost = 0，M7 仍比 E4 差 22%」— 證明問題本質不在 reload cost 而在 lost progress（victim 整段重跑）。這給 future work 一個明確方向：改 preempt+suspend，保留 GPU memory state。
- 寫進 paired-diff 的時候因 Student-t 表只到 n=10，n=5 用 2.776；後面如果加到 10 seeds 要記得查表或直接用 scipy。

## 問題 16：E7 live cluster 兩 pass 之間，叢集會卡進無法自己復原的死結

**症狀（2026-05-11）：**
E7 要在實機跑 vendor (multifactor) vs our (M3 score) 兩 pass。每次切換 `slurm.jobSubmit.enabled` 都要 `helm upgrade`、controller 會 rolling restart。重啟的副作用拖到後續 pass：worker 拿不到 slurmctld 心跳、in-flight job 卡 COMPLETING 不釋放、operator 看到 ghost job 不肯 scale up、squeue 全部 PENDING 寫著 `ReqNodeNotAvail, UnavailableNodes:slurm-worker-gpu-rtx4070-[0-1]`。整個 cluster 進入「pod 都沒了但 Slurm 認為還有 job 在跑」的死結，drain 永遠等不到 0。

這不是單一 bug，是好幾個元件的設計交互。本節按照「踩到順序 + 修法 + 哪一層的責任」記錄。

### 16.1 helm post-upgrade hook 失敗，整次 upgrade 被當失敗

- 觸發：`helm upgrade slurm-platform ...` 任何一次。
- 表象：`Error: UPGRADE FAILED: post-upgrade hooks failed: job slurm-platform-gpu-labeler failed: BackoffLimitExceeded`。chart 本體的資源其實 *已經* 套上去了（controller 真的有重啟、values 真的有變），但 helm 把 release 標 FAILED，下次 `helm get values` 跟 status 都不可信。
- 根因：gpu-labeler 是 one-shot job，只在「第一次 install」需要跑（給 GPU 節點貼 `nvidia.com/device-plugin.config` label），它的 RBAC 用 `kubectl label` 但有時 backoff retry 限制太小。每次 upgrade 都重跑 = 每次都可能撞 backoff。
- 修法：`helm upgrade --no-hooks ...`。GPU label 已經貼好了，不需要重跑。
- 責任層：chart。長線可以把這個 hook 改成 `helm.sh/hook: post-install` 而不是 `post-upgrade`，或加 idempotency guard。

### 16.2 controller restart → workers NOT_RESPONDING → in-flight jobs 卡 COMPLETING

- 觸發：`kubectl rollout restart sts/slurm-controller` 或 helm upgrade 觸發的 controller 重建。
- 表象：sinfo 看到 `slurm-worker-gpu-rtx4070-0 idle*` 或 `completing*`，`*` 是 NOT_RESPONDING。`scontrol show node` 上 `State=IDLE+COMPLETING+NOT_RESPONDING`。已經跑到一半的 job 永遠停在 COMPLETING、squeue 不會掉。
- 根因：slurmctld 重啟後 in-memory state 重建，但 slurmd 還停留在「上一個 controller 連線」。心跳 timeout 後 controller 把 node 標 NOT_RESPONDING，但 in-flight job 的 epilog 還沒被確認，於是 job 永遠 COMPLETING。
- 修法 A（短期）：`scontrol update NodeName=... State=DOWN` 再 `State=RESUME`，強制 controller 把 node 狀態重置，這會清掉 stuck completing。
- 修法 B（更徹底）：直接 `kubectl delete pod slurm-controller-0`，讓 slurmctld 從 state save 檔重建。這個動作意外地比上面溫和——重建後 controller 會 re-read state、跟 slurmd 重新 handshake，stuck COMPLETING 全部清掉。
- 責任層：Slurm 21.08 / 部署設計。Slurm 本來預期 slurmctld 重啟是稀有事件；K8s 把它變家常便飯，但 chart 沒處理過渡期。

### 16.3 Operator scale-down 把 ghost job 當 running，永遠 keep replicas=0

- 觸發：上面 16.2 的 stuck COMPLETING job 持續存在。
- 表象：operator log 重複出現
  `"event_type": "scale_skipped", "from_replicas": 0, "to_replicas": 0, "reason": "no_pending_jobs", "pending_jobs": 0, "running_jobs": 1`
  Job 1 是 ghost、pod 已經沒了，但 operator 從 slurmrestd 看到的 job state 仍是 RUNNING / COMPLETING。它的 policy 是「只看 pending」、看到 0 pending 就不 scale up。於是新提交的 job 都 PENDING (ReqNodeNotAvail)、又因為 0 pending（前面那 1 個 ghost running）所以 scale up 不會發生。死結。
- 修法：人手做 16.2 修法 B（delete controller pod）清掉 ghost job 之後，operator 才會看到正確狀態。
- 責任層：operator policy。建議加一個 detector：「running_jobs > 0 但 sts replicas = 0 且 pod = 0」是不一致狀態，要 escalate（例如 log warning + 強制 scale up 至少 1 個 pod 來吸收 ghost 或讓 epilog 跑完）。

### 16.4 AllocTRES zombie：MPS slot 沒被釋放

- 觸發：一個 mps:100 job 結束（或被 scancel）但 slurmd 還沒對 controller 報完 epilog 就被中斷。
- 表象：`scontrol show node slurm-worker-gpu-rtx4070-1` 顯示 `State=IDLE` 但 `AllocTRES=gres/mps=50`。下一個需要 mps:100 的 job 看到 50 slot 已分配、顯示 `Pending (Resources)`，但實際上沒有任何 job 在跑。
- 修法：`scontrol reconfigure` 會強制 controller 重新從 slurmd 收集 TRES，AllocTRES 清空、blocked job 立刻 dispatch。**注意 reconfigure 本身會 timeout（`slurm_reconfigure error: Socket timed out`）但其實有生效，可以忽略錯誤訊息。**
- 責任層：Slurm 21.08 bug。GRES（特別是 MPS）的 accounting 跟 job state machine 沒完全同步，在快速 churn 或 slurmd 不穩時會殘留。21.08 之後（22.05+）的 release notes 有提到類似 fix，升級可以解決。

### 16.5 sacct 從 login pod 連 slurmdbd 失敗、但 controller pod OK

- 表象：`kubectl -n slurm exec deploy/slurm-login -- sacct ...` 回 `slurm_persist_conn_open_without_init: failed to open persistent connection to host:slurmdbd.slurm.svc.cluster.local:6819: Connection refused`。同時間從 controller pod 跑 sacct 完全正常。
- 根因：slurmdbd 重啟後（之前的 controller cascade restart 觸發），login pod 的 munge auth socket 跟 slurmdbd 之間的 persistent connection 失效，但 sacct 沒重連邏輯，直接報錯。controller pod 因為 munge 跟 slurmctld 共享 socket，路徑不同所以沒受影響。
- 修法：所有 sacct dump 改從 controller pod 跑（已 patch 進 `eval/scripts/e7_one_pass.sh::dump_sacct`）。
- 責任層：slurm 21.08 + chart 部署。可能是 NetworkPolicy 沒對 login pod 完整放行 slurmdbd，或 munge ConfigMap mount 在 login 跟 controller 上有微差。沒深追。

### 16.6 GPU pool 兩個 worker pod 共享同一塊實體卡

- 表象：sts `slurm-worker-gpu-rtx4070` replicas = 2，但實機只有 1 張 RTX 4070。兩個 pod 都拿到 `nvidia.com/gpu: 1` resource、都 1/1 Running。
- 根因：NVIDIA device plugin 配 `replicas: 2` 的 time-slicing，把一塊實體 GPU 切成 2 個 logical device 給 K8s 排程。Slurm 看到 2 個 GPU node、配置 200 mps slot；實際上兩個 pod 共用 SM、context-switch 由 NVIDIA driver 負責。
- 影響：一個 pod 跑爆 GPU 時，另一個 pod 的 job 也會變慢（共享 SM）。Slurm 排程把 200 slot 當獨立資源 schedule，但實際吞吐量是 100 slot 等級。對 E7 的影響：vendor pass 跟 our pass 的 wall-clock 都受同樣的共享 cost 影響，paired diff 還算 fair，但絕對吞吐量比 sim 預測的差一倍。
- 修法：沒修。這是 chart `values-k3s.yaml::pools.gpu-rtx4070.replicas: 2` + device plugin time-slicing 設計給的能力。要拿到乾淨的單卡 baseline 得改 `replicas: 1`。

### 16.7 死結復原 SOP（給下次踩到的人）

按順序試，能停就停：

1. `sudo kubectl -n slurm exec deploy/slurm-login -- scancel --user=root`（清掉所有 pending）
2. `sudo kubectl -n slurm exec slurm-controller-0 -- scontrol update NodeName=slurm-worker-gpu-rtx4070-[0-1] State=DOWN Reason=reset; sleep 2; scontrol update NodeName=... State=RESUME`
3. 還沒救：`sudo kubectl -n slurm delete pod slurm-controller-0`（slurmctld 重啟，stuck COMPLETING 會清掉）
4. 等 controller Ready、`sinfo` 不再出現 `*` 跟 `down/drain` 才能送新 job
5. 送 job 後檢查 `scontrol show node ... | grep AllocTRES`，若有殘留 mps slot 但實際沒 job 在跑，`scontrol reconfigure`（忽略 timeout 訊息）

### 16.8 對 operator 設計的建議

E7 兩次撞死結之後，我認為 operator 有一個值得加的功能：**inconsistency detector**。loop 內如果同時看到：
- `current_replicas == 0`（pod 不存在）
- `running_jobs > 0`（slurmrestd 報有 job 在跑）

代表那些 running job 是 ghost，應該 emit 一個 `ghost_jobs_detected` warning event。可以選擇 (a) escalate 給 human、(b) 強制 scale up 1 個 pod 來吸收 epilog 收尾、(c) 自動觸發 `scontrol update State=DOWN/RESUME`。

這不在當前 Phase 6 scope，但留作 future operator hardening 的方向。

---

> **狀態（2026-05-05）：** Phase 5 收斂為 **Lmod + Helm chart cutover** 兩件事，皆已完成並上線。原計畫中的「工作負載模板（5-C）」從 Roadmap 移除（使用者目前用既有 verify 腳本 + `docs/cluster.md` 範例足以上手）；原 5-B（OpenTelemetry）與 5-D（SSH Login）改編到 Phase 7。
>
> 本節以下保留 5-A 的設計與決策記錄，作為 chart 結構的歷史脈絡；活躍的下一階段請看 [`# Phase 6 Plan`](#phase-6-plan) 與 [`# Phase 7 Plan`](#phase-7-plan)。

---

## 5-A：Helm Chart 封裝

### 設計方向

**Monolithic chart for slurm 子系統 + NVIDIA GPU Operator 獨立安裝 + slurm.conf 拆兩個 ConfigMap。**

- 主體不拆 subchart；monitoring / storage / gpu 用 `enabled` flag 控制。
- `render-core.py` 廢棄，`slurm.conf` / `gres.conf` 改由 `_helpers.tpl` 從 `values.yaml` 的 `pools` 列表產生。
- **GPU 子系統用 NVIDIA GPU Operator，但獨立安裝（不是 chart dependency）**——見上面修訂版 3 banner。本 chart 只在 `gpu-operator` namespace 放 device-plugin-config ConfigMap + 一個 cluster-wide 的 node-labeler Job 把 GPU 節點標上 `nvidia.com/device-plugin.config=<key>`。GPU Operator 由 `scripts/install-gpu-operator.sh` 用 `helm install gpu-operator nvidia/gpu-operator -n gpu-operator --create-namespace --set driver.enabled=false --set toolkit.enabled=false` 裝進自己的 PSS=privileged namespace。Operator 內建的 `mps-control-daemon` DaemonSet 解決 v0.15–v0.17.x device-plugin 內建 MPS 的 spawn race。
- **GPU Operator 的 driver / toolkit 子模組關掉**：host 已用 `apt install nvidia-driver-535` + `nvidia-container-toolkit` 裝好，重複裝會撞。Operator 只負責 device-plugin、MPS daemon、DCGM exporter（可選）、gpu-feature-discovery、node-feature-discovery。
- 自寫的 `manifests/gpu/nvidia-device-plugin.yaml` + `manifests/gpu/mps-daemonset.yaml` 廢棄。
- `slurm.conf` ConfigMap 拆成 **`slurm-config-static`**（ClusterName / Auth / Plugin / AccountingStorageTRES，幾乎不變）+ **`slurm-config-nodes`**（NodeName / PartitionName，每次 pool 變動都重產）。worker 只 mount 後者 → 改一個 pool 的 `maxReplicas` 不會 rolling restart 全部 worker。
- secret（munge.key / slurm-jwt-key）**不由 chart 產生**，install 前要先跑 `scripts/deploy-1.sh`（chart 用 `helm.sh/hook-pre-install` 檢查存在性即可）。

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

> [!WARNING]
> GPU Operator 預設會把 driver / toolkit / DCGM exporter 一起裝，跟 host 已裝的 `nvidia-driver-535` + `nvidia-container-toolkit` 衝突——`install-gpu-operator.sh` 用 `--set driver.enabled=false --set toolkit.enabled=false` 把這兩個子模組關掉。



### values overlay 策略（移除 `mps.enabled`）

| 檔案 | 用途 | 關鍵差異 |
|------|------|---------|
| `values.yaml` | 基準（Kind 開發） | `runtime: kind`、`gpu.enabled: false`、`slurm.taskPlugin.kind: task/none`、`storage/monitoring: enabled` 看情況 |
| `values-k3s.yaml` | Linux + 真實 GPU + MPS | `runtime: k3s`、`gpu.enabled: true`、`gpu.autoLabel: true` |
| `values-dev.yaml` | CI / 無 GPU 環境 | `gpu.enabled: false`、`monitoring.enabled: false`、`storage.enabled: false`、`pools` 只留 cpu |

---

# Phase 7 Plan

> **狀態：** 📋 規劃中。Phase 6  完成一定階段後，Phase 7 回到使用者體驗與端到端可觀測性。
>
> 開發順序：**OpenTelemetry（7-A）→ SSH Login（7-B）**

---

## 7-A：OpenTelemetry 分散式追蹤

### 為什麼 metrics 不夠
Prometheus 告訴你「現在 p95 provisioning latency 是 45 秒」，但不告訴你：
- 這 45 秒是花在 K8s 排程（pending pod）、image pull、還是 Slurm node registration？
- 是某個特定 job 特別慢，還是系統性問題？

OpenTelemetry trace 回答的是「**這一次** job J42 為什麼比較慢」。

### Trace 結構設計

```
TraceID: job-{SLURM_JOB_ID}
│
├── [Span] job_submit          ← serve.py /decide 建立，寫入 admin_comment
│     attributes: job_id, partition, gres, requested_cpus
│
├── [Span] queue_wait          ← Operator 從 admin_comment 讀取 trace context 後 continue
│     attributes: pending_jobs_at_submit, pool
│
├── [Span] scale_up_decision   ← Operator _do_scale_up()
│     attributes: from_replicas, to_replicas, reason
│
├── [Span] k8s_provisioning    ← Operator 現有 _provisioning dict 轉為 span
│     attributes: pool, target_replicas
│     → 已有資料來源：slurm_operator_provisioning_latency_seconds histogram
│
├── [Span] slurm_node_registration
│     attributes: node_name, registered_at
│
├── [Span] job_running         ← Operator 偵測 job state → RUNNING
│     attributes: nodes, cpus, gres
│
└── [Span] checkpoint_write    ← fine-tuning job 專用，SIGTERM handler 觸發
      attributes: checkpoint_path, file_size_bytes
```

### Trace Context 傳播設計（方案 B：admin_comment 作為 carrier）

serve.py 與 Operator 之間沒有同步呼叫，需要一個帶外（out-of-band）管道傳遞 trace context。選用 Slurm job 的 `admin_comment` 欄位作為 carrier：

```
[sbatch 提交]
  → serve.py /decide 被 Lua hook 呼叫
      1. 建立 root span: job_submit
      2. 將 W3C traceparent header 序列化：
         traceparent = "00-{trace_id}-{span_id}-01"
      3. scontrol update JobId={job_id} AdminComment="otel={traceparent}"
      4. job_submit span 結束（或保持 open 到 queue_wait 開始）

[Operator polling loop]
  → 首次從 squeue 看到 job_id 時：
      1. 呼叫 slurmrestd GET /slurm/v0.0.40/job/{job_id} 取得 admin_comment
      2. 解析 "otel={traceparent}" → 還原 trace_id + parent_span_id
      3. 用還原的 context 建立 queue_wait span（child of job_submit）
      4. 後續所有 span（scale_up_decision、k8s_provisioning、job_running）
         都在同一個 trace 下繼續
```

**前提條件：**
- serve.py 需要 `scontrol` 執行權限（同 slurmctld 網路可達，已有 SLURM_JWT_TOKEN）
- Operator 的 slurmrestd client（`slurm.py`）已有 `get_job()` 方法，可直接讀 `admin_comment`
- serve.py 不被呼叫時（score fallback）：Operator 偵測不到 admin_comment，自行建立新 trace（`job_id` 當 trace root），鏈路從 queue_wait 開始

### 實作步驟

1. **serve.py 加 OTel SDK**：
   - `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc`
   - 建立 `TracerProvider`，export 到 OTel Collector（`OTEL_EXPORTER_OTLP_ENDPOINT` env var）
   - `/decide` handler：建立 `job_submit` span → `scontrol update AdminComment`

2. **Operator 加 OTel SDK**（`operator/app.py`）：
   - 首次看到 job 時讀 `admin_comment`，嘗試 extract trace context
   - `_do_scale_up()` 建立 `scale_up_decision` span
   - `_provisioning` dict 已追蹤 start/end → 直接轉為 `k8s_provisioning` span
   - `slurm.py` 的 slurmrestd 呼叫統一包在 child span

3. **OTel Collector**（`chart/templates/monitoring/otel-collector.yaml`）：
   ```yaml
   receivers:
     otlp:
       protocols:
         grpc: { endpoint: "0.0.0.0:4317" }
         http: { endpoint: "0.0.0.0:4318" }
   exporters:
     otlp:
       endpoint: "tempo:4317"
       tls: { insecure: true }
   service:
     pipelines:
       traces:
         receivers: [otlp]
         exporters: [otlp]
   ```

4. **Grafana Tempo**（`chart/templates/monitoring/tempo.yaml`）：
   - 單節點部署，port 4317（OTLP gRPC）+ 3200（query）
   - Grafana datasource 加 `exemplarTraceIdDestinations`

5. **Prometheus Exemplar 連結**：
   - `slurm_operator_provisioning_latency_seconds` histogram 在記錄時附帶 `{TraceID: "xxx"}`
   - Grafana 面板 provisioning latency p95 spike → 點擊 → 跳到對應 Tempo trace

### Exemplar 連結（差異化觀測點）
在 Provisioning Latency p95 圖上，spike 對應的那個 TraceID 可以直接點進去看整條鏈。這是目前所有 Slurm-on-K8s 開源方案（SUNK、Slinky、Slonk）都沒有做到的端到端觀測視角。

---

## 7-B：SSH Login

### 問題
目前進入 login node 需要 `kubectl exec -it deploy/slurm-login -- bash`，使用者必須安裝 kubectl 並持有 kubeconfig，這不是「共用 AI 計算平台」應有的使用體驗。

### 目標
```
ssh -p 30022 user@<k3s-host-ip>
       ↓
NodePort :30022 → slurm-login pod
                    ├── sbatch / squeue / sinfo（Slurm 指令即開即用）
                    └── /shared/（NFS 掛載，模型 + 輸出共用）
```

### 實作路徑
- `docker/login/Dockerfile` 加入 `openssh-server`，SSH key 認證（禁用密碼登入）。
- `chart/templates/login.yaml` 的 Service 改為 `NodePort`，固定 port 2222。
- Login 容器啟動腳本加入 SSH host key 初始化（`ssh-keygen -A`）。
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
slurmdbd 搭配 MySQL 後端，把每個 job 的 CPU-hours、使用者、帳戶等資訊持久化。`sacct` 可以查詢歷史 job 統計，也是後期 Fair-Share 多租戶排程的前置條件。

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
