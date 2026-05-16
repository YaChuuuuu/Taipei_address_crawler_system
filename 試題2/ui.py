import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(
    page_title="爬蟲管理介面",
    layout="wide",
)

st.sidebar.title("爬蟲管理介面")
page = st.sidebar.radio(
    "選擇頁面",
    ["查詢爬蟲紀錄", "排程管理", "Log 查詢"],
)


# ─── Page 1: 查詢爬蟲紀錄 ────────────────────────────────────────────────────
if page == "查詢爬蟲紀錄":
    st.title("查詢爬蟲紀錄")

    col1, col2 = st.columns(2)
    with col1:
        city = st.text_input("縣市", placeholder="例如：臺北市")
    with col2:
        district = st.text_input("行政區", placeholder="例如：中正區")

    if st.button("查詢", type="primary"):
        if not city or not district:
            st.warning("請輸入縣市與行政區")
        else:
            with st.spinner("查詢中..."):
                try:
                    r = requests.post(
                        f"{API_URL}/query",
                        json={"city": city, "district": district},
                        timeout=15,
                    )
                    r.raise_for_status()
                    data = r.json()

                    if data.get("status") == "no_data":
                        st.info("查無資料")
                    else:
                        st.metric("總筆數", data.get("count", 0))
                        df = pd.DataFrame(data.get("data", []))
                        st.dataframe(df, use_container_width=True)
                except requests.exceptions.ConnectionError:
                    st.error("無法連線至 API 服務，請確認服務是否啟動")
                except Exception as e:
                    st.error(f"查詢失敗：{e}")


# ─── Page 2: 排程管理 ─────────────────────────────────────────────────────────
elif page == "排程管理":
    st.title("排程管理")

    def load_status():
        try:
            r = requests.get(f"{API_URL}/scheduler/status", timeout=5)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    # 狀態區塊
    st.subheader("目前排程狀態")
    status = load_status()

    STATUS_LABEL = {
        "running": "進行中",
        "idle":    "未啟用",
        "set":     "已排程",
    }

    if "error" in status:
        st.error(f"無法取得排程狀態：{status['error']}")
    else:
        raw_status = status.get("status", "idle")
        next_run_raw = status.get("next_run")

        if next_run_raw:
            try:
                next_run_str = datetime.fromisoformat(next_run_raw).strftime("%m/%d %H:%M")
            except Exception:
                next_run_str = next_run_raw
        else:
            next_run_str = "—"

        col1, col2 = st.columns(2)
        with col1:
            st.metric("狀態", STATUS_LABEL.get(raw_status, raw_status))
        with col2:
            st.metric("下次執行", next_run_str)

    if st.button("重新整理狀態"):
        st.rerun()

    st.divider()

    # 手動觸發
    st.subheader("手動觸發")
    st.caption("立即執行一次爬蟲，不影響現有排程")
    if st.button("立即觸發爬蟲", type="primary"):
        with st.spinner("觸發中..."):
            try:
                r = requests.post(f"{API_URL}/scheduler/run", timeout=10)
                r.raise_for_status()
                st.success("已成功觸發，爬蟲執行中")
            except requests.exceptions.ConnectionError:
                st.error("無法連線至 API 服務")
            except Exception as e:
                st.error(f"觸發失敗：{e}")

    st.divider()

    # 排程設定
    st.subheader("設定排程")

    col1, col2 = st.columns(2)
    with col1:
        hour = st.number_input("執行時間（時）", min_value=0, max_value=23, value=8)
    with col2:
        minute = st.number_input("執行時間（分）", min_value=0, max_value=59, value=0)

    DOW_OPTIONS = {
        "每天": "*",
        "星期一至五（工作日）": "mon,tue,wed,thu,fri",
        "星期一": "mon",
        "星期二": "tue",
        "星期三": "wed",
        "星期四": "thu",
        "星期五": "fri",
        "星期六": "sat",
        "星期日": "sun",
    }
    dow_label = st.selectbox("執行日", list(DOW_OPTIONS.keys()))
    day_of_week = DOW_OPTIONS[dow_label]

    col_set, col_stop = st.columns(2)
    with col_set:
        if st.button("設定排程", type="primary", use_container_width=True):
            with st.spinner("設定中..."):
                try:
                    r = requests.post(
                        f"{API_URL}/scheduler/set",
                        json={"hour": int(hour), "minute": int(minute), "day_of_week": day_of_week},
                        timeout=10,
                    )
                    r.raise_for_status()
                    st.success(f"排程已設定：{dow_label} {int(hour):02d}:{int(minute):02d}")
                    st.rerun()
                except requests.exceptions.ConnectionError:
                    st.error("無法連線至 API 服務")
                except Exception as e:
                    st.error(f"設定失敗：{e}")
    with col_stop:
        if st.button("停止排程", use_container_width=True):
            with st.spinner("停止中..."):
                try:
                    r = requests.post(f"{API_URL}/scheduler/stop", timeout=10)
                    r.raise_for_status()
                    st.success("排程已停止")
                    st.rerun()
                except requests.exceptions.ConnectionError:
                    st.error("無法連線至 API 服務")
                except Exception as e:
                    st.error(f"停止失敗：{e}")


