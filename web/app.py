#!/usr/bin/python3
"""docstor Web：docsify 站点 + 问题 API + 全量问题页。"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import re
import subprocess
import time
from datetime import datetime
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import checks  # noqa: E402
import auth as gcauth  # noqa: E402  GitCode 登录（web/auth.py）
import issueq  # noqa: E402  筛选条件构造（列表/导出/订阅共用）
import subs as subcenter  # noqa: E402  个人中心 + 邮件订阅
import users as usermgr  # noqa: E402  站点管理 · 用户管理
import ds_catalog
import ds_db  # noqa: E402
import ds_alias  # noqa: E402
import ds_rules  # noqa: E402
from checks.link_check import abs_href  # noqa: E402
import checks.link_check as _lc  # noqa: E402

CFG = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
SITE = BASE / CFG["site"]["output_dir"]
DB_PATH = BASE / CFG["db"]["path"]

def _load_env(path: Path) -> dict:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


_ENV = _load_env(BASE / ".admin.env")
app = Flask(__name__, template_folder=str(BASE / "web" / "templates"))
# nginx 反向代理：让 url_root 认得出 https，导出的镜像链接才是 https://…
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = _ENV.get("SECRET_KEY") or os.urandom(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ── 安全基线（对外开放前加固）────────────────────────────────────
_FAILS: dict[str, list[float]] = {}          # 后台口令失败记录：IP → 时间戳
_FAIL_MAX, _FAIL_WINDOW, _FAIL_LOCK = 8, 600, 900   # 10 分钟内错 8 次 → 锁 15 分钟


def _client_ip() -> str:
    return (request.headers.get("X-Real-IP") or request.remote_addr or "").strip()


# ---------------- CSRF / 同源：写操作只能从本站页面发起 ----------------
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "X-CSRF-Token"


def _csrf_token() -> str:
    """会话级令牌：首次渲染页面时生成，之后一直复用（签名 cookie，多 worker 共享）。"""
    t = session.get("csrf")
    if not t:
        t = secrets.token_urlsafe(24)
        session["csrf"] = t
    return t


@app.context_processor
def _inject_csrf():
    return {"csrf_token": _csrf_token}


def _same_origin(url: str) -> bool:
    try:
        return urlsplit(url).netloc == request.host
    except Exception:  # noqa: BLE001
        return False


def _csrf_reject(msg: str):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": msg}), 403
    return (f"<div style='font:15px/1.8 system-ui;padding:60px;text-align:center'>"
            f"<h2 style='margin:0 0 8px'>请求被拒绝</h2>"
            f"<p style='color:#777'>{msg}</p>"
            f"<p><a href='/issues'>← 回到全量问题</a></p></div>", 403)


@app.before_request
def _csrf_guard():
    """所有写操作（POST/PUT/PATCH/DELETE）：
    ① 带 Origin/Referer 的必须同源；/api/ 下连来源头都没有的直接拒（挡住 curl 直线调用）；
    ② 必须带页面内的一次性令牌（X-CSRF-Token 头或表单 csrf_token 字段）。
    """
    if request.method in _SAFE_METHODS:
        return None
    origin = (request.headers.get("Origin") or "").strip()
    referer = (request.headers.get("Referer") or "").strip()
    if origin:
        if not _same_origin(origin):
            return _csrf_reject("跨站请求被拒绝")
    elif referer:
        if not _same_origin(referer):
            return _csrf_reject("跨站请求被拒绝")
    elif request.path.startswith("/api/"):
        return _csrf_reject("缺少来源信息，请从站点页面操作")
    tok = (request.headers.get(CSRF_HEADER) or "").strip()
    if not tok and request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        tok = (request.form.get("csrf_token") or "").strip()
    want = session.get("csrf") or ""
    if not want or not tok or not hmac.compare_digest(tok, want):
        return _csrf_reject("页面令牌已失效，请刷新页面后重试")
    return None


@app.before_request
def _harden_cookie():
    """全站走 https 时才给会话 cookie 加 Secure；本地 http 调试仍可用。"""
    app.config["SESSION_COOKIE_SECURE"] = bool(request.is_secure)


@app.after_request
def _secure_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if request.is_secure:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "接口不存在"}), 404
    return e


@app.errorhandler(Exception)
def _unhandled(e):  # noqa: ANN001
    """兜底：接口一律回 JSON，页面回一句人话；别把 500 的堆栈暴露给访客。"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    app.logger.exception("未处理异常 %s", request.path)
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "服务器内部错误，请稍后重试"}), 500
    return ("<div style='font:15px/1.8 system-ui;padding:60px;text-align:center'>"
            "<h2 style='margin:0 0 8px'>服务器内部错误</h2>"
            "<p style='color:#777'>请稍后重试；如果一直这样，把访问的地址告诉管理员。</p>"
            "<p><a href='/issues'>← 回到全量问题</a></p></div>", 500)

gcauth.init_app(app)

_db = ds_db.DB(str(DB_PATH))
ds_rules.ensure(_db.conn, checks.REGISTRY)
usermgr.ensure_schema(_db.conn)
try:                                  # AI 三张表（含 ai_usage_log.actor 补列）
    from ai.cache import ensure_schema as _ai_ensure
    _ai_ensure()
except Exception as _e:  # noqa: BLE001
    app.logger.warning("AI 表初始化失败: %s", _e)
try:                                  # 别名表只在缺失文档时才用到，起不来也不影响主站
    ds_alias.build(_db.conn)
except Exception:  # noqa: BLE001
    pass


def conn():
    return _db.conn


_MD_LINES: dict = {}


def _md_lines(doc_key: str) -> list[str]:
    """按 doc_key 取 md 源文件行（老问题的 locator 没存链接文字时用它反查）。"""
    if doc_key in _MD_LINES:
        return _MD_LINES[doc_key]
    lines: list[str] = []
    try:
        row = conn().execute("SELECT md_path FROM docs WHERE doc_key=?", (doc_key,)).fetchone()
        if row and row["md_path"] and Path(row["md_path"]).exists():
            lines = Path(row["md_path"]).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:  # noqa: BLE001
        lines = []
    if len(_MD_LINES) > 300:
        _MD_LINES.clear()
    _MD_LINES[doc_key] = lines
    return lines


def _link_meta(doc_key: str, loc: dict) -> tuple[str, str]:
    """问题里那条链接的「原文地址」和「链接文字」。"""
    target = loc.get("target") or ""
    href = loc.get("href") or abs_href(target)
    text = loc.get("text") or ""
    if not text and target:
        line = loc.get("line") or 0
        lines = _md_lines(doc_key)
        if 0 < line <= len(lines):
            for _ln, t2, text2, _h in _lc.iter_links(lines[line - 1]):
                if t2 == target or t2.split("#")[0] == target.split("#")[0]:
                    text = text2
                    break
    return href, text


def is_admin() -> bool:
    return bool(session.get("admin"))


def require_admin(fn):
    from functools import wraps

    @wraps(fn)
    def inner(*a, **kw):
        if not is_admin():
            return redirect(url_for("admin_page"))
        return fn(*a, **kw)
    return inner


# ---------------- 静态站点 ----------------
@app.route("/")
def index():
    """文档站首页：docsify 的静态入口，注入与 /issues /sync /me 同一套顶栏。"""
    html = (SITE / "index.html").read_text(encoding="utf-8")
    nav = render_template("_nav.html")
    if "<!--DOCSTOR_NAV-->" in html:
        html = html.replace("<!--DOCSTOR_NAV-->", nav, 1)
    else:
        html = html.replace("<body>", "<body>\n" + nav, 1)
    return html, 200, {"Content-Type": "text/html; charset=utf-8",
                       "Cache-Control": "no-cache, must-revalidate"}


