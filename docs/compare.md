# Slurm on Kubernetes：方案比較與本專案定位

本文整理幾個較常被提及的「Slurm on Kubernetes」開源路線，對照本專案目前的作法（以一個輕量的 elastic operator 觀察 Slurm queue 狀態，動態調整 worker StatefulSet replicas，並以固定上限的 slurm.conf NodeName 列表承載彈性節點）。

---

## 0. 本專案目前的核心設計（你現在這套）

1) 部署型態  
- Slurm controller（slurmctld）與 login 以 Deployment/StatefulSet 形式跑在 k8s。  
- Slurm worker 以 StatefulSet 跑在 k8s，並由 operator 透過 scale replicas 進行增減。  
- slurm.conf 先宣告 NodeName=slurm-worker-[0..MAX-1]（上限），真正存在的 Pod 才會 Ready + slurmd 註冊；不存在的節點在 slurmctld 端會被標記 DOWN（scaledown），避免干擾排程。

2) 伸縮觸發（elastic）  
- 透過在 login 端提交 sbatch（或你的 e2e trigger job）讓 queue 出現 PENDING，operator 偵測到「資源不足」後把 worker replicas 往上加到 TARGET / 上限。  
- scale-up 後，透過 DNS gate（worker->controller / worker0->workerN）以及 slurmctld reason gate（NO NETWORK ADDRESS 消失）再放行真正的 multi-node smoke job。  
- scale-down（若已實作）通常會以 idle timeout / queue 空閒來縮回 MIN_WORKERS。

3) 優點與限制  
- 優點：改動小、容易理解、和原生 Slurm 觀念一致；對「使用者只要 sbatch」的體驗很接近傳統 HPC。  
- 限制：NodeName 上限必須預先宣告；worker pod 的 DNS / Endpoints 生成與 slurmd 註冊之間存在競態，需要 gate；若 worker 容器/entrypoint 不穩定，容易出現 NO NETWORK ADDRESS 或 CrashLoop 時把整體流程卡住。

---

## 1. Slinky（Slurm Operator + Slurm Bridge）路線

這條路線通常會被視為「想把 Slurm 更深地 k8s 化」的一組解法。

1) slurm-operator（部署/維運面）  
- 提供 CRD/Controller 來管理 Slurm 叢集生命週期（controller、compute、login、可能還有 accounting/DB），並把設定/憑證注入到 pods。  
- 你現在做的是「以 k8s 原生物件（STS/CM/Secret）+ 自己寫 operator」的最小閉環；slurm-operator 更偏「以 CRD 封裝整套 Slurm 平台」，讓叢集的建立/更新/滾動更一致。

2) slurm-bridge（排程整合面）  
- slurm-bridge 的目的不是單純把 Slurm 跑在 k8s 裡，而是讓 Kubernetes 的 scheduler 介面能把 Pod 的排程決策交給 Slurm（或把 Slurm 當成一個 scheduler backend）。  
- 這種路線的價值在於：如果你的工作負載本質上是 k8s Pod（GPU training / batch workloads），你可以保留 k8s 生態（CRD、runtime、網路、observability），但讓 Slurm 決定 allocation/placement。  
- 相對地，你目前目標更像傳統 HPC：「使用者 sbatch」為主，k8s 對使用者是透明的，所以不一定需要 bridge；除非你想讓 k8s 與 Slurm 的工作負載共存/互相轉譯。

3) 跟本專案差異總結  
- 你的 operator 更像 “queue-driven autoscaler”：看到 PENDING 就加 replicas。  
- Slinky 的 operator 更像 “platform operator”：把 Slurm 叢集作為一個 CRD 管理；而 bridge 是把 Slurm 延伸成 k8s scheduler 的一環。  
- 如果你只追求「sbatch -> 自動長出 worker -> 跑完縮回」，你現在這條路徑其實更短、更可控；若要做成「通用平台」，才需要往 slurm-operator / bridge 的抽象層靠攏。

---

## 2. Soperator（常被提及的「Slurm operator」概念）與你現在的關係

Soperator 這個名稱在一些文章/報告中會被用來指「以 operator 方式在 k8s 上維運 Slurm、並支援彈性 compute」的方向，但實務上各家做法差異很大：  
- 有些是「完整 CRD 管理叢集」；  
- 有些只是「針對 compute 做 autoscaling」；  
- 也有人把重點放在「把 Pod 映射為 Slurm node」的 registration/地址解析與安全性（munge）一致性。

就你目前的設計而言，你其實已經做出一個「最小可行的 Soperator 子集」：  
- 只做 autoscale（以 STS replicas 為伸縮單位）  
- 以預先宣告 NodeName 上限來避免需要動態改 slurm.conf  
- 以 gate 消弭 DNS/registration 的競態

---

## 3. 其他常見的開源路線（補充）

1) 「只有 Helm / YAML」的 Slurm-on-k8s  
- 這類方案通常用 Helm chart 或靜態 YAML 把 slurmctld + slurmd 起來，但不處理 autoscaling，也不做更深的 scheduler 整合。  
- 優點：簡單；缺點：伸縮/治理要靠人手或另外接 KEDA/HPA/自製 controller。

2) 「提交 Slurm Job 的 operator」而不是「在 k8s 裡跑 Slurm」  
- 有些專案提供 CRD（例如 SlurmJob）讓使用者在 k8s 端宣告一個 job，controller 幫你去 slurmctld 提交/查詢/回收。  
- 這更像“Slurm as a service”的 API 層，而不是替你在 k8s 裡維運一個完整 Slurm 叢集。  
- 若你的最終 UX 是「使用者只會 sbatch」，這條路線通常不是主軸；但若你想讓 k8s 原生工作流（GitOps/Argo）管理 HPC job，就有價值。

