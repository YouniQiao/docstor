#!/usr/bin/python3
"""docstor Web：docsify 站点 + 问题 API + 全量问题页。"""
from __future__ import annotations

import hmac
import json
import os
import re
from datetime import datetime
import sys
from pathlib import Path

import yaml
from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import checks  # noqa: E402
import auth as gcauth  # noqa: E402  GitCode 登录（web/auth.py）
import issueq  # noqa: E402  筛选条件构造（列表/导出/订阅共用）
import subs as subcenter  # noqa: E402  个人中心 + 邮件订阅
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

gcauth.init_app(app)

_db = ds_db.DB(str(DB_PATH))
ds_rules.ensure(_db.conn, checks.REGISTRY)
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
def _enabled_rules() -> list[str]:
    """对外可见的检查项（enabled & visible）。

    关闭的、以及「调试中」（跑但不对外）的检查项都不进全量问题：
    卡片、列表、侧栏、文档问题面板、facets、导出、订阅预览一律不显示。
    """
    try:
        return ds_rules.visible_rules(conn(), checks.REGISTRY)
    except Exception:  # noqa: BLE001
        return []


def _enabled_filter(col: str = "rule") -> tuple[str, list]:
    rules = _enabled_rules()
    if not rules:
        return "1=0", []
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


@app.get("/api/issues")
def api_issues():
    page = max(1, int(request.args.get("page", 1)))
    size = min(200, max(5, int(request.args.get("size", 50))))
    w, args = _issue_where()
    total = conn().execute(f"SELECT COUNT(*) n FROM issues i WHERE {w}", args).fetchone()["n"]
    rows = conn().execute(
        f"""SELECT i.id, i.doc_key, i.lang, i.section, i.rule, i.severity, i.locator, i.message,
                   i.first_seen, i.last_seen, i.status, i.ignored_by, i.ignored_at,
                   d.url AS doc_url, d.is_api
            FROM issues i LEFT JOIN docs d ON d.doc_key = i.doc_key
            WHERE {w}
            ORDER BY CASE i.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     i.last_seen DESC, i.id DESC
            LIMIT ? OFFSET ?""", args + [size, (page - 1) * size]).fetchall()
    items = []
    for r in rows:
        loc = {}
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        doc_key = r["doc_key"] or ""
        parts = doc_key.split("/")
        # 官网原文地址：按实测目录拼（板块 ≠ 目录时按板块拼会 404，如 games 文档在 games-guides 下）
        official = (ds_catalog.url_for(r["lang"], r["section"], parts[-1] or "", int(r["is_api"] or 0))
                    if len(parts) == 3 else "")
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
    if not user:
        return jsonify({"ok": False, "need_login": True, "error": "请先用 GitCode 登录"}), 401
    row = _db.get_issue(iid)
    if row is None:
        return jsonify({"ok": False, "error": "问题不存在"}), 404
    if row["status"] == "resolved":
        return jsonify({"ok": False, "error": "已解决的问题不能忽略"}), 400
    _db.set_ignored(iid, gcauth.login_name())
    cur = _db.get_issue(iid)
    return jsonify({"ok": True, "id": iid, "status": cur["status"],
                    "ignored_by": cur["ignored_by"], "ignored_at": cur["ignored_at"]})


@app.post("/api/issues/<int:iid>/unignore")
def api_issue_unignore(iid: int):
    """取消忽略，回到未解决（需 GitCode 登录）。"""
    if not gcauth.current_user():
        return jsonify({"ok": False, "need_login": True, "error": "请先用 GitCode 登录"}), 401
    if not _db.clear_ignored(iid):
        return jsonify({"ok": False, "error": "这条问题不是已忽略状态"}), 400
    return jsonify({"ok": True, "id": iid, "status": "open"})


