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

## 開源的 Slurm-on-Kubernetes 實作方向

This section focuses on *how* different projects bridge **Slurm’s job model** (multi-node allocations, steps, job arrays, fair-share, backfill, accounting) with **Kubernetes’ pod model** (desired state, controllers, autoscaling, service discovery).

### 1) Slinky (slurm-operator + slurm-bridge): “Slurm on K8s” as a suite

**What it is**
- **slurm-operator** manages a Slurm cluster as Kubernetes resources (CRDs) and owns the lifecycle of controller/login/compute components.
- **slurm-bridge** introduces a *Kubernetes-first execution path* where Slurm jobs are represented as Kubernetes objects and are executed as Kubernetes workloads/pods, while preserving a Slurm-facing UX.

**Execution model**
- Two patterns show up in Slinky deployments:
  1) *Static/elastic Slurm nodes as pods* (Slurm sees node objects that correspond to pods).
  2) *“Bridge” mode* where Slurm jobs are translated into Kubernetes jobs/pods (Slurm becomes a front-door + policy engine; Kubernetes becomes the runtime).

**Where it shines**
- Kubernetes-native lifecycle management of Slurm components and predictable day-2 operations (upgrades/rollouts/health).
- A clearer separation between **Slurm as scheduler/policy** and **K8s as runtime** when using slurm-bridge.

**Trade-offs**
- More moving pieces and CRDs; adopting slurm-bridge is a bigger conceptual and operational step than “run slurmd in pods”.

**References**
- Slinky org + repos: https://github.com/SlinkyProject
- AWS reference deployment on EKS: https://aws.amazon.com/blogs/containers/running-slurm-on-amazon-eks-with-slinky/

### 2) Soperator (Nebius): Kubernetes-first Slurm operator with strong “cluster product” features

**What it is**
- A Kubernetes operator that manages Slurm clusters as k8s resources and emphasizes production features (HA/self-healing, isolation boundaries, GPU health checks, accounting integration, etc.).

**Notable design choices**
- **Kubernetes-first** stance with “bring the cluster to desired state continuously”.
- Focus on *platform* features (accounting, GPU checks, secure access, isolation), not only “get Slurm running”.

**Where it shines**
- If you want a more feature-complete platform layer (multi-tenant boundaries, accounting, GPU health automation).

**Trade-offs**
- Larger scope than a minimal elastic worker scaler; higher adoption/maintenance cost, but a lot more features out-of-the-box.

**References**
- Repo: https://github.com/nebius/soperator
- Background article: https://medium.com/nebius-ai/introducing-soperator-the-slurm-operator-for-kubernetes-a72ce73c4d57

### 3) “Run Slurm inside Kubernetes” Helm charts / manifests (no dedicated operator)

**What it is**
- Traditional approach: containerize `slurmctld`/`slurmd`/`munge` and deploy with Helm/YAML.
- Typically relies on headless services + stable DNS, and uses StatefulSets for predictable node identity.

**Where it shines**
- Minimal conceptual overhead; easiest to understand and debug.
- Great for labs, PoCs, or as a base layer to evolve into an operator.

**Trade-offs**
- You own day-2 operations (upgrades, drift remediation), and elasticity requires custom automation.
