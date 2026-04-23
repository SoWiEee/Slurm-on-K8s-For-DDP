# 應用方向評估：Slurm-on-K8s 基礎設施的實際用途

本文件評估目前已建成的基礎設施（Slurm on Kubernetes + 彈性 Operator + Monitoring + CPU + RTX 5070 + RTX 4080 + MPS + k3s）能夠支援哪些應用，以及對應到 2024–2026 的哪些現實場景。

---

## 一、目前硬體能力速覽

| 資源 | 規格 | 限制 |
|------|------|------|
| RTX 5070（Blackwell GB203） | 12 GB GDDR7、48 SM、MPS ✅、MIG ❌ | 不適合 70B+ FP16 模型 |
| RTX 4080（Ada Lovelace AD103） | 16 GB GDDR6X、76 SM、MPS ✅、MIG ❌ | 不適合 70B FP16，70B Q4 勉強可行 |
| 雙 GPU 合計 VRAM | 28 GB | 可跑 30B Q4 或 2x8B FP16 DDP |
| Slurm MPS | `mps:25` ~ `mps:100`（0–100% SM） | 低並行數時 overhead 增加 |
| k3s + Linux | 直接存取 `/dev/nvidia*` | 無 WSL2 限制 |
| 彈性 Operator | CPU / GPU 兩個 pool，獨立 autoscale | 延遲約 15–30s（輪詢間隔） |
| Prometheus + Grafana | 叢集 + 自訂 Slurm metrics | 已有即時 job queue 監控 |

**底線：** 這套系統最適合「中小規模、多使用者、注重 GPU 利用率」的場景——而非需要整塊 A100/H100 的大模型預訓練。

---

## 二、可支援的應用類型

### 應用 A：私有 LLM 推論服務（Private LLM Inference）

**適合什麼：** 企業或研究室將已有模型（Llama、Mistral、Qwen）部署為 API，對外不暴露資料。

**如何跑：**
- RTX 4080（16 GB）跑 Llama 3.1 8B FP16 或 Mistral 7B FP16，完整佔用一張卡。
- 用 MPS 讓 3–4 個推論 worker 共用 RTX 5070，處理較低延遲需求的請求。
- Slurm job = 一個 `vllm serve` 或 `ollama` 實例，由 Operator 依 queue 長度動態開關 GPU worker。

**對應現實案例（2024–2026）：**
- **Bloomberg、摩根士丹利**：私有 GPT 模型，金融文件摘要，不外送資料到 OpenAI API。
- **醫院 RAG**：病歷摘要 + 藥物交互查詢，法規要求資料不出院內網。
- **學術機構 LLM Gateway**：多研究室共用一台 GPU 主機，用 Slurm 排隊避免搶資源。

**跟本系統的連結：**
| 基礎設施功能 | 如何服務這個應用 |
|---|---|
| Operator autoscale | Queue 中有推論 job → 自動開 GPU worker |
| MPS `mps:25` | 4 個 7B 推論 process 同時跑在 RTX 5070 |
| Slurm `--constraint` | 大模型指定 RTX 4080（16 GB）；小模型用 RTX 5070 |
| Grafana | 即時顯示 GPU 利用率、pending job 數量 |

---

### 應用 B：Embedding / RAG 服務（向量化 + 檢索增強生成）

**適合什麼：** 把企業/學校文件轉成向量，讓 LLM 回答「基於你們自己資料」的問題。

**如何跑：**
- Embedding 模型（`bge-m3`、`text-embedding-3-small` 替代品）很小（<1 GB），適合 MPS 多工。
- 一張 RTX 5070 可同時跑 4–8 個 embedding worker，各佔 `mps:12`（約 6 SM）。
- LLM 生成部分（Reranker + Generator）排 Slurm job 動態分配到 RTX 4080。
- Vector DB（Qdrant / Milvus）可跑在 CPU worker pod 中。

**完整 RAG Pipeline：**
```
[文件上傳] → Slurm CPU job (chunking) → Slurm GPU job (embedding, MPS)
                                                ↓
                                      [Qdrant Vector DB, PVC]
                                                ↓
[使用者問題] → embedding → Qdrant 搜尋 top-k → Slurm GPU job (rerank + LLM 生成)
                                                ↓
                                           [回答回傳]
```

**對應現實案例（2024–2026）：**
- **Notion AI、Confluence AI**：把公司知識庫接上 LLM，員工問問題直接得到來源連結。
- **法律科技**：台灣法院判決書全文 RAG，律師所節省閱讀時間。
- **學術 Paper QA**：把 arXiv PDF 建成 vector store，研究生問「這個領域最近的 benchmark 是什麼」。

**跟本系統的連結：**
| 基礎設施功能 | 如何服務這個應用 |
|---|---|
| Operator autoscale | 有 embedding batch job → 開 GPU worker；空閒 → 縮回 0 |
| MPS `mps:12` | 多個 embedding request 並行，RTX 5070 SM 不浪費 |
| NFS PVC（shared storage） | Vector DB 資料持久化，多個 job 讀同一份索引 |
| CPU worker pool | Chunking / preprocessing 純 CPU，不佔 GPU |
| Prometheus alert | GPU 滿載時發 alert，可自動擴 embedding worker |

---

### 應用 C：分散式訓練實驗平台（Multi-GPU DDP）

**適合什麼：** 研究生跑 fine-tuning 實驗，用真實 2-GPU DDP 驗證訓練收斂性。