def _find_doc_section(lang: str, slug: str) -> str:
    """在镜像里按 slug 找这篇文档实际所在的板块（跨板块兜底，返回板块名或空串）。"""
    base = SITE / "docs" / lang
    try:
        for d in base.iterdir():
            if d.is_dir() and (d / f"{slug}.md").exists():
                return d.name
    except OSError:
        return ""
    return ""


@app.route("/docs/<path:fn>")
def docs(fn):
    """静态文档由 nginx 直出；只有 404 时（error_page）才进到这里，用于旧地址跳转。"""
    target = SITE / "docs" / fn
    if not target.exists() and fn.endswith(".md"):
        parts = fn[:-3].split("/")
        if len(parts) == 3:
            hit = ds_alias.resolve(conn(), *parts,
                                   exists=lambda rel: (SITE / "docs" / rel).exists())
            if hit:
                return redirect(f"/docs/{hit[0]}/{hit[1]}/{hit[2]}.md", code=302)
        low = (SITE / "docs" / fn.lower())      # 大小写不一致（官网源码里的相对链接常见）
        if low.exists():
            return redirect(f"/docs/{fn.lower()}", code=302)
        if len(parts) == 3:
            # 老板块名（/cn/games/、/cn/multi-device/ …）：同一篇文档被镜像在别的板块下
            found = _find_doc_section(parts[0], parts[2])
            if found:
                return redirect(f"/docs/{parts[0]}/{found}/{parts[2]}.md", code=302)
    return send_from_directory(SITE / "docs", fn)


@app.route("/assets/<path:fn>")
def assets(fn):
    return send_from_directory(SITE / "assets", fn)


@app.route("/favicon.ico")
def favicon():
    p = SITE / "assets" / "favicon.ico"
    if p.exists():
        return send_from_directory(p.parent, p.name)
    abort(404)


# ---------------- API ----------------
def _preview_rule() -> str:
    """管理员调试预览时（?preview=<rule>），让这条检查项穿透可见性过滤。

    只认后台 cookie，普通用户带这个参数无效，避免调试中的规则被外部看到。
    """
    r = (request.args.get("preview") or "").strip()
    if not r or r not in checks.REGISTRY:
        return ""
    try:
        return r if is_admin() else ""
    except Exception:  # noqa: BLE001
        return ""


def _enabled_rules() -> list[str]:
    """当前请求可见的检查项（enabled & visible，预览时再加一条）。

    关闭的、以及「调试中」（跑但不对外）的检查项都不进全量问题：
    卡片、列表、侧栏、文档问题面板、facets、导出、订阅预览一律不显示。
    管理员带 ?preview=<rule> 时，额外允许这一条（调试预览页用）。
    """
    try:
        rules = ds_rules.visible_rules(conn(), checks.REGISTRY)
    except Exception:  # noqa: BLE001
        rules = []
    pv = _preview_rule()
    if pv and pv not in rules:
        rules = rules + [pv]
    return rules


def _enabled_filter(col: str = "rule", lang_col: str | None = None) -> tuple[str, list]:
    """「已开启且对外」的规则过滤；带上语言限制（见 ds_rules.sql_filter）。"""
    rules = _enabled_rules()
    if not rules:
        return "1=0", []
    if lang_col is None:                       # 跟着别名走：i.rule → i.lang
        lang_col = (col.rsplit(".", 1)[0] + ".lang") if "." in col else "lang"
    pv = _preview_rule()
    try:
        clause, args = ds_rules.sql_filter(conn(), checks.REGISTRY,
                                           rule_col=col, lang_col=lang_col,
                                           extra_rules=[pv] if pv else None)
        return clause.lstrip().removeprefix("AND ").strip(), args
    except Exception:  # noqa: BLE001  兜底：至少不忽略规则白名单
        return f"{col} IN ({','.join('?' * len(rules))})", rules


@app.get("/api/sidebar-counts")
def api_sidebar_counts():
    lang = request.args.get("lang", "cn")
    section = request.args.get("section", "")
    fw, fa = _enabled_filter()
    rows = conn().execute(
        f"""SELECT doc_key, rule, COUNT(*) n FROM issues
           WHERE lang=? AND section=? AND status='open' AND {fw} GROUP BY doc_key, rule""",
        [lang, section] + fa).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        slug = r["doc_key"].rsplit("/", 1)[-1]
        d = out.setdefault(slug, {"total": 0, "rules": {}})
        d["total"] += r["n"]
        d["rules"][r["rule"]] = r["n"]
    return jsonify({"counts": out})


@app.get("/api/doc-issues")
def api_doc_issues():
    lang = request.args.get("lang", "cn")
    section = request.args.get("section", "")
    slug = request.args.get("slug", "")
    doc_key = f"{lang}/{section}/{slug}"
    fw, fa = _enabled_filter()
    rows = conn().execute(
        f"""SELECT rule, severity, locator, message FROM issues
           WHERE doc_key=? AND status='open' AND {fw}
           ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                    rule, id""", [doc_key] + fa).fetchall()
    issues = []
    for r in rows:
        loc = {}
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        href, link_text = _link_meta(doc_key, loc)
        issues.append({"rule": r["rule"], "rule_label": checks.rule_label(r["rule"]),
                       "severity": r["severity"], "message": r["message"],
                       "locator": r["locator"],
                       "anchor_text": loc.get("anchor_text", ""),
                       "anchorId": loc.get("anchorId", ""),
                       "href": href, "link_text": link_text,
                       "suggest": loc.get("new", "")})
    return jsonify({"issues": issues, "total": len(issues)})


def _issue_where(a=None) -> tuple[str, list]:
    """全量问题的筛选条件（列表、导出、订阅共用一套口径，见 web/issueq.py）。"""
    return issueq.issue_where(a if a is not None else request.args, _enabled_filter)