# ─── Page 3: Log 查詢 ─────────────────────────────────────────────────────────
elif page == "Log 查詢":
    st.title("Log 查詢")

    def parse_time(s: str) -> str | None:
        s = s.strip()
        if not s:
            return None
        try:
            dt = datetime.strptime(s, "%Y/%m/%d %H:%M:%S")
            dt_aware = dt.replace(tzinfo=TAIPEI_TZ)
            return dt_aware.isoformat()
        except ValueError:
            return None

    col1, col2 = st.columns(2)
    with col1:
        job_options = {"全部": None, "crawler": "crawler", "api": "api"}
        job_label = st.selectbox("Job", list(job_options.keys()))
        job = job_options[job_label]
    with col2:
        level_options = {"全部": None, "INFO": "INFO", "WARNING": "WARNING", "ERROR": "ERROR"}
        level_label = st.selectbox("等級", list(level_options.keys()))
        level = level_options[level_label]

    now = datetime.now(TAIPEI_TZ)
    default_start = (now - timedelta(hours=1)).strftime("%Y/%m/%d %H:%M:%S")
    default_end = now.strftime("%Y/%m/%d %H:%M:%S")

    col3, col4 = st.columns(2)
    with col3:
        start_str = st.text_input(
            "開始時間",
            value=default_start,
            placeholder="YYYY/MM/DD hh:mm:ss",
            help="格式：YYYY/MM/DD hh:mm:ss，例如 2025/05/16 08:00:00",
        )
    with col4:
        end_str = st.text_input(
            "結束時間",
            value=default_end,
            placeholder="YYYY/MM/DD hh:mm:ss",
            help="格式：YYYY/MM/DD hh:mm:ss，例如 2025/05/16 09:00:00",
        )

    limit = st.number_input("最多筆數", min_value=1, max_value=1000, value=100)

    if st.button("查詢", type="primary"):
        start_iso = parse_time(start_str)
        end_iso = parse_time(end_str)

        if start_str.strip() and start_iso is None:
            st.warning("開始時間格式不正確，請使用 YYYY/MM/DD hh:mm:ss")
        elif end_str.strip() and end_iso is None:
            st.warning("結束時間格式不正確，請使用 YYYY/MM/DD hh:mm:ss")
        else:
            params: dict = {"limit": int(limit)}
            if job:
                params["job"] = job
            if level:
                params["level"] = level
            if start_iso:
                params["start"] = start_iso
            if end_iso:
                params["end"] = end_iso

            with st.spinner("查詢中..."):
                try:
                    r = requests.get(f"{API_URL}/logs", params=params, timeout=15)
                    r.raise_for_status()
                    data = r.json()

                    count = data.get("count", 0)
                    logs = data.get("logs", [])

                    st.metric("查詢結果筆數", count)

                    if logs:
                        df = pd.DataFrame(logs)
                        display_cols = [c for c in ["timestamp", "job", "message"] if c in df.columns]
                        st.dataframe(df[display_cols], use_container_width=True)
                    else:
                        st.info("查無 log 資料")
                except requests.exceptions.ConnectionError:
                    st.error("無法連線至 API 服務，請確認服務是否啟動")
                except Exception as e:
                    st.error(f"查詢失敗：{e}")
