"""SMTP 邮件发送（订阅日报）。

配置从项目根目录 .smtp.env 读取（600、git-ignored，值不要写进代码）：

    SMTP_HOST=smtp.exmail.qq.com
    SMTP_PORT=465
    SMTP_USER=noreply@openharmony.cool
    SMTP_PASS=<授权码>
    MAIL_FROM=noreply@openharmony.cool
"""
from __future__ import annotations

import smtplib
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".smtp.env"
DEFAULT_FROM_NAME = "华为文档质量日报"


def _load_config() -> dict:
    cfg: dict = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def is_configured() -> bool:
    cfg = _load_config()
    return bool(cfg.get("SMTP_HOST") and cfg.get("SMTP_USER") and cfg.get("SMTP_PASS"))


def send_mail(to: str, subject: str, body_html: str, from_name: str = "") -> None:
    cfg = _load_config()
    host = cfg.get("SMTP_HOST")
    port = int(cfg.get("SMTP_PORT") or 465)
    user = cfg.get("SMTP_USER")
    pwd = cfg.get("SMTP_PASS")
    fr = cfg.get("MAIL_FROM") or user
    if not (host and user and pwd):
        raise RuntimeError("SMTP 未配置：请在 .smtp.env 填写 SMTP_HOST / SMTP_USER / SMTP_PASS")

    msg = MIMEText(body_html, "html", "utf-8")
    msg["From"] = formataddr((str(Header(from_name or DEFAULT_FROM_NAME, "utf-8")), str(fr)))
    msg["To"] = to
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800")
    msg["X-Mailer"] = "docstor-subscriber"

    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(fr or user, [to], msg.as_string())


def send_bulk(to_list: list[str], subject: str, body_html: str) -> dict:
    """批量发送同一封内容，返回 {ok: [...], failed: [(邮箱, 错误)]}。"""
    ok, failed = [], []
    for to in to_list:
        try:
            send_mail(to, subject, body_html)
            ok.append(to)
        except Exception as e:  # noqa: BLE001
            failed.append((to, str(e)))
    return {"ok": ok, "failed": failed}
