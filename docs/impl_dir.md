# 應用方向評估：以資源分配為核心的 Slurm-on-K8s 系統

## 核心理念

本系統的核心價值不在於「跑哪種 AI 模型」，而在於**如何把有限的 CPU + GPU 硬體資源，盡可能完整地分配與利用**。

k3s + Slurm 的組合解決了一個實際問題：  
> 一台有 CPU 和 GPU 的機器，同時有多種類型的工作要跑，要怎麼自動分配、不浪費、不搶資源？

應用場景是用來**包裝與展示**這套資源管理機制——讓評審能看到系統在真實工作負載下的行為，而不只是空轉的 cluster。

---

## 一、系統解決的核心問題

### 問題描述

一台混合硬體主機（RTX 4070 + RTX 4080 + 多核 CPU）上，同時存在多種工作：

| 工作類型 | 資源需求 | 特性 |
|---------|---------|------|
| 模型推論（inference） | GPU，短時間 | 大量、可並行、latency 敏感 |
| 模型訓練（fine-tune/training） | GPU 獨占，長時間 | 少量、需要整張卡 |
| 資料前處理（preprocessing） | CPU，多核 | 與 GPU job 可並行 |
| 超參數搜尋（HPO） | GPU，多個小 job | 大量小實驗，最適合 MPS |

**沒有好的排程系統時，會發生：**
- 推論 job 開著 GPU 卻大量閒置（utilization < 20%）
- 訓練 job 獨占 GPU 導致其他人等待
- CPU 和 GPU 的工作無法並行，資源交替空轉
- 手動分配，無法彈性擴縮

**本系統的解法：**
- Slurm GRES `--gres=gpu:1` / `--gres=mps:25` 細粒度分配 GPU 資源
- Operator 根據 job queue 深度，動態開關 worker pod
- CPU pool 和 GPU pool 獨立 autoscale，互不干擾
- MPS 讓多個推論 process 共用同一張 GPU，SM 利用率從 20% 提升到 80%+

---

## 二、適合包裝的應用場景

以下三個場景都不需要外部資料，且都能充分展示資源分配機制。

---

### 場景 A：AI 模型服務平台（推薦，最完整）

**概念：** 一個多使用者提交 AI 推論 job 的平台。使用者選擇模型與輸入，系統自動分配 GPU 資源（小任務走 MPS、大任務獨佔整卡），完成後回傳結果。

**工作負載組合：**

```
使用者提交 job
    │
    ├── 文字生成（LLM 7B）     → --gres=gpu:rtx4080:1    獨佔整張 RTX 4080
    ├── 文字分類（小模型）      → --gres=mps:25           RTX 4070 上 MPS 分享
    ├── 圖片生成（SD 1.5）      → --gres=gpu:rtx4070:1    獨佔 RTX 4070
    └── 資料前處理             → --cpus-per-task=4        純 CPU worker
```

**為什麼這個場景好：**
- 不需要自己收集資料（模型從 HuggingFace 下載）
- 同時展示 CPU pool + GPU pool + MPS 三種資源分配路徑
- Operator autoscale 有明確的觸發條件（queue 中有 job → 開 worker）
- 可以量化：GPU utilization、job wait time、throughput

**2024–2026 現實對應：**
- **AWS SageMaker HyperPod**（2024 正式 GA）：本質就是 Slurm on EKS，自動管理 GPU instance 生命周期，與本系統架構高度相似
- **CoreWeave / Lambda Labs 私有雲**：讓研究機構自建 GPU cluster，提供類 SageMaker 體驗
- **學校 AI 研究室共用主機**（台大、成大、陽明交大）：研究生排隊使用 GPU，Slurm 是事實標準

---

### 場景 B：超參數搜尋平台（展示 MPS 最有力）

**概念：** 使用者提交一個模型訓練任務，系統自動展開多組超參數（learning rate、batch size、optimizer），並行跑多個小實驗，找出最佳組合。

**為什麼 MPS 在這裡最關鍵：**

```
傳統方式（無 MPS）：
  實驗 1 → 獨佔 GPU 20 分鐘 → 實驗 2 → 獨佔 GPU 20 分鐘 → ...
  總時間 = N × 20 分鐘

MPS 方式：
  實驗 1、2、3、4 → 各佔 mps:25 → 同時跑 → 每個 5 分鐘跑完
  總時間 ≈ 20 分鐘（N 組實驗並行）
```

**工作負載：**
- 標準 dataset（CIFAR-10、MNIST、HuggingFace datasets）不需要自己收集資料
- 每個實驗是一個獨立 Slurm job，`--array=1-16` 提交 16 個 job
- Operator 依 pending job 數量開啟足夠多的 GPU worker

**2024–2026 現實對應：**
- **Optuna + SLURM**：學術界主流的分散式 HPO 工具鏈，直接整合 Slurm
- **Ray Tune on K8s**：AWS / Google 推薦的雲端 HPO pipeline，本系統是它的 on-premise 版本
- **MLflow + Slurm**：企業 MLOps 常見組合，實驗追蹤 + GPU 排程

---

### 場景 C：分散式訓練實驗平台（展示 DDP）

