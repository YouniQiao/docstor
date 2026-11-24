"""问题反馈：公开提交、公开列表、本人可删、邮件通知维护者。

路由：
  GET  /feedback                    反馈页（提交表单 + 公开列表）
  GET  /api/feedback                列表（分页 + 类型/状态筛选 + 统计）
  POST /api/feedback                提交一条反馈，返回删除码
  POST /api/feedback/<id>/delete    删除（删除码 / 本人登录 / 管理员）
  POST /api/feedback/<id>/handle    管理员：标记已处理、写公开回复

设计要点（对同事友好优先）：
  · 不要求登录：填 5 个字就能提，称呼和联系方式都可留空（匿名）。
  · 提交后立刻给一个删除码，同时浏览器本地记住 —— 同一浏览器里点「删除」即可；
    换设备/清缓存后用删除码删。登录 GitCode 的账号可以跨设备直接删自己的。
  · 所有内容公开可见（称呼公开、联系方式不公开）；维护者的回复也公开，形成闭环。
  · 每条新反馈即时发一封邮件给维护者；发信失败只记日志，绝不影响提交本身。
  · 防刷：同 IP 10 分钟最多 5 条、同内容 10 分钟内不重复入库。
"""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request

import mailer

# 反馈范围：只针对「检查」本身（加一条检查项 / 某条检查结果判错了），不接文档内容质量问题
KINDS = {"rule_new": "新增检查项", "rule_wrong": "检查结果有误", "other": "其它"}
STATUSES = {"pending": "待处理", "done": "已处理"}
FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_CONTENT, MIN_CONTENT = 2000, 5
MAX_PAGE, MAX_NAME, MAX_CONTACT = 200, 30, 100
RATE_N, RATE_WINDOW = 5, 600          # 同 IP：10 分钟 5 条
DUP_WINDOW = 600                      # 同 IP 同内容 10 分钟内算重复

SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  kind       TEXT NOT NULL DEFAULT 'issue',
  page       TEXT DEFAULT '',
  page_title TEXT DEFAULT '',
  content    TEXT NOT NULL,
  name       TEXT DEFAULT '',
  contact    TEXT DEFAULT '',
  login      TEXT DEFAULT '',
  ip_hash    TEXT DEFAULT '',
  ua         TEXT DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'pending',
  reply      TEXT DEFAULT '',
  replied_at TEXT,
  handled_by TEXT DEFAULT '',
  del_token  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feedback_created ON feedback(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_feedback_login ON feedback(login);
"""

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_SLUG_RE = re.compile(r"^(cn|en)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)$")
_RATE: dict[str, list[float]] = {}     # 进程内限速（4 worker 下约为 4 倍粒度，够用）


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now().strftime(FORMAT)


def _clean(s: str, limit: int) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u200b", "")).strip()[:limit]


def _body(s: str) -> str:
    """正文：保留换行，去掉不可见字符与超长。"""
    s = (s or "").replace("\r\n", "\n").replace("\u200b", "")
    lines = [ln.rstrip() for ln in s.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)[:MAX_CONTENT]


def _rate_ok(ip: str) -> bool:
    now = time.time()
    ts = [t for t in _RATE.get(ip, []) if now - t < RATE_WINDOW]
    if len(ts) >= RATE_N:
        _RATE[ip] = ts
        return False
    ts.append(now)
    _RATE[ip] = ts
    return True


def _ip_hash(ip: str, key: bytes) -> str:
    return hmac.new(key, ip.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _page_link(page: str) -> str:
    """把 'cn/capabilities/arkts-xxx' 转成站点路由；外链原样；其它当纯文本。"""
    p = (page or "").strip()
    if not p:
        return ""
    if p.startswith("http://") or p.startswith("https://"):
        return p if p.startswith("https://") else ""
    m = _SLUG_RE.match(p)
    if m:
        return f"/#/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return ""


def _row_out(r: sqlite3.Row, login: str, admin: bool) -> dict:
    mine = bool(login) and r["login"] == login
    return {
        "id": r["id"],
        "created_at": r["created_at"][:16],
        "kind": r["kind"],
        "kind_label": KINDS.get(r["kind"], r["kind"]),
        "page": r["page"] or "",
        "page_title": r["page_title"] or "",
        "page_link": _page_link(r["page"] or ""),
        "content": r["content"],
        "name": r["name"] or "",
        "status": r["status"],
        "status_label": STATUSES.get(r["status"], r["status"]),
        "reply": r["reply"] or "",
        "replied_at": (r["replied_at"] or "")[:16],
        "mine": mine,
        "can_delete": bool(mine or admin),
    }


def _esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def _mail_html(item: dict, site: str, kind_label: str) -> str:
    link = f"{site}/feedback?item={item['id']}"
    page = item["page"] or "—"
    if item["page_link"]:
        page = f'<a href="{_esc(site + item["page_link"])}" style="color:#2f6fed">{_esc(item["page"] or "")}</a>'
    rows = [
        ("类型", kind_label),
        ("页面", page),
        ("称呼", item["name"] or "匿名"),
        ("联系方式", item["contact"] or "—"),
        ("时间", item["created_at"]),
        ("编号", f"#{item['id']}"),
    ]
    tr = "".join(
        f'<tr><td style="padding:6px 10px;background:#f7f8fa;border:1px solid #e6e8eb;'
        f'color:#6b7280;font-size:13px;white-space:nowrap">{_esc(k)}</td>'
        f'<td style="padding:6px 10px;border:1px solid #e6e8eb;font-size:13px">{v if k == "页面" else _esc(v)}</td></tr>'
        for k, v in rows)
    body = _esc(item["content"]).replace("\n", "<br>")
    return (
        '<div style="font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        'color:#1f2430;max-width:640px">'
        f'<h3 style="margin:0 0 12px;font-size:16px">收到一条{_esc(kind_label)}</h3>'
        f'<table style="border-collapse:collapse;width:100%;margin-bottom:14px">{tr}</table>'
        '<div style="font-size:13px;color:#6b7280;margin-bottom:6px">反馈内容</div>'
        f'<div style="border:1px solid #e6e8eb;border-radius:8px;padding:12px;font-size:14px;'
        f'line-height:1.7;background:#fff">{body}</div>'
        f'<p style="margin:16px 0 0;font-size:13px"><a href="{_esc(link)}" '
        'style="color:#2f6fed">在站点上查看 / 回复 / 处理 →</a></p>'
        '<p style="margin:6px 0 0;font-size:12px;color:#9aa3b2">'
        '这封邮件由「问题反馈」页自动发送；直接在站点上回复即可公开答复，不用回邮件。</p>'
        '</div>'
    )


def _reply_mail_html(row: dict, reply: str, site: str) -> str:
    """回复通知：给留了邮箱的提交人发一封，附原文与回复。"""
    link = f"{site}/feedback?item={row['id']}"
    kind = KINDS.get(row["kind"], row["kind"])
    orig = _esc(row["content"]).replace("\n", "<br>")
    rep = _esc(reply).replace("\n", "<br>")
    return (
        '<div style="font-family:-apple-system,\'PingFang SC\',\'Microsoft YaHei\',sans-serif;'
        'color:#1f2430;max-width:640px">'
        f'<h3 style="margin:0 0 6px;font-size:16px">你的{_esc(kind)}反馈收到回复了</h3>'
        '<p style="margin:0 0 14px;font-size:13px;color:#6b7280">'
        f'编号 #{row["id"]} · {_esc(row["created_at"][:16])}</p>'
        '<div style="font-size:13px;color:#6b7280;margin-bottom:6px">你提的内容</div>'
        '<div style="border:1px solid #e6e8eb;border-radius:8px;padding:10px 12px;font-size:13.5px;'
        'line-height:1.7;background:#fafbfc;color:#4b5563;margin-bottom:14px">'
        f'{orig}</div>'
        '<div style="font-size:13px;color:#6b7280;margin-bottom:6px">维护者回复</div>'
        '<div style="border:1px solid #cddcff;border-left:3px solid #2f6fed;border-radius:0 8px 8px 0;'
        'padding:10px 12px;font-size:14px;line-height:1.75;background:#f5f9ff">'
        f'{rep}</div>'
        f'<p style="margin:16px 0 0;font-size:13px"><a href="{_esc(link)}" '
        'style="color:#2f6fed">在站点上查看 →</a></p>'
        '<p style="margin:6px 0 0;font-size:12px;color:#9aa3b2">'
        '这是自动发出的通知，直接回邮件不会有人看到；后续讨论请在站点那条反馈下面跟进。</p>'
        '</div>'
    )


def register(app, db_path: str, site_url: str, notify_to: str, log_path: Path, secret: bytes) -> None:
    bp = Blueprint("feedback", __name__)

    def db() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _login() -> str:
        import auth as gcauth
        u = gcauth.current_user() or {}
        return u.get("login") or ""

    def _admin() -> bool:
        from flask import session
        return bool(session.get("admin"))

    def _log(msg: str) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"[{_now()}] {msg}\n")
        except Exception:  # noqa: BLE001
            pass

    def _ip() -> str:
        return (request.headers.get("X-Real-IP") or request.remote_addr or "").strip()

    @bp.get("/feedback")
    def page():
        import auth as gcauth
        u = gcauth.current_user() or {}
        return render_template("feedback.html", is_admin_user=_admin(),
                               logged_in=bool(u.get("login")),
                               me_name=(u.get("name") or u.get("login") or ""))

    @bp.get("/api/feedback")
    def api_list():
        def int_arg(k, d, lo, hi):
            try:
                return max(lo, min(hi, int(request.args.get(k, d))))
            except (TypeError, ValueError):
                return d

        p = int_arg("page", 1, 1, 100000)
        size = int_arg("size", 10, 1, 50)
        item = int_arg("item", 0, 0, 10 ** 9)     # 邮件/分享链接里的「直达某条」
        kind = (request.args.get("kind") or "all").strip()
        status = (request.args.get("status") or "all").strip()
        where, args = [], []
        if kind in KINDS:
            where.append("kind=?")
            args.append(kind)
        if status in STATUSES:
            where.append("status=?")
            args.append(status)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        conn = db()
        focus = 0
        try:
            # 直达：按「比它新的条数」算出它在当前筛选下的页，省得前端翻页找
            if item:
                exists = conn.execute("SELECT 1 FROM feedback WHERE id=?", (item,)).fetchone()
                if exists:
                    w2 = (w + " AND id > ?") if w else " WHERE id > ?"
                    newer = conn.execute(f"SELECT COUNT(*) n FROM feedback{w2}",
                                         args + [item]).fetchone()["n"]
                    p = newer // size + 1
                    focus = item
            total = conn.execute(f"SELECT COUNT(*) n FROM feedback{w}", args).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM feedback{w} ORDER BY id DESC LIMIT ? OFFSET ?",
                args + [size, (p - 1) * size]).fetchall()
            stats = {"all": 0, "pending": 0, "done": 0, "by_kind": {}}
            for r in conn.execute("SELECT kind, status, COUNT(*) n FROM feedback GROUP BY kind, status"):
                stats["all"] += r["n"]
                stats[r["status"]] = stats.get(r["status"], 0) + r["n"]
                stats["by_kind"][r["kind"]] = stats["by_kind"].get(r["kind"], 0) + r["n"]
        finally:
            conn.close()
        return jsonify({"ok": True, "total": total, "page": p, "size": size, "focus": focus,
                        "items": [_row_out(r, _login(), _admin()) for r in rows],
                        "stats": stats, "kinds": KINDS})

    @bp.post("/api/feedback")
    def api_create():
        login = _login()
        if not login:
            return jsonify({"ok": False, "need_login": True,
                            "error": "反馈需要先登录 GitCode 账号"}), 401
        ip = _ip()
        if ip and not _rate_ok(ip):
            return jsonify({"ok": False, "error": "提交太频繁了，请过几分钟再试"}), 429
        data = request.get_json(silent=True) or request.form
        kind = (data.get("kind") or "rule_new").strip()
        if kind not in KINDS:
            kind = "other"
        content = _body(data.get("content") or "")
        if len(re.sub(r"\s+", "", content)) < MIN_CONTENT:
            return jsonify({"ok": False, "error": f"内容太短了，至少写 {MIN_CONTENT} 个字"}), 400
        if (data.get("_hp") or "").strip():      # 蜜罐：正常用户看不到这个字段
            return jsonify({"ok": False, "error": "提交失败，请刷新页面重试"}), 400
        page = _clean(data.get("page") or "", MAX_PAGE)
        page_title = _clean(data.get("page_title") or "", 120)
        name = _clean(data.get("name") or "", MAX_NAME)
        contact = _clean(data.get("contact") or "", MAX_CONTACT)
        ih = _ip_hash(ip, secret) if ip else ""
        token = secrets.token_urlsafe(9)
        now = _now()
        conn = db()
        try:
            if ih:
                dup = conn.execute(
                    "SELECT id FROM feedback WHERE ip_hash=? AND content=? "
                    "AND created_at > datetime('now','localtime',?) ORDER BY id DESC LIMIT 1",
                    (ih, content, f"-{DUP_WINDOW} seconds")).fetchone()
                if dup:
                    return jsonify({"ok": False,
                                    "error": "这条内容刚刚已经提交过了，不用重复提交"}), 409
            cur = conn.execute(
                "INSERT INTO feedback(created_at,kind,page,page_title,content,name,contact,"
                "login,ip_hash,ua,status,del_token) VALUES(?,?,?,?,?,?,?,?,?,?, 'pending', ?)",
                (now, kind, page, page_title, content, name, contact, login, ih,
                 (request.headers.get("User-Agent") or "")[:300], token))
            conn.commit()
            fid = cur.lastrowid
        finally:
            conn.close()

        code = f"FB{fid}-{token}"
        # 邮件通知（失败不影响提交）
        try:
            item = {"id": fid, "created_at": now, "page": page, "page_link": _page_link(page),
                    "name": name, "contact": contact, "content": content}
            subject = f"[问题反馈] {KINDS[kind]}｜{content[:24]}"
            mailer.send_mail(
                to=notify_to, subject=subject, body_html=_mail_html(item, site_url, KINDS[kind]),
                from_name="开发者文档质量日报",
                reply_to=(contact if _EMAIL_RE.match(contact or "") else ""))
            _log(f"#{fid} {kind} 已发信 → {notify_to}")
        except Exception as e:  # noqa: BLE001
            _log(f"#{fid} 发信失败：{type(e).__name__}: {e}")
        return jsonify({"ok": True, "id": fid, "code": code})

    @bp.post("/api/feedback/<int:fid>/delete")
    def api_delete(fid: int):
        data = request.get_json(silent=True) or request.form
        code = (data.get("code") or "").strip()
        admin = _admin()
        login = _login()
        conn = db()
        try:
            row = conn.execute("SELECT id, login, del_token FROM feedback WHERE id=?", (fid,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "这条反馈已经不在了"}), 404
            ok = admin or (login and row["login"] == login)
            if not ok and code:
                try:
                    cid, ctok = code.lstrip("FBfb").split("-", 1)
                    ok = int(cid) == fid and hmac.compare_digest(ctok, row["del_token"])
                except (ValueError, AttributeError):
                    ok = False
            if not ok:
                return jsonify({"ok": False, "error": "没有权限删除这条反馈"}), 403
            conn.execute("DELETE FROM feedback WHERE id=?", (fid,))
            conn.commit()
        finally:
            conn.close()
        _log(f"#{fid} 已删除（{'管理员' if admin else login or '删除码'}）")
        return jsonify({"ok": True})

    @bp.post("/api/feedback/<int:fid>/handle")
    def api_handle(fid: int):
        if not _admin():
            return jsonify({"ok": False, "error": "需要管理员权限"}), 403
        data = request.get_json(silent=True) or request.form
        status = (data.get("status") or "").strip()
        reply = (data.get("reply") or "").strip()[:1000]
        if status and status not in STATUSES:
            return jsonify({"ok": False, "error": "状态不正确"}), 400
        conn = db()
        try:
            row = conn.execute(
                "SELECT id, created_at, kind, content, contact, reply FROM feedback WHERE id=?",
                (fid,)).fetchone()
            if not row:
                return jsonify({"ok": False, "error": "这条反馈已经不在了"}), 404
            old_reply = (row["reply"] or "").strip()
            if status:
                conn.execute("UPDATE feedback SET status=?, handled_by=? WHERE id=?",
                             (status, "管理员", fid))
            if reply or (data.get("reply") is not None and reply == ""):
                conn.execute("UPDATE feedback SET reply=?, replied_at=? WHERE id=?",
                             (reply, _now() if reply else None, fid))
            conn.commit()
        finally:
            conn.close()

        # 回复有变化且提交人留了邮箱 → 给他发一封「已回复」通知（失败不影响保存）
        if reply and reply != old_reply and _EMAIL_RE.match((row["contact"] or "").strip()):
            try:
                mailer.send_mail(
                    to=row["contact"].strip(),
                    subject=f"回复：你的{KINDS.get(row['kind'], '')}反馈（#{fid}）",
                    body_html=_reply_mail_html(dict(row), reply, site_url),
                    from_name="开发者文档质量日报")
                _log(f"#{fid} 回复已通知 → {row['contact'].strip()}")
            except Exception as e:  # noqa: BLE001
                _log(f"#{fid} 回复通知失败：{type(e).__name__}: {e}")
        return jsonify({"ok": True})

    app.register_blueprint(bp)


# 供 app.py 直接取（避免在 app 里再算一遍）
def public_code(fid: int, token: str) -> str:
    return f"FB{fid}-{token}"
