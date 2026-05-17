import logging
import logging_loki
import smtplib
import threading
import time
import os
from email.mime.text import MIMEText
from datetime import datetime

_last_notify_time = 0.0
_notify_lock = threading.Lock()

_notify_log_path = os.path.join(os.getenv("DATA_PATH", "./data/"), "notify.log")
_notify_logger = logging.getLogger("notify")
if not _notify_logger.handlers:
    _handler = logging.FileHandler(_notify_log_path, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _notify_logger.addHandler(_handler)
    _notify_logger.setLevel(logging.INFO)
    _notify_logger.propagate = False


def send_notification(record: logging.LogRecord) -> None:
    smtp_host     = os.getenv("SMTP_HOST", "")
    smtp_port     = int(os.getenv("SMTP_PORT", "587"))
    smtp_user     = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    notify_email  = os.getenv("NOTIFY_EMAIL", "")
    if not all([smtp_host, smtp_user, smtp_password, notify_email]):
        return
    try:
        body = (
            f"時間：{datetime.fromtimestamp(record.created).strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"服務：{record.name}\n"
            f"等級：{record.levelname}\n"
            f"訊息：{record.getMessage()}"
        )
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[爬蟲異常] {record.levelname} - {record.name}"
        msg["From"]    = smtp_user
        msg["To"]      = notify_email
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        _notify_logger.info(f"寄出成功 → {notify_email}｜訊息：{record.getMessage()}")
    except Exception as e:
        _notify_logger.warning(f"寄送失敗：{e}｜訊息：{record.getMessage()}")



class LevelAwareLokiHandler(logging_loki.LokiHandler):
    def emit(self, record: logging.LogRecord) -> None:
        global _last_notify_time
        self.emitter.tags["level"] = record.levelname
        if record.levelno >= logging.ERROR:
            cooldown = int(os.getenv("NOTIFY_COOLDOWN", "300"))
            now = time.time()
            should_notify = False
            with _notify_lock:
                if now - _last_notify_time > cooldown:
                    _last_notify_time = now
                    should_notify = True
            if should_notify:
                threading.Thread(
                    target=send_notification,
                    args=(record,),
                    daemon=True,
                ).start()
        super().emit(record)