def _int_arg(name: str, default: int, lo: int, hi: int) -> int:
    """URL 参数转整数：非法值回落默认（直接 int() 会 500，用户分享带脏参数的链接就炸）。"""
    try:
        v = int(str(request.args.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


@app.get("/api/issues")
def api_issues():
    page = _int_arg("page", 1, 1, 100000)
    size = _int_arg("size", 50, 5, 200)
    w, args = _issue_where()
    total = conn().execute(f"SELECT COUNT(*) n FROM issues i WHERE {w}", args).fetchone()["n"]
    rows = conn().execute(
        f"""SELECT i.id, i.doc_key, i.lang, i.section, i.rule, i.severity, i.locator, i.message,
                   i.first_seen, i.last_seen, i.status, i.ignored_by, i.ignored_at,
                   i.ignore_reason, d.url AS doc_url, d.is_api
            FROM issues i LEFT JOIN docs d ON d.doc_key = i.doc_key
            WHERE {w}
            ORDER BY i.first_seen DESC,
                     CASE i.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     i.id DESC
            LIMIT ? OFFSET ?""", args + [size, (page - 1) * size]).fetchall()
    items = []
    _dl = time.time() + 3.0       # 一整页最多花 3 秒拼官网地址，剩下的行直接用 docs.url 兜底
    for r in rows:
        loc = {}
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        doc_key = r["doc_key"] or ""
        parts = doc_key.split("/")
        # 官网原文地址：按实测目录拼（板块 ≠ 目录时按板块拼会 404，如 games 文档在 games-guides 下）
        _b = min(1.5, max(0.0, _dl - time.time()))
        official = ""
        if _b > 0 and len(parts) == 3:
            official = ds_catalog.url_for(r["lang"], r["section"], parts[-1] or "",
                                          int(r["is_api"] or 0), budget=_b)
        official = official or (r["doc_url"] or "")   # 兜底：docs.url 里存的是抓取时用的真实官网地址
        href, link_text = _link_meta(doc_key, loc)
        items.append({**{k: r[k] for k in r.keys()},
                      "rule_label": checks.rule_label(r["rule"]),
                      "anchor_text": loc.get("anchor_text", ""),
                      "anchorId": loc.get("anchorId", ""),
                      "href": href,
                      "link_text": link_text,
                      "suggest": loc.get("new", ""),
                      "official_url": official,
                      "official_gone": (not official)})
    return jsonify({"total": total, "page": page, "size": size,
                    "pages": (total + size - 1) // size, "items": items})


@app.post("/api/issues/<int:iid>/ignore")
def api_issue_ignore(iid: int):
    """人工忽略一条问题（需 GitCode 登录）。记录忽略人与时间。"""
    user = gcauth.current_user()
    by = gcauth.login_name() if user else ("后台（管理员）" if is_admin() else "")
    if not by:
        return jsonify({"ok": False, "need_login": True, "error": "请先用 GitCode 登录"}), 401
    row = _db.get_issue(iid)
    if row is None:
        return jsonify({"ok": False, "error": "问题不存在"}), 404
    if row["status"] == "resolved":
        return jsonify({"ok": False, "error": "已解决的问题不能忽略"}), 400
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()[:200]
    _db.set_ignored(iid, by, reason)
    cur = _db.get_issue(iid)
    return jsonify({"ok": True, "id": iid, "status": cur["status"],
                    "ignored_by": cur["ignored_by"], "ignored_at": cur["ignored_at"],
                    "ignore_reason": cur["ignore_reason"] or ""})


@app.post("/api/issues/<int:iid>/suggest")
def api_issue_suggest(iid: int):
    """对单条问题让 AI 给一条整改建议（登录或管理员可用；结果按 fprint 缓存，重复点击不烧 token）。"""
    if not gcauth.current_user() and not is_admin():
        return jsonify({"ok": False, "need_login": True, "error": "请先用 GitCode 登录"}), 401
    row = _db.get_issue(iid)
    if row is None:
        return jsonify({"ok": False, "error": "问题不存在"}), 404
    loc = {}
    try:
        loc = json.loads(row["locator"] or "{}")
    except Exception:  # noqa: BLE001
        loc = {}
    doc = conn().execute("SELECT md_path, url, title FROM docs WHERE doc_key=?",
                         (row["doc_key"],)).fetchone()
    line_text = ""
    md_path = (doc["md_path"] if doc else "") or ""
    lines: list[str] = []
    if md_path:
        try:
            lines = Path(BASE / md_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:  # noqa: BLE001
            lines = []
    ln = int(loc.get("line") or 0)
    if lines and 1 <= ln <= len(lines):
        lo, hi = max(0, ln - 2), min(len(lines), ln + 1)
        line_text = "\n".join(x.strip() for x in lines[lo:hi] if x.strip())[:600]
    if not line_text:
        # AGC 这类按词命中的规则，locator 里没有行号，行号藏在 evidence.hits 里
        try:
            hits = (json.loads(row["evidence"] or "{}").get("hits") or [])[:3]
        except Exception:  # noqa: BLE001
            hits = []
        picks = []
        for h in hits:
            hln = int(h.get("line") or 0)
            if lines and 1 <= hln <= len(lines):
                picks.append(lines[hln - 1].strip())
            elif h.get("text"):
                picks.append(str(h.get("text")).strip())
        line_text = "\n".join(p for p in picks if p)[:600]
    parts = (row["doc_key"] or "").split("/")
    official = ""
    if len(parts) == 3:
        try:
            official = ds_catalog.url_for(row["lang"], row["section"], parts[-1], budget=1.5)
        except Exception:  # noqa: BLE001
            official = ""
    official = official or (doc["url"] if doc else "") or ""
    fields = {
        "rule_label": checks.rule_label(row["rule"]),
        "message": row["message"] or "",
        "doc_key": row["doc_key"] or "",
        "doc_url": official,
        "line_text": line_text or "（没有定位到原文行）",
        "target": loc.get("target") or loc.get("href") or "、".join(loc.get("terms") or []),
        "url": loc.get("url") or loc.get("target") or "",
        "hint": checks.ai_hint(row["rule"]) or "（无）",
    }
    try:
        import ai
        actor = (gcauth.login_name() if gcauth.current_user() else "") or \
                ("后台（管理员）" if is_admin() else "")
        res = ai.review("fix_suggestion", fields, module="issue", item_key=row["fprint"],
                        actor=actor)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"AI 暂不可用：{str(e)[:120]}"}), 503
    return jsonify({"ok": True, "advice": res.get("advice") or res.get("reason") or "",
                    "confidence": res.get("confidence"),
                    "needs_human": bool(res.get("needs_human")),
                    "snippet": res.get("snippet") or "",
                    "cached": bool((res.get("_meta") or {}).get("cached"))})


@app.post("/api/issues/<int:iid>/unignore")
def api_issue_unignore(iid: int):
    """取消忽略，回到未解决（需 GitCode 登录）。"""
    if not gcauth.current_user() and not is_admin():
        return jsonify({"ok": False, "need_login": True, "error": "请先用 GitCode 登录"}), 401
    if not _db.clear_ignored(iid):
        return jsonify({"ok": False, "error": "这条问题不是已忽略状态"}), 400
    return jsonify({"ok": True, "id": iid, "status": "open"})


EXPORT_COLS = [
    ("问题类型", 18), ("语言", 8), ("板块", 18), ("细分/领域", 22), ("Kit", 24),
    ("文档类别", 10), ("问题描述", 80), ("原文链接文字", 30), ("原文目标地址", 60),
    ("镜像文档", 52), ("官网文档", 52), ("状态", 10), ("识别日期", 12),
    ("最近发现", 20), ("解决时间", 20), ("忽略人", 16), ("忽略时间", 20), ("忽略理由", 24),
    ("严重级别", 10), ("文档标识", 46),
]


@app.get("/sync")
def sync_page():
    return render_template("sync.html")


@app.get("/api/sync/days")
def api_sync_days():
    """按天汇总的抓取记录（新增/修改/删除/耗时/任务数）。"""
    c = conn()
    rows = c.execute(
        """SELECT date(started_at) d, COUNT(*) runs,
                  SUM(COALESCE(json_extract(stats,'$.added'),0)) added,
                  SUM(COALESCE(json_extract(stats,'$.modified'),0)) modified,
                  SUM(COALESCE(json_extract(stats,'$.deleted'),0)) deleted,
                  SUM(CASE WHEN kind='ingest' THEN COALESCE(json_extract(stats,'$.elapsed_s'),0)
                           ELSE 0 END) ingest_secs,
                  SUM(CASE WHEN kind='check' THEN COALESCE(json_extract(stats,'$.elapsed_s'),0)
                           ELSE 0 END) check_secs,
                  SUM(CASE WHEN kind='ingest' THEN 1 ELSE 0 END) ingest_runs
           FROM runs WHERE kind IN ('ingest','check') GROUP BY d ORDER BY d DESC LIMIT 120""").fetchall()

    # 没有 finished_at 的 run = 进程被超时杀掉/中断，这一项当天其实没跑完，页面要标出来
    fails = {}
    for r in c.execute(
            """SELECT date(started_at) d, kind, target, started_at FROM runs
               WHERE finished_at IS NULL AND kind IN ('ingest','check')
               ORDER BY id"""):
        fails.setdefault(r["d"], []).append(
            {"kind": r["kind"], "target": r["target"], "started_at": r["started_at"]})
    docs_total = c.execute("SELECT COUNT(*) n FROM docs WHERE status='ok'").fetchone()["n"]
    last = c.execute("SELECT started_at FROM runs WHERE kind='ingest' "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    secs = [r["section"] for r in c.execute(
        "SELECT DISTINCT section FROM docs WHERE status='ok' ORDER BY 1")]
    return jsonify({
        "today": datetime.now().strftime("%Y-%m-%d"),
        "docs_total": docs_total,
        "last_run": last["started_at"] if last else None,
        "days": [{**dict(r), "fails": fails.get(r["d"], [])} for r in rows],
        "sections": [{"key": s, "name": SECTION_NAMES.get(s, s)} for s in secs],
    })


@app.get("/api/sync/items")
def api_sync_items():
    """某一天的逐条变更明细。"""
    date_s = request.args.get("date", "").strip()
    kind = request.args.get("kind", "")
    lang = request.args.get("lang", "")
    section = request.args.get("section", "")
    q = request.args.get("q", "").strip()
    page = _int_arg("page", 1, 1, 100000)
    size = _int_arg("size", 50, 10, 200)

    where, args = ["1=1"], []
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_s or ""):
        where.append("date(r.started_at)=?"); args.append(date_s)
    if kind in ("added", "modified", "deleted"):
        where.append("i.kind=?"); args.append(kind)
    if lang in ("cn", "en"):
        where.append("i.lang=?"); args.append(lang)
    if section:
        where.append("i.section=?"); args.append(section)
    if q:
        where.append("i.title LIKE ?"); args.append(f"%{q}%")
    base = ("FROM sync_items i JOIN runs r ON r.id=i.run_id "
            "LEFT JOIN docs d ON d.doc_key=i.doc_key WHERE " + " AND ".join(where))
    c = conn()
    total = c.execute("SELECT COUNT(*) n " + base, args).fetchone()["n"]
    rows = c.execute("SELECT i.doc_key, i.kind, i.lang, i.section, i.slug, i.title, i.version, "
                     "i.old_version, r.started_at, d.is_api, d.url AS doc_url " + base +
                     " ORDER BY CASE i.kind WHEN 'deleted' THEN 1 ELSE 0 END, i.id DESC"
                     " LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
    items = []
    for r in rows:
        official = ""
        if r["slug"]:
            try:
                official = ds_catalog.url_for(r["lang"], r["section"], r["slug"],
                                              int(r["is_api"] or 0), budget=1.5)
                official = official or (r["doc_url"] or "")   # 兜底用抓取时的真实地址
            except Exception:  # noqa: BLE001
                official = ""
        items.append({
            "doc_key": r["doc_key"], "kind": r["kind"], "lang": r["lang"],
            "section": r["section"], "section_name": SECTION_NAMES.get(r["section"], r["section"]),
            "slug": r["slug"], "title": r["title"], "version": r["version"],
            "old_version": r["old_version"], "started_at": r["started_at"],
            "href": f"/#/{r['doc_key']}", "official_url": official,
        })
    return jsonify({"total": total, "page": page, "size": size,
                    "pages": (total + size - 1) // size, "items": items})


@app.get("/api/sync-status")
def api_sync_status():
    """每日任务的上次运行结果（data/last_sync.json），供页面展示。"""
    p = BASE / "data" / "last_sync.json"
    if not p.is_file():
        return jsonify({"exists": False})
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return jsonify({"exists": False})
    d["exists"] = True
    return jsonify(d)


_EXPORT_CACHE: dict[str, tuple[float, bytes]] = {}
_EXPORT_LOCK = __import__("threading").Lock()
_EXPORT_TTL = 180


@app.get("/api/export.xlsx")
def api_export():
    """按当前筛选条件导出全部问题（不分页），筛选维度一并作为列。

    同一个筛选 3 分钟内复用已生成的文件，且同一时刻只允许一个线程在生成
    （1,400 行 × 20 列要 3 秒；十几个人同时点会把单进程 CPU 占满）。
    """
    import io
    import time as _t
    from datetime import datetime

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    _key = "&".join(f"{k}={v}" for k, v in sorted(request.args.items())) or "(all)"
    _hit = _EXPORT_CACHE.get(_key)
    if _hit and _t.time() - _hit[0] < _EXPORT_TTL:
        return _send_xlsx(_hit[1])
    with _EXPORT_LOCK:                      # 单飞：并发时只有第一个真生成，其余排队后拿缓存
        _hit = _EXPORT_CACHE.get(_key)
        if _hit and _t.time() - _hit[0] < _EXPORT_TTL:
            return _send_xlsx(_hit[1])
        return _build_export(_key)


def _send_xlsx(data: bytes):
    import io
    from datetime import datetime
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=f"全量问题_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _build_export(_key: str):
    import io
    import time as _t
    from datetime import datetime
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    w, args = _issue_where()
    # 注意：不要把 doc_cat 用 c.lang||'/'||c.section||'/'||c.slug = i.doc_key 这种表达式 JOIN 进来——
    # 没有可用索引，SQLite 会按外层每行整表扫 doc_cat（34k 行 ×1,442 条 ≈ 32 秒）。
    # 这里一次性把 (lang/section/slug)→sub/domain/kit 读进内存，逐行字典查，全量 1,442 行 <1s。
    cat_map = {(r["lang"], r["section"], r["slug"]): (r["sub"], r["domain"], r["kit"])
               for r in conn().execute("SELECT lang, section, slug, sub, domain, kit FROM doc_cat")}
    rows = conn().execute(
        f"""SELECT i.doc_key, i.lang, i.section, i.rule, i.severity, i.locator, i.message,
                   i.first_seen, i.last_seen, i.status, i.resolved_at,
                   i.ignored_by, i.ignored_at, i.ignore_reason,
                   d.is_api, d.url AS doc_url
            FROM issues i
            LEFT JOIN docs d ON d.doc_key = i.doc_key
            WHERE {w}
            ORDER BY i.first_seen DESC,
                     CASE i.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     i.id DESC""", args).fetchall()

    wb = Workbook()
    ws = wb.active
    ws.title = "全量问题"
    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="4C6EF5")
    for i, (name, width) in enumerate(EXPORT_COLS, 1):
        cell = ws.cell(row=1, column=i, value=name)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    base = request.url_root.rstrip("/")
    link_font = Font(color="1155CC", underline="single")

    _dl = time.time() + 5.0                      # 导出整批最多 5 秒拼地址，超出退回 docs.url
    row_i = 1                                     # 表头占第 1 行；自己数，不要用 ws.max_row（O(n²)）
    for r in rows:
        doc_key = r["doc_key"] or ""
        parts = doc_key.split("/")
        slug = parts[-1] if parts else ""
        loc = {}
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        _b = min(1.0, max(0.0, _dl - time.time()))
        official = ""
        if _b > 0 and len(parts) == 3:
            official = ds_catalog.url_for(r["lang"], r["section"], slug,
                                          int(r["is_api"] or 0), budget=_b)
        official = official or (r["doc_url"] or "")   # 兜底：docs.url 是抓取时用的真实官网地址
        href, link_text = _link_meta(doc_key, loc)
        anchor = loc.get("anchorId") or ""
        mirror = f"{base}/#/{doc_key}" + (f"?id={anchor}" if anchor else "")
        _sub, _dom, _kit = cat_map.get((r["lang"], r["section"], parts[-1] if parts else ""),
                                       (None, None, None))
        sub = _dom or _sub or ""
        ws.append([
            checks.rule_label(r["rule"]),
            "中文" if r["lang"] == "cn" else "英文",
            SECTION_NAMES.get(r["section"], r["section"]),
            sub,
            _kit or "",
            "API参考" if r["is_api"] else ("指南" if r["is_api"] is not None else ""),
            r["message"] or "",
            link_text or "",
            href or "",
            mirror,
            official or "已下架",
            {"open": "未解决", "resolved": "已解决", "ignored": "已忽略"}.get(r["status"], r["status"]),
            (r["first_seen"] or "")[:10],
            r["last_seen"] or "",
            r["resolved_at"] or "",
            r["ignored_by"] or "",
            r["ignored_at"] or "",
            r["ignore_reason"] or "",
            {"error": "错误", "warn": "警告", "info": "提示"}.get(r["severity"], r["severity"]),
            doc_key,
        ])
        row_i += 1
        for col_i, val in ((9, href), (10, mirror), (11, official)):
            if val:
                c = ws.cell(row=row_i, column=col_i)
                c.hyperlink = val
                c.font = link_font
        for col_i in (7, 8, 9):
            ws.cell(row=row_i, column=col_i).alignment = Alignment(vertical="top", wrap_text=False)

    ws.auto_filter.ref = f"A1:{get_column_letter(len(EXPORT_COLS))}{row_i}"
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    if len(_EXPORT_CACHE) > 8:
        _EXPORT_CACHE.clear()
    _EXPORT_CACHE[_key] = (_t.time(), data)
    return _send_xlsx(data)

def _section_opts(rows):
    """板块下拉用文档侧边栏的板块名（版本说明/工具…），一个板块一项、中英文合并计数。

    中英文同名板块共用同一个筛选值，分成两项反而会互相串，所以合并。
    """
    agg: dict[str, int] = {}
    for r in rows:
        agg[r["section"]] = agg.get(r["section"], 0) + (r["n"] or 0)   # 与列表默认「未解决」一致
    out = [{"section": k, "n": v, "name": SECTION_NAMES.get(k, k)} for k, v in agg.items()]
    out.sort(key=lambda s: SECTION_ORDER.index(s["section"]) if s["section"] in SECTION_ORDER else 99)
    return out


def _cat_opts(col: str, sort_col: str, with_kind: bool = False):
    """doc_cat 某目录列 → 选项清单（0 条问题的也列出来，置灰显示）。

    with_kind=True 时一并给出「仅指南 / 仅 API 参考」两个口径下的文档数与未解决数，
    前端切换「指南+API参考」下拉后直接用这组数字，列表和计数不会对不上。
    """
    fw, fa = _enabled_filter("i.rule")
    join_docs = ("LEFT JOIN docs d ON d.doc_key = c.lang || '/' || c.section || '/' || c.slug"
                 if with_kind else "")
    kind_cols = """,
               SUM(CASE WHEN d.is_api=1 THEN 1 ELSE 0 END) nd_api,
               SUM(CASE WHEN d.is_api=0 THEN 1 ELSE 0 END) nd_guide,
               SUM(CASE WHEN d.is_api=1 AND i.status='open' THEN 1 ELSE 0 END) n_api,
               SUM(CASE WHEN d.is_api=0 AND i.status='open' THEN 1 ELSE 0 END) n_guide""" if with_kind else ""
    try:
        rows = conn().execute(
            f"""SELECT c.lang, c.section, c.{col} AS v, MIN(c.{sort_col}) s,
                       COUNT(i.id) n_all, SUM(CASE WHEN i.status='open' THEN 1 ELSE 0 END) n{kind_cols}
                FROM doc_cat c {join_docs}
                LEFT JOIN issues i
                  ON i.doc_key = c.lang || '/' || c.section || '/' || c.slug AND {fw}
                WHERE c.{col} IS NOT NULL AND c.{col} != ''
                GROUP BY 1,2,3 ORDER BY 1,2,s""", fa).fetchall()
    except Exception:  # noqa: BLE001  doc_cat 还没建
        return []
    out = []
    for r in rows:
        o = {"lang": r["lang"], "section": r["section"], "name": r["v"],
             "n": r["n"] or 0, "n_all": r["n_all"] or 0}
        if with_kind:
            o.update(nd_api=r["nd_api"] or 0, nd_guide=r["nd_guide"] or 0,
                     n_api=r["n_api"] or 0, n_guide=r["n_guide"] or 0)
        out.append(o)
    return out


def _sub_opts():
    return _cat_opts("sub", "sort")


def _domain_opts():
    # 领域要能跟着「仅指南 / 仅 API 参考」切换，所以带两种口径的计数
    return _cat_opts("domain", "sort_domain", with_kind=True)


def _kit_opts():
    """Kit 选项：按 (板块, 归一化名) 归并中英文，但计数必须留在「语言 + 领域」粒度。

    官网英文站的 Kit 与中文站是同一批（Ability Kit / ArkUI …），只是中文名多了「（程序框架服务）」
    这类括注，所以列表按归一化名合并成一条；但问题数不能合并——筛选时领域是带语言的
    （`中文站|能力|应用框架`），合并后的数会把英文站的问题也算进来，跟列表对不上。
    所以这里按 (板块, 归一名, 语言, 领域) 出行，前端只汇总选中领域那一种语言的数。
    """
    try:
        fw, fa = _enabled_filter("i.rule")
        rows = conn().execute(
            f"""SELECT c.section, c.kit_key, c.lang, c.domain, c.kit AS name,
                       MIN(c.sort_kit) s, MIN(c.sort_domain) sd,
                       COUNT(i.id) n_all, SUM(CASE WHEN i.status='open' THEN 1 ELSE 0 END) n,
                       SUM(CASE WHEN d.is_api=1 THEN 1 ELSE 0 END) nd_api,
                       SUM(CASE WHEN d.is_api=0 THEN 1 ELSE 0 END) nd_guide,
                       SUM(CASE WHEN d.is_api=1 AND i.status='open' THEN 1 ELSE 0 END) n_api,
                       SUM(CASE WHEN d.is_api=0 AND i.status='open' THEN 1 ELSE 0 END) n_guide
                FROM doc_cat c
                LEFT JOIN docs d ON d.doc_key = c.lang || '/' || c.section || '/' || c.slug
                LEFT JOIN issues i ON i.doc_key = c.lang || '/' || c.section || '/' || c.slug AND {fw}
                WHERE c.kit IS NOT NULL AND c.kit != ''
                GROUP BY 1,2,3,4,5 ORDER BY 1, MIN(c.sort_domain), s""", fa).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [{"section": r["section"], "key": r["kit_key"] or r["name"], "lang": r["lang"],
             "domain": r["domain"], "name": r["name"],
             "n": r["n"] or 0, "n_all": r["n_all"] or 0,
             "nd_api": r["nd_api"] or 0, "nd_guide": r["nd_guide"] or 0,
             "n_api": r["n_api"] or 0, "n_guide": r["n_guide"] or 0,
             "sort": r["s"], "sort_domain": r["sd"]}
            for r in sorted(rows, key=lambda r: (r["section"], r["sd"], r["s"]))]


_FACETS_CACHE: dict[str, tuple[float, dict]] = {}


@app.get("/api/facets")
def api_facets():
    """筛选器数据（规则/语言/板块/细分/领域/Kit 计数）。

    这是个重查询（几个 GROUP BY 拼 doc_cat 3.4 万行），一次 0.5 秒；十几个人同时刷页面
    会把这个单进程 CPU 占满。同一套检查项口径下缓存 60 秒，规则开关一变 key 就变。
    """
    fw, fa = _enabled_filter()
    _k = ",".join(fa)
    _hit = _FACETS_CACHE.get(_k)
    if _hit and time.time() - _hit[0] < 60:
        return jsonify(_hit[1])
    by_rule = [r for r in conn().execute(
        f"""SELECT rule, COUNT(*) n_all, SUM(status='open') n FROM issues
            WHERE {fw} GROUP BY rule ORDER BY n DESC""", fa)
        if r["rule"] in checks.REGISTRY]   # 已下线的规则不再出卡片；已关闭的不显示
    by_lang = conn().execute(
        f"""SELECT lang, COUNT(*) n FROM issues WHERE status='open' AND {fw}
            GROUP BY lang""", fa).fetchall()
    by_section = conn().execute(
        f"""SELECT lang, section, COUNT(*) n_all, SUM(status='open') n FROM issues
            WHERE {fw} GROUP BY lang, section ORDER BY n DESC""", fa).fetchall()
    cfg = ds_rules.all_settings(conn())

    def _rule_state(rule):
        c = cfg.get(rule) or {}
        meta = checks.REGISTRY.get(rule) or {}
        return (c.get("enabled", meta.get("default_on", True)),
                c.get("langs", list(meta.get("langs") or ("cn", "en"))))

    payload = {
        "rules": [dict({"rule": r["rule"], "label": checks.rule_label(r["rule"]),
                        "n": r["n"], "n_all": r["n_all"]},
                       enabled=_rule_state(r["rule"])[0],
                       langs=_rule_state(r["rule"])[1]) for r in by_rule],
        "langs": [{"lang": r["lang"], "n": r["n"]} for r in by_lang],
        "sections": _section_opts(by_section),
        "subs": _sub_opts(),
        "domains": _domain_opts(),
        "kits": _kit_opts(),
    }
    _FACETS_CACHE.clear()                       # 只留当前口径这一份
    _FACETS_CACHE[_k] = (time.time(), payload)
    return jsonify(payload)


SECTION_ORDER = ["release-notes", "deveco-studio", "ux-design", "capabilities", "getting-started",
                 "submission", "tools-faq", "advanced-features", "industry-solutions"]
SECTION_NAMES = {
    "release-notes": "版本说明", "deveco-studio": "工具", "ux-design": "设计",
    "capabilities": "指南与API参考", "getting-started": "开发与测试",
    "submission": "上架分发与运营", "tools-faq": "问题处理与运维",
    "advanced-features": "最佳实践", "industry-solutions": "行业解决方案",
}

# 英文站 8 个板块（无行业解决方案）
SECTION_NAMES_EN = {
    "release-notes": "Release Notes", "deveco-studio": "Tools", "ux-design": "Design",
    "capabilities": "Guides & API References", "getting-started": "Develop & Test",
    "submission": "Publish & Distribute", "tools-faq": "Support & FAQ",
    "advanced-features": "Best Practices",
}

_TREE_CACHE: dict = {}


@app.get("/api/aliases")
def api_aliases():
    """旧 slug → 新 slug（前端打开旧地址时先跳到新地址，锚点才能对得上）。"""
    try:
        rows = conn().execute(
            "SELECT lang, section, old_slug, new_lang, new_section, new_slug FROM slug_alias").fetchall()
    except Exception:  # noqa: BLE001
        return jsonify({"aliases": {}})
    out = {}
    for r in rows:
        sec = r["new_section"] or r["section"]
        out[f"{r['lang']}/{r['section']}/{r['old_slug']}"] = [r["new_lang"], sec, r["new_slug"]]
    return jsonify({"aliases": out})


@app.get("/api/tree")
def api_tree():
    """整站目录树（层级与官网一致）+ 各页问题数，供左侧侧边栏一次取用。"""
    lang = request.args.get("lang", "cn")
    stamp = conn().execute("SELECT COALESCE(MAX(id),0) m FROM nodes").fetchone()["m"]
    fw, fa = _enabled_filter()          # 徽标口径：只算已开启的规则
    sig = ",".join(fa)
    def _tree_resp(payload):
        resp = jsonify(payload)
        # 3.4MB 的树（gzip 后约 770KB）没必要每次翻页都重下
        resp.headers["Cache-Control"] = "private, max-age=120"
        return resp

    cached = _TREE_CACHE.get(lang)
    if cached and cached[0] == stamp and cached[1] == sig:
        return _tree_resp(cached[2])

    # 官网的行为：目录树里 hided=1 的节点平时不展开，但当前页所在的这条链路要显示出来
    # （例如「Automatic Scene Configuration Kit（场景配置服务）」整枝都是 hided=1，
    #  官网侧栏在浏览该 Kit 下的页面时照常显示）。所以这里不再过滤，交给前端按链路裁剪。
    rows = conn().execute(
        "SELECT section, node_id, parent, name, slug, sort, id, hided FROM nodes "
        "WHERE lang=? ORDER BY section, sort, id", (lang,)).fetchall()
    nodes: dict[str, dict] = {}
    children: dict[str, list[str]] = {}
    roots: dict[str, list[str]] = {}
    for r in rows:
        key = f"{r['section']}:{r['node_id']}"
        nodes[key] = {"i": key, "n": r["name"], "s": r["slug"] or None, "k": r["section"]}
        if r["hided"]:
            nodes[key]["h"] = 1
        children.setdefault(f"{r['section']}:{r['parent']}", []).append(key)
        if r["parent"] == "root":
            roots.setdefault(r["section"], []).append(key)

    def build(key: str, depth: int) -> dict:
        n = dict(nodes[key])
        kids = children.get(key) or []
        if kids and depth < 12:
            n["c"] = [build(k, depth + 1) for k in kids]
        return n

    sections = []
    for sec in SECTION_ORDER:
        top = roots.get(sec) or []
        if not top:
            continue
        # 每个板块可能有多个根节点（如「指南」「API参考」），用一个虚拟板块节点包起来
        sec_nodes = [build(k, 1) for k in top]
        names = SECTION_NAMES_EN if lang == "en" else SECTION_NAMES
        sections.append({"k": sec, "n": names.get(sec, sec),
                         "c": sec_nodes if len(sec_nodes) > 1 else (sec_nodes[0].get("c") or sec_nodes)})

    # 侧栏徽标必须与「全量问题」同口径：只算已开启的规则、只算 open
    issues = {}
    for r in conn().execute(
            f"SELECT section, doc_key, COUNT(*) n FROM issues WHERE status='open' "
            f"AND lang=? AND {fw} GROUP BY section, doc_key", [lang] + fa):
        issues[f"{r['section']}/{r['doc_key'].rsplit('/', 1)[-1]}"] = r["n"]

    payload = {"lang": lang, "sections": sections, "issues": issues}
    _TREE_CACHE[lang] = (stamp, ",".join(fa), payload)
    return _tree_resp(payload)


# ---------------- 全量问题页 ----------------
# 检查项分类：规则前缀 → 页面上的分组名（顺序即展示顺序；没有规则的分组不显示）
RULE_GROUP_NAMES = {"special": "专项检查", "link": "链接检查",
                   "image": "图片检查", "text": "文本检查"}
# 顺序即 /admin「检查项」页签里的分组展示顺序（专项检查在前）
RULE_GROUP_ORDER = ["special", "link", "image", "text"]

# 只在 /admin 界面上挂出来、规则逻辑还没实现的检查项：
# 不进 checks.REGISTRY，所以每日检查和手动运行都不会碰它们；状态列显示「待实现」，
# 不写 rule_settings，等规则定下来再挪进 REGISTRY 并删掉这里的条目。
PENDING_RULES: list[dict] = []


@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    err = ""
    if request.method == "POST":
        import time as _t
        ip = _client_ip()
        now = _t.time()
        fails = [t for t in _FAILS.get(ip, []) if now - t < _FAIL_LOCK]
        _FAILS[ip] = fails
        if len(fails) >= _FAIL_MAX and now - fails[-1] < _FAIL_LOCK:
            left = int((_FAIL_LOCK - (now - fails[-1])) / 60) + 1
            return render_template("admin.html", authed=False, rules=[], cache=None,
                                   err=f"口令错误次数过多，请 {left} 分钟后再试"), 429
        pwd = request.form.get("password", "")
        if _ENV.get("ADMIN_PASSWORD") and hmac.compare_digest(pwd, _ENV["ADMIN_PASSWORD"]):
            _FAILS.pop(ip, None)
            session["admin"] = True
            return redirect(url_for("admin_page"))
        fails.append(now)
        _FAILS[ip] = fails[-20:]
        _t.sleep(0.3)                      # 拖慢字典攻击；只影响后台登录这一条路径
        err = "口令不正确"
    if not is_admin():
        import time as _t
        fails = [t for t in _FAILS.get(_client_ip(), []) if _t.time() - t < _FAIL_LOCK]
        if not err and len(fails) >= _FAIL_MAX:
            left = int((_FAIL_LOCK - (_t.time() - fails[-1])) / 60) + 1
            err = f"口令错误次数过多，请 {left} 分钟后再试"
        return render_template("admin.html", authed=False, err=err, rules=[])
    cfg = ds_rules.all_settings(conn())
    # 未解决数必须按「规则的生效语言」统计：AGC 这条只管中文，库里还留着当年跑英文时的
    # 478 条（langs 改成 cn 后它们在所有对外口径里都不算），这里不能再算成 744。
    _by_lang = {(r["rule"], r["lang"]): r["n"] for r in conn().execute(
        "SELECT rule, lang, COUNT(*) n FROM issues WHERE status='open' GROUP BY rule, lang")}
    # 检查项按规则前缀归类：link.* → 链接检查、image.* → 图片检查、text.* → 文本检查，
    # 以后加的新检查项只要用对应前缀，就会自动落到该分类里，不用改这里
    buckets: dict[str, list] = {}
    for rule, meta in checks.REGISTRY.items():
        c = cfg.get(rule) or {}
        on = c.get("enabled", meta.get("default_on", True))
        vis = bool(on) and c.get("visible", meta.get("default_visible", meta.get("default_on", True)))
        _langs = [x for x in (c.get("langs") or meta.get("langs") or ["cn", "en"]) if x]
        buckets.setdefault(rule.split(".", 1)[0], []).append({
            "rule": rule, "label": meta["label"],
            "severity": meta.get("severity", "warn"),
            "enabled": on, "visible": vis,
            "state": "off" if not on else ("live" if vis else "debug"),
            "langs": _langs,
            "n": sum(_by_lang.get((rule, lg), 0) for lg in _langs),
            "updated_at": c.get("updated_at", "")})
    for p in PENDING_RULES:
        buckets.setdefault(p["group"], []).append({
            "rule": p["rule"], "label": p["label"], "severity": p["severity"],
            "enabled": False, "visible": False, "state": "pending", "pending": True,
            "langs": p["langs"], "n": 0, "updated_at": ""})
    groups = [{"name": RULE_GROUP_NAMES[key], "rules": buckets[key],
               "n": sum(r["n"] for r in buckets[key]),
               "pending_only": all(r.get("pending") for r in buckets[key])}
              for key in RULE_GROUP_ORDER if buckets.get(key)]
    return render_template("admin.html", authed=True, err="", groups=groups,
                           cache=_cache_stats(), ai=_ai_stats(), ai_calls=_ai_calls(0))


def _ai_stats() -> dict:
    """AI 额度 + 累计；AI 未配置/接口挂了都不该把 /admin 打挂。"""
    try:
        from ai.usage import usage_summary
        return usage_summary()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "stale": False, "error": str(e)[:120], "gauges": [],
                "local": {"calls": 0, "failed": 0, "tokens_h": "0", "tokens_in_h": "0",
                          "tokens_out_h": "0", "today_tokens_h": "0", "kinds": {}}}


def _ai_calls(page: int) -> dict:
    """最近调用（分页）。"""
    try:
        from ai.usage import calls_total, recent_calls
        size = 20
        rows = recent_calls(size, max(0, page) * size)
        total = calls_total()
        return {"rows": rows, "total": total, "page": page, "size": size,
                "pages": max(1, (total + size - 1) // size)}
    except Exception as e:  # noqa: BLE001
        return {"rows": [], "total": 0, "page": 0, "size": 20, "pages": 1, "error": str(e)[:120]}


# ---------------- 站点管理 · 用户管理 ----------------
# 注意：conn() 是进程级共享连接，这里绝不能 close()（关掉会让后续请求全 500）
def _users_conn():
    return conn()


def _user_count_of(c):
    """某个关注范围当前的条数（与全量问题/日报同口径：可见规则 + 规则适用语言）。"""
    def f(params: dict) -> int:
        where, args = issueq.issue_where(params, _enabled_filter)   # 传函数，不是它的返回值
        return c.execute(f"SELECT COUNT(*) FROM issues i WHERE {where}", args).fetchone()[0]
    return f


@app.get("/api/admin/users")
def api_admin_users():
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    c = _users_conn()
    q = (request.args.get("q") or "").strip()[:60]
    page = _int_arg("page", 0, 0, 10000)
    return jsonify({"ok": True, "stats": usermgr.stats(c),
                    "list": usermgr.list_users(c, q, page, 20)})


@app.get("/api/admin/users/<login>")
def api_admin_user_detail(login):
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    c = _users_conn()
    names = {**SECTION_NAMES, **SECTION_NAMES_EN}
    d = usermgr.user_detail(c, login, describe=subcenter.describe, section_names=names,
                            rule_label=checks.rule_label, count_of=_user_count_of(c))
    if not d.get("found"):
        return jsonify({"ok": False, "error": "没有这个用户"}), 404
    return jsonify({"ok": True, "user": d})


@app.post("/api/admin/users/<login>/active")
def api_admin_user_active(login):
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    want = bool((request.get_json(silent=True) or {}).get("active"))
    ok, msg = usermgr.set_active(_users_conn(), login, want)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.post("/api/admin/users/<login>/unbind")
def api_admin_user_unbind(login):
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    ok, msg = usermgr.unbind(_users_conn(), login)
    return jsonify({"ok": ok, "msg": msg}), (200 if ok else 400)


@app.get("/api/admin/sends")
def api_admin_sends():
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    return jsonify({"ok": True, "list": usermgr.sends_page(
        _users_conn(), _int_arg("page", 0, 0, 10000), 20,
        (request.args.get("q") or "").strip()[:60])})


@app.get("/api/admin/ignores")
def api_admin_ignores():
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    return jsonify({"ok": True, "list": usermgr.ignores_page(
        _users_conn(), _int_arg("page", 0, 0, 10000), 20,
        (request.args.get("q") or "").strip()[:60])})


@app.get("/api/admin/ai")
def api_admin_ai():
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    page = _int_arg("page", 0, 0, 10000)
    return jsonify({"ok": True, "summary": _ai_stats(), "calls": _ai_calls(page)})


def _cache_stats() -> dict:
    """看板数据；读失败也不该把 /admin 整个打挂。"""
    try:
        import ds_cache
        return ds_cache.stats()
    except Exception as e:  # noqa: BLE001
        return {"rows": [], "total": 0, "total_expired": 0, "error": str(e),
                "day": "", "at": "", "running": None}


@app.get("/api/admin/cache")
def api_admin_cache():
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    return jsonify({"ok": True, **_cache_stats()})


@app.post("/api/admin/cache/recheck")
def api_admin_cache_recheck():
    """把某张缓存里「已过期」的结论重判一遍（子进程跑，立即返回）。"""
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    kind = ((request.get_json(silent=True) or {}).get("kind") or "").strip()
    if kind not in ("official", "http"):
        return jsonify({"ok": False, "error": "kind 必须是 official 或 http"}), 400
    import ds_cache
    st = _cache_stats()
    running = st.get("running")
    if running and not running.get("finished_at"):
        return jsonify({"ok": False, "error": f"已经在重判 {running['kind']}（{running.get('done')}/{running.get('count')}）"}), 409
    table = "link_official" if kind == "official" else "link_http"
    n = conn().execute(f"SELECT COUNT(*) FROM {table} WHERE {ds_cache.expired_where(table)}").fetchone()[0]
    if n == 0:
        return jsonify({"ok": True, "count": 0, "msg": "没有过期的条目，不用重判"})
    logf = BASE / "logs" / f"cache_recheck_{kind}.log"
    logf.parent.mkdir(exist_ok=True)
    with open(logf, "ab") as fh:
        subprocess.Popen([sys.executable, str(BASE / "scripts" / "recheck_cache.py"),
                          "--kind", kind], cwd=str(BASE), stdout=fh, stderr=fh,
                         start_new_session=True)
    return jsonify({"ok": True, "count": n})


@app.post("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_page"))


@app.post("/api/rules/save")
def api_rules_save():
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    data = request.get_json(silent=True) or {}
    payload = {}
    for rule, val in (data.get("rules") or {}).items():
        if rule not in checks.REGISTRY:
            continue
        item: dict = {}
        st = val.get("state")
        if st in ("live", "debug", "off"):        # 三态：对外 / 调试中 / 停用
            item["enabled"] = st != "off"
            item["visible"] = st == "live"
        elif "enabled" in val or "visible" in val:  # 老字段，兼容
            if "enabled" in val:
                item["enabled"] = bool(val["enabled"])
            if "visible" in val:
                item["visible"] = bool(val["visible"])
        if val.get("langs") is not None:
            langs = [x for x in (val.get("langs") or []) if x in ("cn", "en")]
            item["langs"] = langs or ["cn", "en"]
        if item:
            payload[rule] = item
    n = ds_rules.save(conn(), payload)
    return jsonify({"ok": True, "saved": n})


# ── 检查项调试预览（仅后台） ─────────────────────────────────────
# 预览页就是「全量问题」页本体（同一套卡片 / 筛选 / 列表），只是允许把这条
# 调试中的检查项放进来（?preview=<rule>），并在顶部多一条工具栏。
LOGS = BASE / "logs"


def _rule_state(rule: str) -> tuple[str, list[str]]:
    """(状态, 生效语言)：off=停用 / live=对外 / debug=调试中。"""
    meta = checks.REGISTRY[rule]
    row = conn().execute("SELECT enabled, visible, langs FROM rule_settings WHERE rule=?",
                         (rule,)).fetchone()
    on = bool(row["enabled"]) if row else bool(meta.get("default_on", True))
    vis = on and (bool(row["visible"]) if row else
                  bool(meta.get("default_visible", meta.get("default_on", True))))
    langs = ([x.strip() for x in (row["langs"] or "").split(",") if x.strip()] if row and row["langs"]
             else list(meta.get("langs") or ("cn", "en")))
    return ("off" if not on else ("live" if vis else "debug")), langs


def _rule_run_state(rule: str) -> dict | None:
    """手工运行的记录（logs/rule_run_<rule>.json），顺便判断进程是否还在。"""
    p = LOGS / f"rule_run_{rule.replace('.', '_')}.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    alive = False
    for item in d.get("procs") or []:
        try:
            os.kill(int(item.get("pid") or 0), 0)
            alive = True
        except (OSError, ValueError):
            pass
    d["running"] = alive
    d["logs"] = [{"name": x.get("log")} for x in (d.get("procs") or [])]
    return d


@app.get("/admin/rule/<rule>")
def admin_rule_page(rule: str):
    if not is_admin():
        return redirect(url_for("admin_page"))
    if rule not in checks.REGISTRY:
        abort(404)
    st, langs = _rule_state(rule)
    c = conn()
    n_open = c.execute("SELECT COUNT(*) FROM issues WHERE rule=? AND status='open'",
                       (rule,)).fetchone()[0]
    n_ign = c.execute("SELECT COUNT(*) FROM issues WHERE rule=? AND status='ignored'",
                      (rule,)).fetchone()[0]
    return render_template(
        "issues.html",
        rule_labels={k: v["label"] for k, v in checks.REGISTRY.items()},
        is_admin_user=True,
        preview={"rule": rule, "label": checks.REGISTRY[rule]["label"], "state": st,
                 "langs": langs, "n_open": n_open, "n_ign": n_ign,
                 "run": _rule_run_state(rule)})


@app.post("/api/admin/rule/<rule>/run")
def api_admin_rule_run(rule: str):
    if not is_admin():
        return jsonify({"ok": False, "error": "未登录"}), 403
    if rule not in checks.REGISTRY:
        return jsonify({"ok": False, "error": "没有这条规则"}), 404
    _st, langs = _rule_state(rule)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOGS.mkdir(exist_ok=True)
    procs = []
    for lang in (langs or ["cn"]):
        log = LOGS / f"rule_{rule.replace('.', '_')}_{lang}_{stamp}.log"
        fh = open(log, "wb")  # noqa: SIM115
        p = subprocess.Popen(
            [sys.executable, str(BASE / "ds_check.py"), "--lang", lang,
             "--section", "ALL", "--rules", rule, "--incremental"],
            cwd=str(BASE), stdout=fh, stderr=subprocess.STDOUT, start_new_session=True)
        procs.append({"lang": lang, "pid": p.pid, "log": log.name})
    (LOGS / f"rule_run_{rule.replace('.', '_')}.json").write_text(json.dumps({
        "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "langs": langs or ["cn"], "procs": procs}, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "langs": langs})


@app.get("/admin/rule/<rule>/log")
def admin_rule_log(rule: str):
    if not is_admin():
        return redirect(url_for("admin_page"))
    name = request.args.get("f", "")
    if not re.fullmatch(r"rule_[a-z0-9_.\-]+\.log", name or ""):
        abort(404)
    p = LOGS / name
    if not p.exists():
        abort(404)
    txt = p.read_text(encoding="utf-8", errors="replace")
    tail = txt[-20000:]        # 只看最后 20KB，够看进度和报错
    return ("<pre style=\"font:12.5px/1.6 ui-monospace,Menlo,monospace;padding:16px;"
            "white-space:pre-wrap\">" + tail.replace("&", "&amp;").replace("<", "&lt;") + "</pre>")


@app.get("/changelog")
def changelog_page():
    """站点动态：规则/功能变更时间线 + AI 可用量（公开）。"""
    import changelog as cl
    cls = {"新增规则": "rule", "规则调整": "tune", "新增功能": "feat",
           "修复": "fix", "安全": "sec", "站点": "site"}
    entries = [{**e, "cls": cls.get(e.get("kind"), "fix")} for e in cl.ENTRIES]
    return render_template("changelog.html", entries=entries)


@app.get("/api/ai/status")
def api_ai_status():
    """给「站点动态」页用的公开额度视图：只给剩余百分比与累计数，不给调用明细。"""
    try:
        import ai
        s = ai.usage_summary()
        loc = s.get("local") or {}
        return jsonify({"ok": bool(s.get("gauges")),
                        "error": s.get("error") or "",
                        "gauges": [{k: g.get(k) for k in
                                    ("label", "remaining", "rem_color", "reset")}
                                   for g in (s.get("gauges") or [])],
                        "today_tokens": loc.get("today_tokens"),
                        "today_tokens_h": loc.get("today_tokens_h"),
                        "calls": loc.get("calls"), "tokens": loc.get("tokens"),
                        "tokens_h": loc.get("tokens_h")})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"额度读取失败：{str(e)[:80]}"}), 200


@app.get("/issues")
def issues_page():
    return render_template("issues.html",
                           rule_labels={k: v["label"] for k, v in checks.REGISTRY.items()},
                           is_admin_user=is_admin())


# ── 个人中心 / 邮件订阅 ──────────────────────────────────────────
SITE_URL = os.environ.get("DOCSTOR_SITE_URL") or "https://docstor.openharmony.cool"
try:
    subcenter.ensure_schema(_db.conn)
    subcenter.register(
        app, str(DB_PATH), where_fn=lambda a: _issue_where(a),
        section_names=SECTION_NAMES, rule_label=checks.rule_label, site_url=SITE_URL)
except Exception as e:  # noqa: BLE001  订阅起不来不能拖垮主站
    app.logger.warning("邮件订阅模块初始化失败: %s", e)


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 3009, debug=False)
