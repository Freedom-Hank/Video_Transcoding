# 分散式影片轉檔系統

這是一個以 `manager` / `worker` 架構實作的分散式影片轉檔專案。系統會先讀取上傳影片長度，將影片規劃成多個時間區間任務，再把任務分派到多個 Worker 平行編碼，最後由 Manager 合併成最終輸出檔。

專案提供網頁管理介面，可用來上傳影片、查看任務狀態、監看 Worker 節點健康狀況，並下載已完成的輸出結果。

## 特色

- 影片上傳後自動建立轉檔工作
- Manager 只規劃時間區間，不再預先產生實體切片檔
- 多個 Worker 直接從共用原始影片讀取指定時間區間並平行編碼
- Worker 可使用 NVIDIA NVENC 進行 GPU 硬體加速，並可 fallback 到 CPU 編碼
- Worker 會自動註冊、輪詢任務並回報進度
- 支援節點心跳監控與失聯任務重排
- 提供簡單的 Web UI 與 REST API
- 使用 Docker Compose 一次啟動 Manager 與多個 Worker

## 系統架構

- `manager`
  - 提供 Web UI
  - 接收上傳、建立工作、查詢工作狀態
  - 負責時間區間規劃、合併與任務排程
  - 監控 Worker 心跳與健康狀態
- `worker`
  - 啟動後向 Manager 註冊
  - 輪詢取得編碼任務
  - 使用 FFmpeg 對指定時間區間進行轉檔
  - 回報進度、完成與失敗狀態
- `shared-volume`
  - 由主機資料夾掛載到容器內的 `/data`
  - 保存 `uploads/`、`segments/`、`outputs/`

## 專案目錄

```text
├── compose.yaml
├── manager/
│   ├── app.py
│   ├── scheduler.py
│   ├── splitter.py
│   ├── merger.py
│   ├── health_monitor.py
│   └── static/index.html
└── worker/
    ├── app.py
    ├── encoder.py
    └── metrics.py
```

## 需求

- Docker
- Docker Compose

容器內已安裝：

- `ffmpeg`
- `ffprobe`
- `procps`（Worker 端用於讀取 `top`）

## 快速開始

### 1. 啟動服務

```bash
docker compose up --build
```

啟動後，開啟：

```text
http://localhost:8080
```

### 2. 背景執行

```bash
docker compose up -d --build
```

### 3. 停止服務

```bash
docker compose down
```

## 使用流程

1. 在 Manager 網頁上傳影片。
2. 選擇輸出解析度、格式與位元率。
3. Manager 使用 `ffprobe` 讀取影片長度，規劃時間區間任務。
4. Worker 依序從任務佇列領取時間區間批次並平行編碼。
5. 所有片段完成後，Manager 將結果合併成單一輸出檔。
6. 工作完成後，可從管理介面下載成品。

## 預設行為

- 預設輸出解析度：`1280x720`
- 預設輸出格式：`mp4`
- 預設位元率：`2M`
- 預設時間區間最低長度：`10` 秒
- 預設最多規劃 `90` 個時間區間
- 預設每個 Worker 任務處理 `5` 個時間區間
- Worker 預設自動偵測並優先使用 NVIDIA NVENC
- Worker 預設每 `2` 秒輪詢一次任務
- Worker 預設每 `5` 秒回報一次心跳

## API 說明

### Manager API

| 方法 | 路徑 | 說明 |
|------|------|------|
| `POST` | `/jobs` | 建立轉檔工作，表單欄位包含 `video`、`resolution`、`format`、`bitrate` |
| `GET` | `/jobs` | 列出所有工作 |
| `GET` | `/jobs/:id` | 取得單一工作詳情與片段狀態 |
| `DELETE` | `/jobs/:id` | 刪除尚未開始處理的排隊工作 |
| `GET` | `/jobs/:id/download` | 下載已完成的輸出檔 |
| `GET` | `/nodes` | 列出所有 Worker 節點與資源使用狀態 |
| `POST` | `/nodes/register` | Worker 啟動時註冊節點 |
| `POST` | `/nodes/:name/heartbeat` | Worker 回報心跳與系統資源資訊 |
| `GET` | `/workers/:name/task` | Worker 輪詢取得下一個任務 |
| `POST` | `/workers/:name/task/:id/progress` | 回報任務進度 |
| `POST` | `/workers/:name/task/:id/complete` | 回報任務完成 |
| `POST` | `/workers/:name/task/:id/fail` | 回報任務失敗並重新排隊 |