EXPORT_COLS = [
    ("问题类型", 18), ("语言", 8), ("板块", 18), ("细分/领域", 22), ("Kit", 24),
    ("文档类别", 10), ("问题描述", 80), ("原文链接文字", 30), ("原文目标地址", 60),
    ("镜像文档", 52), ("官网文档", 52), ("状态", 10), ("识别日期", 12),
    ("最近发现", 20), ("解决时间", 20), ("忽略人", 16), ("忽略时间", 20),
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
                  SUM(COALESCE(json_extract(stats,'$.elapsed_s'),0)) secs
           FROM runs WHERE kind='ingest' GROUP BY d ORDER BY d DESC LIMIT 120""").fetchall()
    docs_total = c.execute("SELECT COUNT(*) n FROM docs WHERE status='ok'").fetchone()["n"]
    last = c.execute("SELECT started_at FROM runs WHERE kind='ingest' "
                     "ORDER BY id DESC LIMIT 1").fetchone()
    secs = [r["section"] for r in c.execute(
        "SELECT DISTINCT section FROM docs WHERE status='ok' ORDER BY 1")]
    return jsonify({
        "today": datetime.now().strftime("%Y-%m-%d"),
        "docs_total": docs_total,
        "last_run": last["started_at"] if last else None,
        "days": [dict(r) for r in rows],
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
    page = max(1, int(request.args.get("page", 1)))
    size = min(200, max(10, int(request.args.get("size", 50))))

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
                     "i.old_version, r.started_at, d.is_api " + base +
                     " ORDER BY CASE i.kind WHEN 'deleted' THEN 1 ELSE 0 END, i.id DESC"
                     " LIMIT ? OFFSET ?", args + [size, (page - 1) * size]).fetchall()
    items = []
    for r in rows:
        official = ""
        if r["slug"]:
            try:
                official = ds_catalog.url_for(r["lang"], r["section"], r["slug"],
                                              int(r["is_api"] or 0))
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


@app.get("/api/export.xlsx")
def api_export():
    """按当前筛选条件导出全部问题（不分页），筛选维度一并作为列。"""
    import io
    from datetime import datetime

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    w, args = _issue_where()
    rows = conn().execute(
        f"""SELECT i.doc_key, i.lang, i.section, i.rule, i.severity, i.locator, i.message,
                   i.first_seen, i.last_seen, i.status, i.resolved_at,
                   i.ignored_by, i.ignored_at,
                   d.is_api, d.url AS doc_url, c.sub, c.domain, c.kit
            FROM issues i
            LEFT JOIN docs d ON d.doc_key = i.doc_key
            LEFT JOIN doc_cat c ON c.lang||'/'||c.section||'/'||c.slug = i.doc_key
            WHERE {w}
            ORDER BY CASE i.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     i.rule, i.last_seen DESC, i.id DESC""", args).fetchall()

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

    for r in rows:
        doc_key = r["doc_key"] or ""
        parts = doc_key.split("/")
        slug = parts[-1] if parts else ""
        loc = {}
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        official = (ds_catalog.url_for(r["lang"], r["section"], slug, int(r["is_api"] or 0))
                    if len(parts) == 3 else "")
        official = official or (r["doc_url"] or "")   # 兜底：docs.url 是抓取时用的真实官网地址
        href, link_text = _link_meta(doc_key, loc)
        anchor = loc.get("anchorId") or ""
        mirror = f"{base}/#/{doc_key}" + (f"?id={anchor}" if anchor else "")
        sub = r["domain"] or r["sub"] or ""
        ws.append([
            checks.rule_label(r["rule"]),
            "中文" if r["lang"] == "cn" else "英文",
            SECTION_NAMES.get(r["section"], r["section"]),
            sub,
            r["kit"] or "",
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
            {"error": "错误", "warn": "警告", "info": "提示"}.get(r["severity"], r["severity"]),
            doc_key,
        ])
        row_i = ws.max_row
        for col_i, val in ((9, href), (10, mirror), (11, official)):
            if val:
                c = ws.cell(row=row_i, column=col_i)
                c.hyperlink = val
                c.font = link_font
        for col_i in (7, 8, 9):
            ws.cell(row=row_i, column=col_i).alignment = Alignment(vertical="top", wrap_text=False)

    ws.auto_filter.ref = f"A1:{get_column_letter(len(EXPORT_COLS))}{ws.max_row}"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    name = f"全量问题_{stamp}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

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


@app.get("/api/facets")
def api_facets():
    fw, fa = _enabled_filter()
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

    return jsonify({
        "rules": [dict({"rule": r["rule"], "label": checks.rule_label(r["rule"]),
                        "n": r["n"], "n_all": r["n_all"]},
                       enabled=_rule_state(r["rule"])[0],
                       langs=_rule_state(r["rule"])[1]) for r in by_rule],
        "langs": [{"lang": r["lang"], "n": r["n"]} for r in by_lang],
        "sections": _section_opts(by_section),
        "subs": _sub_opts(),
        "domains": _domain_opts(),
        "kits": _kit_opts(),
    })


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
    cached = _TREE_CACHE.get(lang)
    if cached and cached[0] == stamp and cached[1] == sig:
        return jsonify(cached[2])

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
    return jsonify(payload)
    return jsonify(payload)


# ---------------- 全量问题页 ----------------
# 检查项分类：规则前缀 → 页面上的分组名（顺序即展示顺序；没有规则的分组不显示）
RULE_GROUP_NAMES = {"link": "链接检查", "image": "图片检查", "text": "文本检查"}
RULE_GROUP_ORDER = ["link", "image", "text"]


@app.route("/admin", methods=["GET", "POST"])
def admin_page():
    err = ""
    if request.method == "POST":
        pwd = request.form.get("password", "")
        if _ENV.get("ADMIN_PASSWORD") and hmac.compare_digest(pwd, _ENV["ADMIN_PASSWORD"]):
            session["admin"] = True
            return redirect(url_for("admin_page"))
        err = "口令不正确"
    if not is_admin():
        return render_template("admin.html", authed=False, err=err, rules=[])
    cfg = ds_rules.all_settings(conn())
    counts = {r["rule"]: r["n"] for r in conn().execute(
        "SELECT rule, COUNT(*) n FROM issues WHERE status='open' GROUP BY rule")}
    # 检查项按规则前缀归类：link.* → 链接检查、image.* → 图片检查、text.* → 文本检查，
    # 以后加的新检查项只要用对应前缀，就会自动落到该分类里，不用改这里
    buckets: dict[str, list] = {}
    for rule, meta in checks.REGISTRY.items():
        c = cfg.get(rule) or {}
        on = c.get("enabled", meta.get("default_on", True))
        vis = bool(on) and c.get("visible", meta.get("default_visible", meta.get("default_on", True)))
        buckets.setdefault(rule.split(".", 1)[0], []).append({
            "rule": rule, "label": meta["label"],
            "severity": meta.get("severity", "warn"),
            "enabled": on, "visible": vis,
            "state": "off" if not on else ("live" if vis else "debug"),
            "langs": c.get("langs", list(meta.get("langs") or ("cn", "en"))),
            "n": counts.get(rule, 0),
            "updated_at": c.get("updated_at", "")})
    groups = [{"name": RULE_GROUP_NAMES[key], "rules": buckets[key],
               "n": sum(r["n"] for r in buckets[key])}
              for key in RULE_GROUP_ORDER if buckets.get(key)]
    return render_template("admin.html", authed=True, err="", groups=groups)


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


@app.get("/issues")
def issues_page():
    return render_template("issues.html", rule_labels={k: v["label"] for k, v in checks.REGISTRY.items()})


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
