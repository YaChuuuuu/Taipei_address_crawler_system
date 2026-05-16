from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import traceback
import threading
import os
from dotenv import load_dotenv
import ddddocr
import psycopg2
import csv
import logging
from log_utils import LevelAwareLokiHandler
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from pydantic import BaseModel
from contextlib import asynccontextmanager
import uvicorn

debug = 0
TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# ── 查詢條件 ───────────────────────────────────────────────────
load_dotenv(override=True) # 載入 .env 檔案內容
IS_DOCKER      = os.getenv("RUNNING_IN_DOCKER", "false") == "true"
DATE_START = os.getenv("DATE_START","114-09-01")
DATE_END = os.getenv("DATE_END","114-11-30")
QUERY_TYPE = os.getenv("QUERY_TYPE","門牌初編")
MAX_RETRY  = int(os.getenv("MAX_RETRY", "10"))
TARGET_URL = os.getenv("TARGET_URL","https://www.ris.gov.tw/app/portal/3053")
DATA_PATH=  os.getenv("DATA_PATH","./data/")
LOG_PATH = os.path.join(DATA_PATH, "crawler.log")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB   = os.getenv("POSTGRES_DB", "crawler")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
LOKI_PUSH_URL  = f"{LOKI_URL}/loki/api/v1/push"
CSV_PATH = os.path.join(DATA_PATH, f"scraped_data_{datetime.now(TAIPEI_TZ).strftime('%Y%m%d')}.csv")

TAIPEI_DISTRICTS = [
    "中正區", "大同區", "中山區", "松山區", "大安區",
    "萬華區", "信義區", "士林區", "北投區", "內湖區",
    "南港區", "文山區",
]

# ── 初始化設定 ───────────────────────────────────────────────────
# OCR辨識
ocr = ddddocr.DdddOcr(show_ad=False)

# 設定log
if not os.path.exists(DATA_PATH):
    os.makedirs(DATA_PATH) # 自動建立 data 資料夾

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),  # 寫入檔案
        logging.StreamHandler(),                                # 同時印在終端機
    ],
)
logger = logging.getLogger(__name__)

try:
    loki_handler = LevelAwareLokiHandler(
        url=LOKI_PUSH_URL,
        tags={"job": "crawler"},
        version="1",
    )
    logger.addHandler(loki_handler)
except Exception as e:
    logger.warning(f"Loki 連線失敗，log 只寫入本地檔案：{e}")

# ── 資料庫連線 ───────────────────────────────────────────────────
def get_db_conn():
    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
    )

