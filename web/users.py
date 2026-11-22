"""站点管理 · 用户管理（只读视图 + 两个安全写操作）。

数据来源三张表合流：sub_users（订阅账号）/ user_login（登录日志，90 天）/ issues.ignored_by（忽略审计）。
写操作只有两个：停用/恢复收信（active）、解绑邮箱（保留关注范围），都不删数据。
"""
from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import unquote_plus

LOGIN_TTL_DAYS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_login(
  id    INTEGER PRIMARY KEY AUTOINCREMENT,
  login TEXT NOT NULL,
  name  TEXT,
  ts    TEXT NOT NULL,
  ip    TEXT,
  ua    TEXT
);
CREATE INDEX IF NOT EXISTS ix_user_login_login ON user_login(login, ts);
CREATE INDEX IF NOT EXISTS ix_user_login_ts ON user_login(ts);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def log_login(conn: sqlite3.Connection, login: str, name: str, ip: str = "", ua: str = "") -> None:
    """GitCode 登录成功后记一条；顺手清掉 90 天前的日志（每次一行，代价可忽略）。"""
    try:
        ensure_schema(conn)
        conn.execute("INSERT INTO user_login(login,name,ts,ip,ua) VALUES(?,?,?,?,?)",
                     (login, name or "", _now(), ip or "", (ua or "")[:200]))
        cutoff = (datetime.now() - timedelta(days=LOGIN_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("DELETE FROM user_login WHERE ts < ?", (cutoff,))
        conn.commit()
    except Exception:  # noqa: BLE001  登录日志绝不该把登录流程搞挂
        pass


def mask_email(mail: str) -> str:
    m = (mail or "").strip()
    if "@" not in m:
        return m
    local, domain = m.split("@", 1)
    keep = local[:3] if len(local) > 3 else local[:1]
    return f"{keep}***@{domain}"


def _params_of(query: str) -> dict:
    out: dict = {}
    for kv in (query or "").split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = unquote_plus(v)
    return out


# ---------------- 用户列表 ----------------
def list_users(conn: sqlite3.Connection, q: str = "", page: int = 0, size: int = 20) -> dict:
    conn.row_factory = sqlite3.Row
    logins: dict[str, dict] = {}

    def touch(login: str) -> dict:
        if not login:
            login = "(匿名)"
        return logins.setdefault(login, {
            "login": login, "name": "", "email": "", "verified": 0, "active": 0,
            "subs": 0, "subs_on": 0, "last_login": "", "logins_30d": 0,
            "sends_ok": 0, "last_send": "", "last_send_ok": None, "last_send_err": "",
            "ignores": 0, "last_ignore": "",
        })

    for r in conn.execute("SELECT login,name,email,verified_at,active,created_at FROM sub_users"):
        u = touch(r["login"])
        u["name"] = r["name"] or u["name"]
        u["email"] = r["email"] or ""
        u["verified"] = 1 if r["verified_at"] else 0
        u["active"] = 1 if r["active"] else 0
        u["created_at"] = r["created_at"]
    for r in conn.execute("SELECT login, COUNT(*) n, MAX(ts) last_ FROM user_login "
                          "WHERE ts >= date('now','-30 days') GROUP BY login"):
        u = touch(r["login"])
        u["logins_30d"] = r["n"]
        u["last_login"] = r["last_"] or ""
    for r in conn.execute("SELECT login, MAX(ts) last_ FROM user_login GROUP BY login"):
        u = touch(r["login"])
        if (r["last_"] or "") > u["last_login"]:
            u["last_login"] = r["last_"]
    for r in conn.execute("SELECT login, name FROM user_login WHERE name<>'' "
                          "AND id IN (SELECT MAX(id) FROM user_login GROUP BY login)"):
        u = touch(r["login"])
        if not u["name"]:
            u["name"] = r["name"]
    for r in conn.execute("SELECT login, COUNT(*) n, SUM(enabled=1) on_ FROM subs GROUP BY login"):
        u = touch(r["login"])
        u["subs"] = r["n"]
        u["subs_on"] = r["on_"] or 0
    for r in conn.execute("SELECT login, MAX(sent_at) last_, SUM(ok=1) ok_ FROM sub_sends GROUP BY login"):
        u = touch(r["login"])
        u["last_send"] = r["last_"] or ""
        u["sends_ok"] = r["ok_"] or 0
    for r in conn.execute("SELECT login, sent_at, ok, error FROM sub_sends "
                          "WHERE id IN (SELECT MAX(id) FROM sub_sends GROUP BY login)"):
        u = touch(r["login"])
        u["last_send_ok"] = bool(r["ok"])
        u["last_send_err"] = (r["error"] or "")[:120]
    for r in conn.execute("SELECT ignored_by, COUNT(*) n, MAX(ignored_at) last_ FROM issues "
                          "WHERE ignored_by IS NOT NULL AND ignored_by<>'' GROUP BY ignored_by"):
        u = touch(r["ignored_by"])
        u["ignores"] = r["n"]
        u["last_ignore"] = r["last_"] or ""

    # 以 __ 开头的是内部探针（如早期发信测试的 __probe__），不算用户
    rows = [u for u in logins.values() if not u["login"].startswith("__")]
    if q:
        ql = q.strip().lower()
        rows = [u for u in rows
                if ql in u["login"].lower() or ql in (u["name"] or "").lower()
                or ql in (u["email"] or "").lower()]
    rows.sort(key=lambda u: (u["last_login"] or "", u["last_send"] or ""), reverse=True)
    total = len(rows)
    rows = rows[page * size:(page + 1) * size]
    for u in rows:
        u["email_masked"] = mask_email(u["email"])
        u["state"] = ("未绑定邮箱" if not u["email"] else
                      ("已停用" if not u["active"] else
                       ("已确认" if u["verified"] else "待确认")))
    return {"rows": rows, "total": total, "page": page, "size": size,
            "pages": max(1, (total + size - 1) // size)}


def stats(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    n_login = conn.execute("SELECT COUNT(DISTINCT login) n FROM user_login").fetchone()["n"]
    n_login_7 = conn.execute("SELECT COUNT(DISTINCT login) n FROM user_login "
                             "WHERE ts >= date('now','-7 days')").fetchone()["n"]
    su = conn.execute("SELECT COUNT(*) n, SUM(verified_at IS NOT NULL) v, SUM(active=1) a "
                      "FROM sub_users WHERE TRIM(COALESCE(email,''))<>''").fetchone()
    n_sub = conn.execute("SELECT COUNT(*) n FROM subs").fetchone()["n"]
    n_ign = conn.execute("SELECT COUNT(*) n FROM issues WHERE ignored_by IS NOT NULL "
                         "AND ignored_by<>''").fetchone()["n"]
    return {"logins": n_login, "logins_7d": n_login_7, "subscribers": su["n"] or 0,
            "verified": su["v"] or 0, "active": su["a"] or 0,
            "subs": n_sub, "ignores": n_ign}


# ---------------- 用户详情 ----------------
def user_detail(conn: sqlite3.Connection, login: str, describe=None, section_names=None,
                rule_label=None, count_of=None) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM sub_users WHERE login=?", (login,)).fetchone()
    name = (row["name"] if row else "") or ""
    subs = []
    for r in conn.execute("SELECT id,label,query,summary,enabled,created_at,COALESCE(sort_order,id) o "
                          "FROM subs WHERE login=? ORDER BY o, id", (login,)):
        params = _params_of(r["query"])
        text = r["summary"] or (describe(params, section_names or {}, rule_label) if describe else "")
        n = None
        if count_of:
            try:
                n = count_of(params)
            except Exception:  # noqa: BLE001
                n = None
        subs.append({"id": r["id"], "label": r["label"], "desc": text,
                     "enabled": bool(r["enabled"]), "n": n, "created_at": r["created_at"]})
    sends = [dict(r) for r in conn.execute(
        "SELECT sent_at,email,groups,issues,new_today,ok,error FROM sub_sends "
        "WHERE login=? ORDER BY id DESC LIMIT 10", (login,))]
    ignores = [dict(r) for r in conn.execute(
        "SELECT id,rule,doc_key,message,ignored_at FROM issues "
        "WHERE ignored_by=? ORDER BY ignored_at DESC LIMIT 10", (login,))]
    logins = [dict(r) for r in conn.execute(
        "SELECT ts,ip,ua FROM user_login WHERE login=? ORDER BY id DESC LIMIT 10", (login,))]
    found = bool(row) or bool(logins) or bool(ignores)
    return {"login": login, "name": name, "found": found,
            "email": (row["email"] if row else "") or "",
            "email_masked": mask_email((row["email"] if row else "") or ""),
            "verified": bool(row["verified_at"]) if row else False,
            "active": bool(row["active"]) if row else False,
            "created_at": (row["created_at"] if row else ""),
            "subs": subs, "sends": sends, "ignores": ignores, "logins": logins}


# ---------------- 发信流水 / 忽略审计 ----------------
def sends_page(conn: sqlite3.Connection, page: int = 0, size: int = 20, q: str = "") -> dict:
    conn.row_factory = sqlite3.Row
    where, args = "", []
    if q:
        where = " WHERE (s.login LIKE ? OR s.email LIKE ?)"
        args = [f"%{q}%", f"%{q}%"]
    total = conn.execute(f"SELECT COUNT(*) n FROM sub_sends s{where}", args).fetchone()["n"]
    rows = conn.execute(
        f"SELECT s.* FROM sub_sends s{where} ORDER BY s.id DESC LIMIT ? OFFSET ?",
        args + [size, page * size]).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "size": size,
            "pages": max(1, (total + size - 1) // size)}


def ignores_page(conn: sqlite3.Connection, page: int = 0, size: int = 20, q: str = "") -> dict:
    conn.row_factory = sqlite3.Row
    where, args = "", []
    if q:
        where = " AND (i.ignored_by LIKE ? OR i.doc_key LIKE ?)"
        args = [f"%{q}%", f"%{q}%"]
    total = conn.execute("SELECT COUNT(*) n FROM issues i WHERE i.ignored_by IS NOT NULL "
                         f"AND i.ignored_by<>''{where}", args).fetchone()["n"]
    rows = conn.execute(
        "SELECT i.id,i.rule,i.doc_key,i.message,i.ignored_by,i.ignored_at,i.lang,i.section "
        f"FROM issues i WHERE i.ignored_by IS NOT NULL AND i.ignored_by<>''{where} "
        "ORDER BY i.ignored_at DESC LIMIT ? OFFSET ?", args + [size, page * size]).fetchall()
    return {"rows": [dict(r) for r in rows], "total": total, "page": page, "size": size,
            "pages": max(1, (total + size - 1) // size)}


# ---------------- 两个安全写操作 ----------------
def set_active(conn: sqlite3.Connection, login: str, active: bool) -> tuple[bool, str]:
    row = conn.execute("SELECT email,verified_at FROM sub_users WHERE login=?", (login,)).fetchone()
    if not row:
        return False, "该用户没有订阅账号"
    if active and not (row["email"] or "").strip():
        return False, "该用户还没绑定邮箱，启用也收不到邮件"
    if active and not row["verified_at"]:
        return False, "邮箱还没确认，启用前请先让对方点确认信"
    conn.execute("UPDATE sub_users SET active=?, updated_at=? WHERE login=?",
                 (1 if active else 0, _now(), login))
    conn.commit()
    return True, ("已恢复收信" if active else "已停用收信（关注范围保留，不再发送邮件）")


def unbind(conn: sqlite3.Connection, login: str) -> tuple[bool, str]:
    row = conn.execute("SELECT email FROM sub_users WHERE login=?", (login,)).fetchone()
    if not row or not (row["email"] or "").strip():
        return False, "该用户还没有绑定邮箱"
    old = row["email"]
    conn.execute("UPDATE sub_users SET email='', verified_at=NULL, active=0, token=?, updated_at=? "
                 "WHERE login=?", (secrets.token_urlsafe(24), _now(), login))
    conn.commit()
    return True, f"已解绑 {mask_email(old)}，关注范围保留但不再发信"
