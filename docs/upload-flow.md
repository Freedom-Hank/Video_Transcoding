# 分塊上傳流程

```mermaid
sequenceDiagram
    autonumber
    participant 瀏覽器
    participant Manager

    瀏覽器->>Manager: ① upload-init<br/>(檔名 + 大小 + 設定)
    Manager-->>瀏覽器: 回傳 job_id、chunk_size

    loop 每塊 ≈ 50MB
        瀏覽器->>Manager: ② 上傳一塊
        Manager-->>瀏覽器: 收到
    end

    瀏覽器->>Manager: ③ upload-complete
    Manager-->>瀏覽器: Job 進入 排隊中
```

## 重點

- **為什麼分塊**:Cloudflare Tunnel 對 HTTP body 上限為 100MB,分塊上傳可繞過此限制,並讓單檔最大可達 2GB(可調)。
- **斷點安全**:Manager 嚴格按 index 接收,缺塊不會被接受。
- **容量保護**:`upload-init` 前會檢查 `uploads/` 剩餘空間,超過 `MAX_UPLOADS_TOTAL_MB` 直接拒絕。