def init_db():
    conn = get_db_conn()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scraped_items (
            id SERIAL PRIMARY KEY,
            city TEXT,
            district TEXT,
            sequence_num TEXT,
            address TEXT,
            type TEXT,
            date TEXT,
            created_at TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

def save_to_db(records):
    if not records:
        logger.info("沒有資料需要存入資料庫")
        return

    conn = get_db_conn()
    cursor = conn.cursor()
    data_to_insert = [
        (
            r['縣市'],
            r['鄉鎮市區'],
            r['序號'],
            r['門牌資料'],
            r['編釘類別'],
            r['編釘日期'],
            r["爬取日期"]
        ) for r in records
    ]

    try:
        cursor.executemany('''
            INSERT INTO scraped_items (city, district, sequence_num, address, type, date, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', data_to_insert)
        conn.commit()
        logger.info(f"成功存入 {len(data_to_insert)} 筆資料至 PostgreSQL")
    except Exception as e:
        conn.rollback()
        logger.error(f"資料庫存入失敗: {e}")
    finally:
        conn.close()


def save_to_csv(records):
    if not records:
        return
    # 檢查檔案是否已存在，若不存在則需要寫入標頭
    file_exists = os.path.isfile(CSV_PATH)

    try:
        with open(CSV_PATH, 'a', newline='', encoding='utf-8-sig') as f:
            fieldnames = records[0].keys()
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()  # 檔案第一次建立時才寫標頭
            writer.writerows(records)
        logger.info("資料存至 CSV")
    except Exception as e:
        logger.error(f" CSV 輸出失敗：{e}")

# ── 瀏覽器設定 ───────────────────────────────────────────────────
def build_driver() -> webdriver.Chrome:
    options = Options()
    if IS_DOCKER:
        options.add_argument("--headless")
        options.binary_location = "/usr/bin/chromium"
        service = Service("/usr/bin/chromedriver")
    else:
        # 本機 Windows
        service = Service(ChromeDriverManager().install())
        options.add_argument("--window-size=1920,1080")
    
    # 其他共用設定
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    return webdriver.Chrome(service=service, options=options)

# ── 處理驗證碼 ───────────────────────────────────────────────────

def read_captcha(driver):
    captcha_img = driver.find_element(By.ID, "captchaImage_captchaKey")
    img_bytes = captcha_img.screenshot_as_png
    code = ocr.classification(img_bytes)
    return code.strip()


def submit_captcha(driver, wait, captcha_code):
    captcha_input = wait.until(
        EC.presence_of_element_located((By.ID, "captchaInput_captchaKey")))
    captcha_input.clear()
    captcha_input.send_keys(captcha_code)
    search_btn = wait.until(EC.element_to_be_clickable((By.ID, "goSearch")))
    search_btn.click()

    try:
        #跳出視窗表示無資料或錯誤
        WebDriverWait(driver, 3).until( EC.presence_of_element_located((By.ID, "swal2-title")))
        error_title = driver.find_element(By.ID, "swal2-title" ).text
        driver.find_element( By.CSS_SELECTOR,".swal2-confirm").click()

        if error_title == "查無資料":
            return "NO_DATA"

        return "RETRY"

    except:
        return "SUCCESS"


def scrape_district(driver: webdriver.Chrome, district: str) -> list[dict]:
    """
    對單一行政區執行查詢並回傳結果列表。

    步驟：
      1. 開啟網頁
      2. 點選「以編訂日期、編訂類別查詢」頁籤
      3. 選縣市 → 選鄉鎮市區
      4. 填入起訖日期
      5. 選編訂類別
      6. 驗證碼 
      8. 解析結果表格
      9. 處理分頁（若有）
    """
    records = []
    try:
        driver.get(TARGET_URL)
        wait = WebDriverWait(driver, 3)
        # 切換進 iframe
        iframe = wait.until(EC.presence_of_element_located(
            (By.ID, "content-frame")
        ))
        driver.switch_to.frame(iframe)
        
        # ── Step 1：點選「編訂日期查詢」頁籤 ──────────────────
        tab = wait.until(EC.element_to_be_clickable(
            (By.XPATH, "//button[@data-type='date']")
        ))
        tab.click()
        time.sleep(1)
        
        # ── Step 2：選縣市 ──────────────────────────────────────
        # 找到臺北市的 area 元素 data-id='63000000'，用 data-id 點擊
        taipei = driver.find_element(
            By.XPATH, "//area[@data-id='63000000']"
        )
        driver.execute_script("arguments[0].click();", taipei)
        
        # ── Step 3：選鄉鎮市區 ─────────────────────────────────
        wait.until(EC.element_to_be_clickable((By.ID, "areaCode")))
        district_select = Select(driver.find_element(By.ID, "areaCode"))
        district_select.select_by_visible_text(district)
        
        # ── Step 4：填入起訖日期 ────────────────────────────────
        wait.until(EC.element_to_be_clickable((By.ID, "sDate")))
        driver.execute_script(
            "document.getElementById('sDate').value = arguments[0];", 
            DATE_START
        )

        wait.until(EC.element_to_be_clickable((By.ID, "eDate")))
        driver.execute_script(
            "document.getElementById('eDate').value = arguments[0];", 
            DATE_END
        )
        
        # ── Step 5：選編訂類別 ──────────────────────────────────
        wait.until(EC.element_to_be_clickable((By.ID, "registerKind")))
        type_select = Select(driver.find_element(By.ID, "registerKind"))
        type_select.select_by_visible_text(QUERY_TYPE)
        

        # ── Step 6：驗證碼&點擊搜尋 ──────────────────────────────
        # 依題目提示：驗證碼無法辨識時可人工輸入
       
        no_data = False # 設定無資料跳出
        
        # 開始OCR辨識
        retry_count = 0
        
        while retry_count < MAX_RETRY:
            retry_count += 1
            captcha_code = read_captcha(driver)
            result = submit_captcha(driver, wait, captcha_code)
        
            if result == "SUCCESS":
                break

            if result == "NO_DATA":
                no_data = True
                break
        
        # OCR 失敗超過次數，跳過此行政區
        if retry_count >= MAX_RETRY and not no_data:
            logger.error(f"{district} 驗證碼辨識失敗超過 {MAX_RETRY} 次，跳過")
            return records
       
        # 查無資料跳過解析
        if no_data:
            logger.info(f"{district} 查無資料")
            return records

        
        # ── Step 8：解析結果（含分頁） ──────────────────────────
        while True:
            wait.until(EC.presence_of_element_located((By.ID, "jQGrid")))
            # 只抓有 id 的 tr（真實資料列），跳過 jqgfirstrow
            rows = driver.find_elements(By.CSS_SELECTOR, "#jQGrid tbody tr[id]")
            
            for row in rows:
                cols = row.find_elements(By.TAG_NAME, "td")
                if len(cols) >= 4:
                    records.append({
                        "縣市":    "臺北市",
                        "鄉鎮市區": district,
                        "序號": cols[0].get_attribute("title"), 
                        "門牌資料": cols[1].get_attribute("title"), 
                        "編釘日期": cols[2].get_attribute("title"),
                        "編釘類別": cols[3].get_attribute("title"),
                        "爬取日期": datetime.now(TAIPEI_TZ)
                    })

            # ── 檢查是否有下一頁 ────────────────────────────────
            try:
                next_btn = driver.find_element(By.ID, "next_result-pager")
                if "disabled" in next_btn.get_attribute("class"):
                    break
                next_btn.click()
                time.sleep(1)
            except Exception:
                break  # 沒有下一頁

        logger.info(f"{district} 爬取完成，共 {len(records)} 筆")

    except Exception as e:
        print("錯誤：", type(e).__name__)
        driver.save_screenshot(f"{district}_error_{datetime.now(TAIPEI_TZ).strftime('%Y%m%d_%H%M%S')}.png")
        traceback.print_exc() # Use traceback for full exception info
        logger.error(f"{district} 爬取失敗：{e}", exc_info=True)

    return records    


# 主流程
# ══════════════════════════════════════════════════════════════
 
def run_crawler():
    """主爬蟲流程：爬取台北市所有行政區"""
    if not _running_lock.acquire(blocking=False):
        return
    try:
        logger.info(f"爬蟲開始執行：{datetime.now(TAIPEI_TZ).strftime('%Y-%m-%d %H:%M:%S')}")

        try:
            init_db()
            all_records = []
            driver = build_driver()
        except Exception as e:
            logger.error(f"爬蟲初始化失敗：{e}", exc_info=True)
            return

        if debug:
            districts = TAIPEI_DISTRICTS[:2]
        else:
            districts = TAIPEI_DISTRICTS

        try:
            for district in districts:
                records = scrape_district(driver, district)
                all_records.extend(records)
                time.sleep(2)
        finally:
            driver.quit()

        save_to_db(all_records)
        save_to_csv(all_records)

        logger.info(f"全部完成，共爬取 {len(all_records)} 筆資料")
    finally:
        _running_lock.release()
    


# ── 排程器 ───────────────────────────────────────────────────────
_running_lock = threading.Lock()
scheduler = BackgroundScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

crawler_app = FastAPI(lifespan=lifespan)

class ScheduleConfig(BaseModel):
    hour: int = 8              # 每天幾點（0-23）
    minute: int = 0            # 幾分（0-59）
    day_of_week: str = "*"     # 星期幾，例如 "mon,wed,fri" 或 "0,2,4" 或 "*"（每天）


@crawler_app.post("/run")
def trigger_run():
    """立即觸發一次爬蟲"""
    logger.info("手動觸發爬蟲")
    if _running_lock.locked():
        logger.warning("爬蟲已在執行中，跳過此次")
        return {"status": "skipped", "detail": "爬蟲已在執行中"}
    threading.Thread(target=run_crawler, daemon=True).start()
    return {"status": "running", "detail": "爬蟲已啟動"}

@crawler_app.post("/set")
def set_schedule(config: ScheduleConfig):
    """啟動排程：cron（指定星期幾 + 時間）"""

    scheduler.remove_all_jobs()
    scheduler.add_job(
        run_crawler, "cron",
        day_of_week=config.day_of_week,
        hour=config.hour,
        minute=config.minute,
        id="crawler_job"
    )

    day_label = config.day_of_week if config.day_of_week != "*" else "每天"
    detail = f"{day_label} {config.hour:02d}:{config.minute:02d}"

    if not scheduler.running:
        scheduler.start()

    logger.info(f"排程已啟動：{detail}")
    return {"status": "set", "detail": detail}

@crawler_app.post("/stop")
def stop_schedule():
    """停止排程（保留設定，狀態變為 set）"""
    scheduler.remove_all_jobs()
    logger.info("排程已停止")
    return {"status":  "idle", "detail": "排程已停止"}

@crawler_app.get("/status")
def schedule_status():
    """查詢目前真實狀態：running（執行中） / set（已排程） / idle（閒置）"""
    
    # 1. 檢查「此時此刻」爬蟲是不是正在執行（不論是手動還是排程觸發的）
    is_running = _running_lock.locked()
    
    # 2. 檢查排程器裡面有沒有工作
    jobs = scheduler.get_jobs()
    has_schedule = len(jobs) > 0
    next_run = str(jobs[0].next_run_time) if has_schedule else None

    # 狀態分支判斷
    if is_running:
        return {
            "status": "running",       # 真正正在跑
            "next_run": next_run,
            "detail": "爬蟲正在執行中"
        }
    elif has_schedule:
        return {
            "status": "set",     # 沒在跑，但時間到會自己動
            "next_run": next_run,
            "detail": f"排程中，下次執行時間：{next_run}"
        }
    else:
        return {
            "status": "idle",          # 沒在跑，未來也沒排程
            "next_run": None,
            "detail": "系統閒置中"
        }

@crawler_app.get("/health")
def crawler_health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(crawler_app, host="0.0.0.0", port=8001)