**概念：** 研究生提交 PyTorch DDP 訓練 job，系統分配多個 GPU worker，透過 NCCL AllReduce 同步梯度，訓練結束後回傳 checkpoint。

**這個場景展示的核心能力：**
- Slurm 跨 pod 分配 `--nodes=2 --ntasks-per-node=1`
- k3s pod 網路作為 NCCL 通道（TCP backend）
- NFS PVC 讓兩個 worker 讀同一份 dataset、寫同一份 checkpoint
- Checkpoint-aware scale-down guard：訓練中不能被 Operator 縮掉

**工作負載：**
```bash
# 使用者只需提交這個 job
#SBATCH --nodes=2
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:1
torchrun --nproc_per_node=1 --nnodes=2 train_ddp.py \
  --dataset cifar10 --model resnet50 --epochs 10
```

**2024–2026 現實對應：**
- **NCHC 台灣杉**：台灣最大 HPC cluster，Slurm + InfiniBand，PyTorch DDP 是主要工作負載
- **Academic computing clusters**（TACC、NERSC）：全球學術界標準 GPU 訓練環境
- **企業私有 MLOps**（台積電、聯發科 AI Lab）：不上雲的大廠自建 GPU 訓練環境

---

## 三、建議的包裝策略

### 主要場景：場景 A（AI 模型服務平台）

**原因：**
1. 同時跑 CPU job + GPU 獨占 job + MPS job，三種資源分配路徑都展現
2. Operator autoscale 的效果最直覺（有 job → worker 出現，沒 job → worker 消失）
3. 不需要收集資料，模型從 HuggingFace 下載即用
4. 未來加多租戶（Slurm Account + QOS）可以直接套在這個場景上

**輔助展示：場景 B（HPO）**

把超參數搜尋作為場景 A 的其中一種 job 類型，展示 MPS 的利用率提升。可以做一個簡單的 benchmark：
- 基準：16 個實驗串行跑，GPU utilization ≈ 20–40%
- MPS：16 個實驗並行跑，GPU utilization ≈ 70–90%
- 這個數字就是論文的核心 contribution 之一

---

## 四、論文貢獻點（Contribution）重新整理

以「資源分配」為核心，貢獻點如下：

### C1：彈性 Operator 設計

現有的 Slurm on K8s 方案（SUNK、Volcano Slurm plugin）大多是靜態部署，不會根據 job queue 自動縮放。本系統實現了：
- Checkpoint-aware scale-down guard（縮容前確認 checkpoint 存在）
- Cooldown annotation 持久化（Operator 重啟不重置冷卻計時器）
- 多 pool 獨立 autoscale（CPU pool 和 GPU pool 互不影響）

### C2：MPS 細粒度 GPU 分配

傳統 K8s GPU 分配是整數（0 或 1 張卡），本系統透過 Slurm GRES `mps:N` 實現百分比分配：
- 一張 RTX 4070（48 SM）最多可以同時跑 4 個 `mps:25` job（各 12 SM）
- 對比「一 job 一卡」，throughput 提升 3–4×（可量化）

### C3：CPU + GPU 混合 pool 資源隔離

不同類型工作負載（preprocessing / inference / training）路由到不同的 pool，互不競爭：
- CPU pool 的 autoscale 不受 GPU job 影響
- GPU pool 的縮容不影響正在跑的 CPU job

### C4（未來）：多租戶 Slurm QOS

在現有 Operator 上加入 Slurm Account + QOS，讓多個使用者有各自的資源配額，不互搶。

---

## 五、可量化的實驗指標

這些指標可以直接寫進論文的 Evaluation 章節：

| 指標 | 測量方式 | 預期結果 |
|------|---------|---------|
| GPU 利用率（MPS vs 無 MPS） | `nvidia-smi dmon` 採樣，Grafana 顯示 | MPS 時 utilization ≈ 70–90%，無 MPS ≈ 20–40% |
| Operator scale-up 延遲 | 從 job 進 queue 到 worker pod Ready | 15–60 秒（受 k3s pod 啟動時間限制） |
| Job throughput（HPO 場景） | 單位時間完成的實驗數 | MPS 下 ≈ 4× 串行方式 |
| DDP 訓練 throughput | samples/sec on 2-GPU vs 1-GPU | 接近線性加速（NCCL TCP 有一定 overhead） |
| Checkpoint guard 有效性 | 在 checkpoint 缺失時強制觸發縮容嘗試 | scale-down 被正確阻止 |

---

## 六、不適合的方向（避免走錯路）

| 方向 | 原因 |
|------|------|
| RAG / 知識庫問答 | 需要大量領域資料建索引，資料收集成本高 |
| 即時推論 API（< 100ms） | Slurm 排隊有 15–30s 延遲，不適合即時場景 |
| 70B+ 大模型推論 | VRAM 不足（只有 28GB 合計），需要 A100 80GB |
| 大規模預訓練 | 需要數十至數百張 GPU，硬體限制 |
| 多租戶 SaaS（現在） | Quota、Network isolation 尚未實作，是 Phase 5 工作 |

---

*最後更新：2026-04-24*
