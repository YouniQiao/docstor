"""SMTP 邮件发送（订阅日报）。

配置从项目根目录 .smtp.env 读取（600、git-ignored，值不要写进代码）：

    SMTP_HOST=smtp.exmail.qq.com
    SMTP_PORT=465
    SMTP_USER=noreply@openharmony.cool
    SMTP_PASS=<授权码>
    MAIL_FROM=noreply@openharmony.cool

发信要点（踩过的坑）：
- 发件人显示名**不要带「华为」**：从非 huawei.com 域发、显示名却声称华为，容易被企业的
  反钓鱼网关判为仿冒而静默隔离（docscheck 用的是「OpenHarmony 文档检查」，同一账号能正常送达）。
- 正文要给纯文本副本（multipart/alternative）：只有 text/html 的群发邮件更容易被判垃圾。
- 带 List-Unsubscribe（以及 -Post）头，表明是订阅邮件而不是营销轰炸。
"""
from __future__ import annotations

import re
import smtplib
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".smtp.env"
DEFAULT_FROM_NAME = "OpenHarmony 文档质量日报"


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


def html_to_text(body_html: str) -> str:
    """粗略地把 HTML 转成纯文本（够用：去掉样式/标签、链接留文字、压掉空行）。"""
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", body_html)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</(tr|div|p|h\d|table)>", "\n", s)
    s = re.sub(r"(?is)</t[dh]>", " | ", s)
    s = re.sub(r"(?is)<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"\2 <\1>", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return "\n".join(x.strip(" |") for x in s.splitlines()).strip()


def send_mail(to: str, subject: str, body_html: str, from_name: str = "",
              unsubscribe_url: str = "", reply_to: str = "") -> None:
    cfg = _load_config()
    host = cfg.get("SMTP_HOST")
    port = int(cfg.get("SMTP_PORT") or 465)
    user = cfg.get("SMTP_USER")
    pwd = cfg.get("SMTP_PASS")
    fr = cfg.get("MAIL_FROM") or user
    if not (host and user and pwd):
        raise RuntimeError("SMTP 未配置：请在 .smtp.env 填写 SMTP_HOST / SMTP_USER / SMTP_PASS")

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((str(Header(from_name or DEFAULT_FROM_NAME, "utf-8")), str(fr)))
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
    msg["Subject"] = Header(subject, "utf-8")
    msg["Date"] = datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0800")
    msg["X-Mailer"] = "docstor-subscriber"
    if unsubscribe_url:
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    msg.attach(MIMEText(html_to_text(body_html), "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

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