**如何跑：**
- 2 個 GPU worker（各持有 1 張 GPU），透過 Slurm `--nodes=2 --ntasks-per-node=1` 分配。
- PyTorch `torchrun` 用 NCCL over TCP（k3s pod 網路），做 AllReduce 梯度同步。
- 適合的模型大小：7B–13B（每卡放 FP16，不做 tensor parallel）。

**RTX 5070 + RTX 4080 混合訓練的現實：**
- DDP 速度取決於最慢的卡（RTX 5070 bottleneck，VRAM 較小）。
- 適合實驗性 fine-tuning（LoRA、QLoRA），不適合大規模預訓練。
- 對論文展示「我有做 DDP 實驗」是足夠的。

**對應現實案例（2024–2026）：**
- **大學 GPU Cluster**（如成大、台大 AI 中心）：學生共用 GPU 資源，排隊跑實驗。
- **Hugging Face AutoTrain**：雲端服務讓沒有 ML 背景的人也能 fine-tune，本系統是它的 on-premise 版本。
- **Corporate MLOps**（台積電、聯發科研究部門）：不想把 training data 送上 AWS 的情境。

---

### 應用 D：多使用者 GPU 資源共享（學術實驗室）

**適合什麼：** 多個研究生共用一台主機，各自提交不同類型的 job（inference / training / embedding），自動分配資源不互搶。

**這是整個系統最完整的展示場景：**

```
使用者 A: sbatch --gres=gpu:rtx4080:1 fine_tune.sh    → 佔用整張 RTX 4080
使用者 B: sbatch --gres=mps:25 --array=1-4 infer.sh   → 4 個 job 共用 RTX 5070
使用者 C: sbatch --cpus-per-task=4 preprocess.sh       → CPU worker，不用 GPU
                    ↓
          Operator 監測 queue，動態開關 worker pod
                    ↓
          Grafana 顯示每個使用者的 CPU/GPU 佔用時間
```

**對應現實案例（2024–2026）：**
- **NCHC 國網中心**：台灣高校最大共用 GPU cluster，Slurm 排隊是標準。
- **HuggingFace Spaces 自架版**：讓實驗室成員部署自己的 demo，不用搶同一台 notebook。
- **AWS SageMaker HPC（Slurm 模式）**：2024 年 AWS 推出 SageMaker HyperPod，本質是 Slurm on K8s，與本系統架構高度相似。

---

## 三、推薦的畢業論文定位

### 題目方向

> **「基於 Kubernetes 的彈性 Slurm 排程系統：設計、實作與 AI 工作負載驗證」**

或更聚焦：

> **「Slurm-on-Kubernetes 彈性排程器設計與 LLM 推論 / RAG 工作負載評估」**

### 系統貢獻點（Contribution）

1. **Operator 設計**：無 CRD 框架的輕量彈性控制器，支援 Checkpoint-aware scale-down guard——這是現有開源系統（SUNK、Volcano）未充分解決的問題。

2. **MPS 多工整合**：在 Kubernetes pod 層級透過 Slurm GRES `mps:N` 實現 SM 細粒度分配，對比傳統「一 job 一卡」提升 GPU 利用率。

3. **工作負載評估**：以 LLM 推論（throughput / latency）和 Embedding batch（QPS）兩種典型 AI 任務，量化彈性排程對 GPU 利用率的改善。

### 論文章節對應

| 論文章節 | 對應程式碼 / 實驗 |
|---------|----------------|
| 相關工作（Slurm、K8s、MPS） | `docs/note.md`、`docs/migration.md` |
| 系統設計 | `operator/main.py`、`manifests/core/` |
| GPU 資源管理 | `gres.conf`、`mps-daemonset.yaml`、MPS socket mount |
| 彈性排程實驗 | Operator scale-up latency、cooldown behavior |
| 工作負載驗證 | LLM inference throughput under MPS；DDP 2-GPU 訓練 throughput |
| 監控與可觀測性 | Grafana dashboard、Prometheus metrics |

---

## 四、各應用與現有系統功能的對應總表

| 現有功能 | LLM 推論 | RAG Serving | DDP 訓練 | 多使用者共用 |
|---------|---------|------------|---------|-----------|
| Slurm 排隊 | ✅ 推論 job 排程 | ✅ batch embedding | ✅ 訓練 job | ✅ 核心功能 |
| Operator autoscale | ✅ 依 queue 開 GPU worker | ✅ 依 embedding load 擴展 | ✅ 訓練時開 N worker | ✅ 自動縮回省電 |
| MPS `mps:N` | ✅ 多路 inference | ✅ 並行 embedding | ❌（訓練需獨占） | ✅ 細粒度分配 |
| Checkpoint guard | ✅ 推論服務不中斷 | ✅ | ✅ 訓練 checkpoint 保護 | ✅ |
| NFS PVC | 模型權重共享 | Vector DB 持久化 | Dataset 共享 | ✅ |
| Prometheus / Grafana | GPU 利用率、latency | Embedding QPS | 訓練 throughput | 使用者資源用量 |
| Dual GPU（5070+4080） | 大小模型分流 | Embed+Generate 分流 | 真實 2-GPU DDP | 隔離不同 job |

---

## 五、不適合的應用（避免誤用）

- **70B+ 模型完整 FP16 推論**：需要 ≥80 GB VRAM（A100 80GB×2），本系統 VRAM 不足。
- **大規模預訓練**（GPT-4 規模）：需要數十至數百張 A100/H100。
- **即時串流影像處理**（< 10ms latency）：Slurm 排隊本質上有 15–30s 延遲，不適合即時推理。
- **多租戶 SaaS 平台**：沒有 quota enforcement、network isolation（Phase 5 功能），目前不宜。

---

*最後更新：2026-04-24*
