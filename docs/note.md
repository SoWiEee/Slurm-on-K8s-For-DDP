# Development Notes (Phase 1)

## 目標

完成 Timeline 的 Phase 1：

1. 建立 Slurm Controller / Worker 映像。
2. 在 Kind 部署靜態 Slurm 叢集。
3. 讓 Pod 間具備 SSH 互通與 Munge 認證。

---

## 開發想法

## 1) 先穩定，再擴展

Phase 1 先不做 Operator 與自動擴縮，先把最小可用系統（MVP）做穩：

- 固定 1 個 Controller + 2 個 Worker。
- 以 StatefulSet 保障 Pod 命名穩定（例如 `slurm-worker-0`）。
- `slurm.conf` 先採固定節點名稱，避免動態註冊提升除錯成本。

## 2) 設定集中管理

- Slurm 設定放在 `ConfigMap`（`phase1/manifests/slurm-static.yaml`）。
- SSH 與 Munge 金鑰放在 `Secret`，並以腳本產生，避免把敏感資料提交到 Git。
- 啟動流程放在容器 `entrypoint.sh`，讓行為可讀、可追蹤。

## 3) 針對 timeout 問題的修正策略

根據使用者回報（StatefulSet rollout timeout + Pod Error），Phase 1 追加了以下強化：

- `entrypoint.sh` 先建立並修正 munge 所需目錄權限（`/run/munge`、`/var/lib/munge`、`/var/log/munge`）。
- 啟動 `munged` 後主動檢查程序是否存在，不再「失敗但繼續跑」。
- `SlurmctldHost` 改為完整 DNS：`slurm-controller-0.slurm-controller`，降低解析不穩定。
- StatefulSet 增加 readiness/liveness probe，讓 rollout 判斷更準確。
- bootstrap 失敗時自動收集 `get pods` / `describe` / `logs`，減少手動排查時間。

---

## 除錯方式

## A) Pod 卡在 CrashLoopBackOff 或 Error

1. 看 Pod 事件：

```bash
kubectl -n slurm describe pod <pod-name>
```

2. 看容器日誌：

```bash
kubectl -n slurm logs <pod-name>
```

常見原因：
- `munge.key` 沒掛載成功。
- `munge.key` 權限不正確（必須 `0400` 且 owner `munge`）。
- `munged` 需要的目錄權限不正確。

## B) `sinfo` 看不到 worker

1. 到 controller 內查節點：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- scontrol show nodes
```

2. 到 worker 看 slurmd log：

```bash
kubectl -n slurm logs pod/slurm-worker-0
```

常見原因：
- `SlurmctldHost` 寫錯。
- DNS service 名稱與 StatefulSet serviceName 不一致。

## C) SSH 不通

1. 從 controller 手動 ssh：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- \
  bash -lc 'ssh -o StrictHostKeyChecking=no slurm-worker-0.slurm-worker hostname'
```

2. 若失敗，檢查：
- `id_ed25519` / `id_ed25519.pub` 是否掛載。
- `authorized_keys` 是否在 entrypoint 中正確生成。
- `sshd` 是否有正常啟動。

## D) Munge 驗證

可在 controller 產生 token 再在 worker 解碼（進階）：

```bash
kubectl -n slurm exec pod/slurm-controller-0 -- munge -n
kubectl -n slurm exec pod/slurm-worker-0 -- unmunge
```

若解碼失敗，通常是 `munge.key` 不一致或權限不正確。

---


## E) 本次真實 root cause（依錯誤訊息）

你提供的訊息其實有兩個獨立問題：

1. `Exit Code: 127` + `/usr/bin/env: 'bash\r': No such file or directory`
   - 代表 entrypoint 以 CRLF 換行被複製進 image，容器內解析 shebang 失敗。
   - 解法：用 `.gitattributes` 強制 LF，並建議重置 working tree 後重建 image。

2. `error mounting ... /etc/slurm/slurm.conf ... no such file or directory`
   - 來自 `subPath` 掛單一檔案在某些環境容易碰到初始化邊緣錯誤。
   - 解法：把 ConfigMap 整個掛到 `/etc/slurm`，避免 subPath mount path 問題。

## 後續銜接（Phase 2 前）

- 把 worker 改成 Deployment + 動態 replicas。
- 將節點數量與 partition 設定改為可程式化更新。
- 導入 Operator（Kopf）觀察 Pending jobs 並觸發 scale up/down。
