"""个人中心 + 邮件订阅。

路由：
  GET  /me                     个人中心（左侧类别：邮件订阅；后续可加别的类别）
  GET  /me/subscribe           邮件订阅：收件邮箱 + 关注范围管理
  POST /api/sub/email          保存邮箱并发确认信
  GET  /me/confirm?token=…     确认订阅（邮件里的链接）
  GET  /me/unsubscribe?token=… 退订（邮件底部的链接）
  POST /api/subs/add           新增关注范围 {label, query}
  POST /api/subs/<id>/delete   删除
  POST /api/subs/<id>/toggle   启用/停用
  GET  /api/subs/preview       按全量问题的筛选参数预览条数

设计要点：
  · 一个 GitCode 账号绑一个收件邮箱；邮箱要点确认信里的链接才算生效。
  · 每条订阅是一个「关注范围」，参数与全量问题页完全一致（复用 web/issueq.issue_where），
    组之间并集：邮件里按组分别列出，各自数量互不覆盖。
"""
from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime
from urllib.parse import urlencode

from flask import Blueprint, jsonify, redirect, render_template, request

import mailer

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FORMAT = "%Y-%m-%d %H:%M:%S"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sub_users(
  login       TEXT PRIMARY KEY,
  name        TEXT,
  email       TEXT,
  token       TEXT,
  verified_at TEXT,
  active      INTEGER DEFAULT 1,
  created_at  TEXT,
  updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS subs(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  login      TEXT NOT NULL,
  label      TEXT,
  query      TEXT NOT NULL,
  summary    TEXT,
  enabled    INTEGER DEFAULT 1,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS sub_sends(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  login     TEXT,
  email     TEXT,
  sent_at   TEXT,
  groups    INTEGER,
  issues    INTEGER,
  new_today INTEGER,
  ok        INTEGER,
  error     TEXT
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> str:
    return datetime.now().strftime(_FORMAT)


def _params_of(query: str) -> dict:
    """把订阅里存的 query 串解析成 dict（存的是 urlencode 后的串，这里要还原）。"""
    from urllib.parse import unquote
    out: dict = {}
    for kv in (query or "").split("&"):
        if not kv or "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        out[k] = unquote(v)
    return out


def describe(params: dict, section_names: dict, rule_label) -> str:
    """把筛选参数翻译成一句人话，用于订阅列表和邮件标题。"""
    from urllib.parse import unquote

    bits: list[str] = []
    lang = params.get("lang", "")
    if lang == "cn":
        bits.append("中文站")
    elif lang == "en":
        bits.append("英文站")
    section = params.get("section", "")
    if section:
        bits.append(section_names.get(section, section))
    kind = params.get("kind", "")
    if kind == "guide":
        bits.append("仅指南")
    elif kind == "api":
        bits.append("仅 API 参考")
    if params.get("domain"):
        bits.append(unquote(params["domain"]).split("|")[-1])
    if params.get("sub"):
        bits.append(unquote(params["sub"]).split("|")[-1])
    if params.get("kit"):
        parts = unquote(params["kit"]).split("|")
        bits.append(parts[-1] if len(parts) < 3 else parts[2])
    rule = params.get("rule", "")
    if rule:
        bits.append(rule_label(rule))
    # 订阅一律是「未解决」，条件描述里不再重复写状态
    q = params.get("q", "")
    if q:
        bits.append(f"含「{unquote(q)}」")
    return " · ".join(bits) or "全部问题"


def register(app, db_path: str, where_fn, section_names: dict, rule_label, site_url: str) -> None:
    bp = Blueprint("subs", __name__)

    def db() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def me():
        import auth as gcauth
        return gcauth.current_user()

    def _user_row(conn, login: str):
        return conn.execute("SELECT * FROM sub_users WHERE login=?", (login,)).fetchone()

    def _subs_of(conn, login: str):
        return conn.execute("SELECT * FROM subs WHERE login=? ORDER BY id", (login,)).fetchall()

    def _count(params: dict) -> int:
        w, args = where_fn(params)
        conn = db()
        try:
            return conn.execute(f"SELECT COUNT(*) n FROM issues i WHERE {w}", args).fetchone()["n"]
        finally:
            conn.close()

    def _new_today(params: dict) -> int:
        p = dict(params)
        p["dfrom"] = p["dto"] = datetime.now().strftime("%Y-%m-%d")
        return _count(p)

    def _context(tab: str):
        u = me()
        conn = db()
        try:
            row = _user_row(conn, u["login"])
            subs = _subs_of(conn, u["login"]) if row else []
            last = conn.execute(
                "SELECT sent_at, ok FROM sub_sends WHERE login=? ORDER BY id DESC LIMIT 1",
                (u["login"],)).fetchone()
        finally:
            conn.close()
        groups = []
        for s in subs:
            p = _params_of(s["query"])
            groups.append({
                "id": s["id"], "label": s["label"] or "未命名",
                "summary": s["summary"] or describe(p, section_names, rule_label),
                "enabled": bool(s["enabled"]),
                "total": _count(p), "new": _new_today(p),
                "query": s["query"],
            })
        return {
            "tab": tab, "user": u,
            "email": (row["email"] if row else "") or "",
            "verified": bool(row and row["verified_at"]),
            "active": bool(row["active"]) if row else True,
            "groups": groups,
            "email_state": ("none" if not (row and row["email"]) else
                            "unsub" if not row["active"] else
                            "verified" if row["verified_at"] else "pending"),
            "last_send": (last["sent_at"] if last else ""),
            "last_ok": bool(last["ok"]) if last else None,
            "site": site_url,
        }

    def _require_login():
        u = me()
        if not u:
            nxt = request.full_path.rstrip("?") if request.query_string else request.path
            return redirect("/auth/login?next=" + nxt)
        return None

    # ── 页面 ──────────────────────────────────────────────────────
    @bp.get("/me")
    def me_home():
        r = _require_login()
        if r:
            return r
        return render_template("me.html", **_context("home"))

    @bp.get("/me/subscribe")
    def me_subscribe():
        r = _require_login()
        if r:
            return r
        return render_template("me.html", **_context("subscribe"))

    # ── 邮箱 ──────────────────────────────────────────────────────
    @bp.post("/api/sub/email")
    def api_set_email():
        u = me()
        if not u:
            return jsonify({"ok": False, "error": "请先登录"}), 403
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip().lower()
        if not _EMAIL_RE.match(email):
            return jsonify({"ok": False, "error": "邮箱格式不正确"}), 400
        conn = db()
        try:
            row = _user_row(conn, u["login"])
            token = secrets.token_urlsafe(24)
            if row:
                changed = (row["email"] or "") != email
                conn.execute(
                    "UPDATE sub_users SET name=?, email=?, token=?, verified_at=?, active=1,"
                    " updated_at=? WHERE login=?",
                    (u.get("name") or u["login"], email, token,
                     None if changed else row["verified_at"], _now(), u["login"]))
            else:
                conn.execute(
                    "INSERT INTO sub_users(login,name,email,token,verified_at,active,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,1,?,?)",
                    (u["login"], u.get("name") or u["login"], email, token, None, _now(), _now()))
            conn.commit()
            verified = bool(conn.execute(
                "SELECT verified_at FROM sub_users WHERE login=?", (u["login"],)).fetchone()["verified_at"])
        finally:
            conn.close()
        if verified:
            return jsonify({"ok": True, "verified": True, "msg": "邮箱已保存（之前已确认过，立即生效）"})

        link = f"{site_url}/me/confirm?token={token}"
        html = _confirm_mail_html(u.get("name") or u["login"], link, site_url)
        try:
            mailer.send_mail(email, "确认订阅：开发者文档质量日报", html)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"邮箱已保存，但确认信发送失败：{e}"}), 500
        return jsonify({"ok": True, "verified": False,
                        "msg": f"确认信已发到 {email}，点邮件里的按钮即可生效"})

    @bp.post("/api/sub/email/unbind")
    def api_unbind_email():
        """取消邮箱绑定：清空邮箱、作废确认令牌、置为未启用；关注范围保留。"""
        u = me()
        if not u:
            return jsonify({"ok": False, "error": "请先登录"}), 403
        conn = db()
        try:
            row = _user_row(conn, u["login"])
            if not row or not (row["email"] or "").strip():
                return jsonify({"ok": False, "error": "还没有绑定邮箱"}), 400
            old = row["email"]
            conn.execute(
                "UPDATE sub_users SET email='', verified_at=NULL, active=0, token=?, updated_at=?"
                " WHERE login=?", (secrets.token_urlsafe(24), _now(), u["login"]))
            conn.commit()
        finally:
            conn.close()
        return jsonify({"ok": True, "msg": f"已取消绑定 {old}，关注范围保留但不再发送邮件"})

    @bp.get("/me/confirm")
    def me_confirm():
        token = request.args.get("token", "")
        conn = db()
        try:
            row = conn.execute("SELECT * FROM sub_users WHERE token=?", (token,)).fetchone() if token else None
            if not row:
                return _plain("确认链接无效或已过期。请回个人中心重新发送确认信。")
            conn.execute("UPDATE sub_users SET verified_at=?, active=1, updated_at=? WHERE login=?",
                         (_now(), _now(), row["login"]))
            conn.commit()
            return _plain(f"订阅已生效：{row['email']}<br>每天早上 8:00 会收到日报，可随时在个人中心退订。", ok=True)
        finally:
            conn.close()

    @bp.get("/me/unsubscribe")
    def me_unsubscribe():
        token = request.args.get("token", "")
        conn = db()
        try:
            row = conn.execute("SELECT * FROM sub_users WHERE token=?", (token,)).fetchone() if token else None
            if not row:
                return _plain("退订链接无效。")
            conn.execute("UPDATE sub_users SET active=0, updated_at=? WHERE login=?", (_now(), row["login"]))
            conn.commit()
            return _plain(f"已退订：{row['email']} 不会再收到日报。需要时可在个人中心重新启用。", ok=True)
        finally:
            conn.close()

    # ── 关注范围 ────────────────────────────────────────────────
    @bp.get("/api/subs/preview")
    def api_preview():
        if not me():
            return jsonify({"ok": False, "error": "请先登录"}), 403
        params = {k: v for k, v in request.args.items() if k in
                  ("lang", "section", "kind", "sub", "domain", "kit", "rule", "status", "q")}
        try:
            total = _count(params)
            new = _new_today(params)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 400
        return jsonify({"ok": True, "total": total, "new": new,
                        "summary": describe(params, section_names, rule_label)})

    @bp.post("/api/subs/add")
    def api_add():
        u = me()
        if not u:
            return jsonify({"ok": False, "error": "请先登录"}), 403
        data = request.get_json(silent=True) or {}
        params = data.get("params") or {}
        params = {k: v for k, v in params.items() if k in
                  ("lang", "section", "kind", "sub", "domain", "kit", "rule", "status", "q") and v}
        params.setdefault("status", "open")
        query = urlencode(params)
        total = _count(params)
        if total <= 0:
            return jsonify({"ok": False, "error": "这个关注范围下没有问题，不用关注"}), 400
        label = (data.get("label") or "").strip() or describe(params, section_names, rule_label)
        conn = db()
        try:
            n = conn.execute("SELECT COUNT(*) n FROM subs WHERE login=?", (u["login"],)).fetchone()["n"]
            if n >= 20:
                return jsonify({"ok": False, "error": "最多 20 条订阅，先删掉不用的"}), 400
            dup = conn.execute("SELECT id FROM subs WHERE login=? AND query=?", (u["login"], query)).fetchone()
            if dup:
                return jsonify({"ok": False, "error": "这个关注范围已经存在"}), 400
            conn.execute("INSERT INTO subs(login,label,query,summary,enabled,created_at) VALUES(?,?,?,?,1,?)",
                         (u["login"], label, query,
                          describe(params, section_names, rule_label), _now()))
            conn.commit()
            return jsonify({"ok": True, "total": total})
        finally:
            conn.close()

    @bp.post("/api/subs/<int:sid>/delete")
    def api_delete(sid: int):
        u = me()
        if not u:
            return jsonify({"ok": False, "error": "请先登录"}), 403
        conn = db()
        try:
            n = conn.execute("DELETE FROM subs WHERE id=? AND login=?", (sid, u["login"])).rowcount
            conn.commit()
            return jsonify({"ok": bool(n)})
        finally:
            conn.close()

    @bp.post("/api/subs/<int:sid>/toggle")
    def api_toggle(sid: int):
        u = me()
        if not u:
            return jsonify({"ok": False, "error": "请先登录"}), 403
        data = request.get_json(silent=True) or {}
        enabled = 1 if data.get("enabled") else 0
        conn = db()
        try:
            n = conn.execute("UPDATE subs SET enabled=? WHERE id=? AND login=?",
                             (enabled, sid, u["login"])).rowcount
            conn.commit()
            return jsonify({"ok": bool(n)})
        finally:
            conn.close()

    app.register_blueprint(bp)


def _plain(msg: str, ok: bool = False):
    color = "#1a7f37" if ok else "#d92d20"
    return (f"<div style='max-width:520px;margin:80px auto;font:15px/1.8 -apple-system,"
            f"\"PingFang SC\",\"Microsoft YaHei\",sans-serif;color:#1f2430'>"
            f"<p style='color:{color}'>{msg}</p>"
            f"<p><a href='https://docstor.openharmony.cool/me' style='color:#2a6df4'>回个人中心</a></p></div>")


def _confirm_mail_html(name: str, link: str, site: str) -> str:
    """确认信：纯表格布局 + 内联样式，兼容企邮/Outlook。"""
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px;background:#f6f7f9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f7f9;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border:1px solid #e4e7ec;border-radius:12px;">
  <tr><td style="padding:26px 28px 8px;font:16px/1.5 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2430;">
    <b>确认订阅「开发者文档质量日报」</b>
  </td></tr>
  <tr><td style="padding:6px 28px 0;font:14px/1.8 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#4a5160;">
    {name}，你好：<br>
    你正在订阅开发者文档质量日报。确认后，每天早上 <b>8:00</b> 会按你设置的关注范围，
    把问题数量、新增明细和当天文档变更发到这个邮箱。
  </td></tr>
  <tr><td style="padding:22px 28px 4px;">
    <a href="{link}" style="display:inline-block;padding:11px 22px;background:#2a6df4;color:#ffffff;
       text-decoration:none;border-radius:8px;font:14px/1 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
       确认订阅</a>
  </td></tr>
  <tr><td style="padding:14px 28px 26px;font:12.5px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#9aa3b2;">
    按钮点不开时，把下面地址复制到浏览器：<br>
    <span style="color:#2a6df4;word-break:break-all;">{link}</span><br><br>
    不是本人操作？忽略这封邮件即可，不会生效。<br>
    <a href="{site}/me" style="color:#9aa3b2;">{site}/me</a>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