---

## 4. 本專案接下來的建議規劃（核心技術 -> 實作 -> 應用）

下面是以「最小改動、可逐步加強」的路線圖；每一階段都可獨立交付，避免一次做太大。

A) 核心技術改進（可靠性與可觀測性）  
1) DNS/Endpoint 競態治理（已部分完成）  
- 維持你目前的 DNS gate（worker0->workerN、workerN->controller FQDN）。  
- 針對 headless service：確保 readinessProbe 會影響 EndpointSlice 的 ready endpoints（你目前已用 pgrep slurmd/munged 作 probe，是合理基線）。  
- 針對「偶發 NO NETWORK ADDRESS」：保留 slurmctld reason gate；若仍偶發，可加上 worker 啟動時的自我檢測（例如在 slurmd 啟動前先確認 getent controllerFQDN 成功）。

2) Munge 一致性（你之前遇到過 Invalid credential）  
- 確保所有角色（controller/login/worker）使用同一把 munge.key（Secret），並且容器 entrypoint 對 key 的權限/owner 正確（munge:munge, 0400）。  
- 最小化變更：把「把 /slurm-secrets/munge.key 複製到 /etc/munge/munge.key」的動作統一到同一段 entrypoint，並在 munged 啟動前做 checksum/permission assert。

3) Slurm 控制面健壯性  
- 對 slurmctld 加上更明確的 readiness（不只是 pgrep；可加 scontrol ping 的快速檢查，或至少確認 6817 監聽）。  
- 把 controller service 的存取改成「service DNS」而非直接 pod FQDN 也可以降低重啟時的抖動（但會牽涉 slurm.conf 的 SlurmctldHost 設計；如果你需要固定 host name 也可保留 pod FQDN）。

B) 動態 worker（autoscaling）機制完善（上限 MAX=4）  
1) 伸縮策略（建議最小可行版本）  
- scale-up：看到「需要 N nodes」的 job pending 且原因是資源不足/節點不足，就把 replicas 拉到 min(MAX_WORKERS, 需要的節點數)。  
- scale-down：當 queue 空、且所有 worker 在 sinfo/squeue 下 idle 超過一個 idle_timeout（例如 60~180 秒），把 replicas 縮回 MIN_WORKERS。  
- 重要：scale-down 前先在 slurmctld 把要縮掉的節點 drain（DRAIN）或 DOWN，避免砍到正在跑的 step。

2) 上限治理  
- MAX_WORKERS=4 的設計要在 slurm.conf 反映（NodeName=slurm-worker-[0-3]、Partition Nodes=slurm-worker-[0-3]）。  
- operator 也必須硬限制 replicas <= MAX_WORKERS，並且在 scale-up 時以「目前 replicas + delta」或「直接設定目標 replicas」都可以，但要有 backoff，避免頻繁抖動。

3) 伸縮決策依據（逐步加強）  
- V1：只看 squeue（PENDING jobs 的 node count）就做決策。  
- V2：加上 sinfo / scontrol show node 的狀態，避免把壞掉的 worker 算進可用資源；對 CrashLoop 的 worker 做隔離（先降 replicas，再重建）。  
- V3：支援多 partition、或支援不同 NodeSet（例如 cpu worker、gpu worker）以符合實際 DDP 訓練場景。

C) 驗證與使用者體驗（sbatch 即可）  
1) 把 e2e 驗證腳本變成「參數化、非指定 worker 編號」  
- 你現在已經往這方向修了：用 -N__NODES__ 來產生 multi-node job，再以輸出解析確認 distinct hosts。  
- 建議再加：若 distinct hosts < N，dump err 檔與 squeue/scontrol show job 的狀態，縮短 debug 迴圈。

2) 使用者作業範例與文件  
- 提供一個最小 DDP/mpi 例子（即使先用 hostname demo），並明確寫「需要幾個 node、operator 會長出幾個 worker、上限 4」。  
- 把常見故障（NO NETWORK ADDRESS、Munge credential、CrashLoop、DNS）整理成 runbook（含 3-5 條最常用的 kubectl/scontrol 指令）。

---

## 5. 「Endpoints v1 deprecated」需要處理嗎？

你看到的 `Warning: v1 Endpoints is deprecated in v1.33+; use discovery.k8s.io/v1 EndpointSlice`，通常是 kubectl 在讀 `Endpoints` 物件時的提醒。  
- 若你的控制邏輯/腳本沒有依賴 `kubectl get endpoints` 的輸出（而是用 DNS gate / readiness gate），短期不必改。  
- 若你想長期維護並兼容新版本 k8s，建議把任何「讀 Endpoints」的地方改成讀 EndpointSlice（discovery.k8s.io/v1）；但在你目前這個專案階段，它不是阻塞項。

---

## 6. 建議的「下一步」commit 拆分（可直接當 roadmap）

1) docs/runbook.md：整理 NO NETWORK ADDRESS、munge、DNS、CrashLoop 的排查指令與判斷邏輯。  
2) operator：加入 scale-down（idle timeout + drain/down），並把 scale decision 與 MAX_WORKERS=4 綁死。  
3) e2e：把 trigger job 的 -N 與 smoke job 的 -N 都完全參數化（TARGET_WORKERS/SMOKE_NODES），並把驗證輸出與錯誤 dump 做完整。  
4) （可選）支援多種 worker class（cpu/gpu）與 partition mapping，為未來 DDP/GPU 訓練預留擴充點。
