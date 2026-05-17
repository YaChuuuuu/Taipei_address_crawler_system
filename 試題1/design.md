# 試題1 — 設計說明

## 爬蟲框架選擇

| 框架 | 主要用途 | 適合場景 |
|------|----------|----------|
| **Requests** | 發送 HTTP 請求，取得原始 HTML | 靜態網頁、有明確 API endpoint、不需瀏覽器渲染 |
| **Selenium** | 控制真實瀏覽器操作 | 動態網頁（JavaScript 渲染）、需要點擊/填表、iframe、驗證碼截圖 |
| **Scrapy** | 非同步爬蟲框架，內建 pipeline | 大規模爬取多頁/多網站、需要佇列管理、高效能批量任務 |

本專案選用 **Selenium**，原因如下：

1. 目標網站內容由 JavaScript 動態渲染，Requests 拿到的 HTML 不含資料
2. 查詢流程需要切換 iframe、點選頁籤、操作下拉選單、填表單後送出
3. 驗證碼需對 `<img>` 元素截圖（`screenshot_as_png`）後送 OCR 辨識
4. 僅爬取台北市 12 個行政區，Scrapy 的分散式架構超出本題規模

---

## 排程設計說明

### 架構

排程邏輯住在 **crawler 容器**內，由 **APScheduler BackgroundScheduler** 管理。  
外部不直接操作 scheduler 物件，而是透過 crawler 容器內建的 FastAPI 端點控制：

| 端點 | 說明 |
|------|------|
| `POST /set` | 啟動 cron 排程（指定 hour / minute / day_of_week） |
| `POST /run` | 立即觸發一次（不影響既有排程） |
| `POST /stop` | 移除所有排程 |
| `GET /status` | 查詢目前狀態與下次執行時間 |

`api` 容器不持有任何排程邏輯，只將前端請求 **HTTP proxy** 轉發至 `crawler:8001`。

### 選用 APScheduler cron 模式的理由

- `cron` trigger 支援 `hour`、`minute`、`day_of_week` 組合，適合「每天指定時間執行」的需求
- `BackgroundScheduler` 在背景執行緒中運行，不阻塞 FastAPI 的請求處理
- APScheduler 不需要額外的 broker 服務，部署更簡單

### 使用範例

```bash
# 設定每天 08:30 執行
curl -X POST http://localhost:8000/scheduler/set \
     -H "Content-Type: application/json" \
     -d '{"hour": 8, "minute": 30}'

# 立即觸發一次（不影響排程）
curl -X POST http://localhost:8000/scheduler/run

# 查詢目前排程狀態
curl http://localhost:8000/scheduler/status

# 停止排程
curl -X POST http://localhost:8000/scheduler/stop
```

### 注意事項

排程狀態**不持久化**，crawler 容器重啟後需重新呼叫 `/scheduler/set`。  
可透過 Streamlit 管理介面（`http://localhost:8501`）操作，無需手動執行 curl。
