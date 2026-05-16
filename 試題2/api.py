from fastapi import FastAPI, HTTPException, Query
import psycopg2
import psycopg2.extras
import httpx
import asyncio
import os
from pydantic import BaseModel
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import logging
from log_utils import LevelAwareLokiHandler

#載入環境變數
load_dotenv()

DATA_PATH     = os.getenv("DATA_PATH", "./data/")
os.makedirs(DATA_PATH, exist_ok=True)
LOG_PATH      = os.path.join(DATA_PATH, "api.log")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
LOKI_PUSH_URL  = f"{LOKI_URL}/loki/api/v1/push"
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB   = os.getenv("POSTGRES_DB", "crawler")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
CRAWLER_URL   = os.getenv("CRAWLER_URL", "http://crawler:8001")

#設定log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

try:
    loki_handler = LevelAwareLokiHandler(
        url=LOKI_PUSH_URL,
        tags={"job": "api"},
        version="1",
    )
    logger.addHandler(loki_handler)
except Exception as e:
    logger.warning(f"Loki 連線失敗，log 只寫入本地檔案：{e}")

app = FastAPI()

@app.post("/test/error")
def test_error():
    logger.error("測試錯誤通知：這是一則手動觸發的 ERROR log")
    return {"triggered": True}

def get_db_conn():
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )

# 輸入格式
class QueryInput(BaseModel):
    city: str
    district: str

# 查詢資料庫
def query_db(city: str, district: str):
    with get_db_conn() as conn:
        with conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor
        ) as cur:

            cur.execute("""
                SELECT *
                FROM scraped_items
                WHERE city = %s AND district = %s
            """, (city, district))

            rows = cur.fetchall()

    return [dict(row) for row in rows]

# API 端點
@app.post("/query")
def query(input: QueryInput):
    logger.info(f"查詢請求：city={input.city}, district={input.district}")
    try:
        results = query_db(input.city, input.district)
    except Exception as e:
        logger.error(f"查詢失敗：{e}", exc_info=True)
        raise HTTPException(status_code=500, detail="資料庫查詢失敗")

    if not results:
        logger.warning(f"{input.city}, {input.district}: 查無資料")
        return {"status": "no_data", "message": "查無資料", "data": []}
    logger.info(f"{input.city}, {input.district}: 共 {len(results)} 筆資料")
    return {"status": "ok", "count": len(results), "data": results}
    

# ── 排程代理端點 ──────────────────────────────────────────────────
class ScheduleConfig(BaseModel):
    hour: int = 8              # 每天幾點（0-23）
    minute: int = 0            # 幾分（0-59）
    day_of_week: str = "*"     # 星期幾，例如 "mon,wed,fri" 或 "*"（每天）

def _proxy_post(path: str, label: str, body: dict | None = None):
    try:
        r = httpx.post(f"{CRAWLER_URL}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"{label}失敗：{e}")
        raise HTTPException(status_code=502, detail=f"無法聯繫爬蟲服務：{e}")

def _proxy_get(path: str, label: str):
    try:
        r = httpx.get(f"{CRAWLER_URL}{path}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"{label}失敗：{e}")
        raise HTTPException(status_code=502, detail=f"無法聯繫爬蟲服務：{e}")

@app.post("/scheduler/set")
def scheduler_set(config: ScheduleConfig):
    return _proxy_post("/set", "排程設定", config.model_dump())

@app.post("/scheduler/stop")
def scheduler_stop():
    return _proxy_post("/stop", "排程停止")

@app.get("/scheduler/status")
def scheduler_status():
    return _proxy_get("/status", "排程狀態查詢")

@app.post("/scheduler/run")
def scheduler_run_now():
    return _proxy_post("/run", "手動觸發爬蟲")

# ── Log 查詢 ──────────────────────────────────────────────────────
@app.get("/logs")
async def get_logs(
    job:   Optional[str] = Query(None, description="crawler / api，不填則全選"),
    level: Optional[str] = Query(None, description="INFO / WARNING / ERROR，不填則全選"),
    start: Optional[str] = Query(None, description="ISO 時間字串，預設 1 小時前"),
    end:   Optional[str] = Query(None, description="ISO 時間字串，預設現在"),
    limit: int           = Query(100, ge=1, le=1000),
):
    now = datetime.now(timezone.utc)

    def _parse_dt(s: str) -> datetime:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    end_dt   = _parse_dt(end)   if end   else now
    start_dt = _parse_dt(start) if start else now - timedelta(hours=1)

    label_filters = [f'job="{job}"'] if job else ['job=~".+"']
    if level:
        label_filters.append(f'level="{level.upper()}"')
    logql = "{" + ", ".join(label_filters) + "}"

    params = {
        "query":     logql,
        "start":     start_dt.isoformat(),
        "end":       end_dt.isoformat(),
        "limit":     limit,
        "direction": "backward",
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params=params,
                timeout=10,
            )
            r.raise_for_status()
    except Exception as e:
        logger.error(f"Loki 查詢失敗：{e}")
        raise HTTPException(status_code=502, detail=f"Loki 查詢失敗：{e}")

    logs = []
    for stream in r.json().get("data", {}).get("result", []):
        job_label = stream.get("stream", {}).get("job", "unknown")
        for ts_ns, message in stream.get("values", []):
            ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=ZoneInfo("Asia/Taipei"))
            logs.append({
                "timestamp": ts.isoformat(),
                "job":       job_label,
                "message":   message,
            })

    logs.sort(key=lambda x: x["timestamp"], reverse=True)
    return {"count": len(logs), "logs": logs}


# API運作檢查
@app.get("/health")
async def health():
    checks = {}

    try:
        conn = get_db_conn()
        conn.cursor().execute("SELECT 1")
        conn.close()
        checks["db"] = "ok"
    except Exception as e:
        logger.warning(f"資料庫異常：{e}")
        checks["db"] = str(e)

    async def check_crawler():
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{CRAWLER_URL}/health", timeout=5)
                r.raise_for_status()
                return "ok"
        except Exception as e:
            logger.warning(f"爬蟲服務異常：{e}")
            return str(e)

    async def check_loki():
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(f"{LOKI_URL}/ready", timeout=5)
                r.raise_for_status()
                return "ok"
        except Exception as e:
            logger.warning(f"Loki 服務異常：{e}")
            return str(e)

    checks["crawler"], checks["loki"] = await asyncio.gather(
        check_crawler(), check_loki()
    )

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if all_ok else "degraded", **checks}