### Worker API

| 方法 | 路徑 | 說明 |
|------|------|------|
| `GET` | `/task` | 查詢本機是否忙碌 |
| `GET` | `/metrics` | 回傳 CPU 與記憶體資訊 |

## 任務流程

1. 上傳影片後，Manager 會先建立工作紀錄。
2. 工作進入 `splitting` 狀態後，Manager 使用 `ffprobe` 規劃時間區間，不產生 raw segment 檔案。
3. Manager 會把多個時間區間包成批次任務。
4. Worker 從任務佇列中領取批次任務，直接讀取原始影片的指定時間區間並進行編碼。
5. Worker 會持續回報進度，完成後上報輸出檔位置。
6. 當所有片段都完成後，Manager 會將片段合併成最終檔案。

## 容錯與健康監控

- Worker 會定期送出心跳與系統資源數據。
- Manager 超過心跳逾時後會先標記節點為 `suspected`。
- 若連續多次逾時，節點會被標記為 `offline`。
- 離線節點正在處理中的任務會被重新排回佇列。
- Worker 重新啟動並再次註冊後，會自動恢復可派工狀態。

## 環境變數

### Manager

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `DATA_DIR` | `/data` | 共用資料目錄 |
| `SEGMENT_DURATION` | `10` | 每個時間區間的最低秒數 |
| `MAX_SEGMENTS` | `90` | 依影片長度自動提高區間秒數，避免產生過多任務 |
| `TASK_BATCH_SIZE` | `5` | 每個 Worker 任務一次處理的時間區間數 |
| `HEARTBEAT_TIMEOUT` | `15` | 心跳逾時秒數 |
| `HEARTBEAT_FAILURE_THRESHOLD` | `3` | 判定離線前的連續逾時次數 |
| `TASK_TIMEOUT_SECONDS` | `3600` | 單個批次任務逾時秒數 |
| `MAX_TASK_RETRIES` | `3` | 批次任務失敗後的最大重試次數 |
| `MAX_UPLOAD_MB` | `2048` | 上傳檔案大小上限 MB |

### Worker

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `WORKER_NAME` | `worker-1` | Worker 節點名稱 |
| `MANAGER_URL` | `http://manager:8080` | Manager 位址 |
| `DATA_DIR` | `/data` | 共用資料目錄 |
| `POLL_INTERVAL` | `2` | 輪詢任務間隔秒數 |
| `HEARTBEAT_INTERVAL` | `5` | 心跳間隔秒數 |
| `VIDEO_ENCODER` | `auto` | `auto` 會優先使用 `h264_nvenc`，不可用時 fallback 到 `libx264` |
| `FFMPEG_THREADS` | `2` | CPU fallback 編碼時的 FFmpeg thread 數 |

## Docker Compose

`compose.yaml` 預設會啟動：

- `manager`
- `worker-1`
- `worker-2`
- `worker-3`

如果要擴充 Worker，可以複製現有 Worker 服務區塊，並替每個節點設定不同的 `WORKER_NAME`。

## 資料位置

容器內部資料會寫到 `/data`，包含：

- `/data/uploads`：原始上傳檔
- `/data/segments`：Worker 編碼後的時間區間輸出片段
- `/data/outputs`：最後合併完成的成品

## 注意事項

- 時間區間規劃依賴 `ffprobe`，編碼與合併依賴 FFmpeg，容器內已內建安裝。
- GPU 加速需要 Docker Desktop 可使用 NVIDIA runtime，且 FFmpeg 支援 `h264_nvenc`。
- 目前系統是以 Docker Compose 單機環境為主，適合展示、測試或小型部署。
- 只有狀態為 `queued` 的工作可以刪除。

## 開發補充

Manager 與 Worker 都是 Flask 應用。若你想直接檢查行為，可以分別查看：

- [Manager 入口](/Volumes/Work_Data/Video_Transcoding/manager/app.py)
- [Worker 入口](/Volumes/Work_Data/Video_Transcoding/worker/app.py)
