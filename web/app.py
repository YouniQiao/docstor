#!/usr/bin/python3
"""docstor Web：docsify 站点 + 问题 API + 全量问题页。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import checks  # noqa: E402
import ds_db  # noqa: E402

CFG = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
SITE = BASE / CFG["site"]["output_dir"]
DB_PATH = BASE / CFG["db"]["path"]

app = Flask(__name__, template_folder=str(BASE / "web" / "templates"))
app.config["TEMPLATES_AUTO_RELOAD"] = True

_db = ds_db.DB(str(DB_PATH))


def conn():
    return _db.conn


# ---------------- 静态站点 ----------------
@app.route("/")
def index():
    return send_from_directory(SITE, "index.html")


@app.route("/docs/<path:fn>")
def docs(fn):
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
@app.get("/api/sidebar-counts")
def api_sidebar_counts():
    lang = request.args.get("lang", "cn")
    section = request.args.get("section", "")
    rows = conn().execute(
        """SELECT doc_key, rule, COUNT(*) n FROM issues
           WHERE lang=? AND section=? AND status='open' GROUP BY doc_key, rule""",
        (lang, section)).fetchall()
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
    rows = conn().execute(
        """SELECT rule, severity, locator, message FROM issues
           WHERE doc_key=? AND status='open'
           ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                    rule, id""", (doc_key,)).fetchall()
    issues = []
    for r in rows:
        loc = {}
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        issues.append({"rule": r["rule"], "rule_label": checks.rule_label(r["rule"]),
                       "severity": r["severity"], "message": r["message"],
                       "locator": r["locator"],
                       "anchor_text": loc.get("anchor_text", ""),
                       "anchorId": loc.get("anchorId", "")})
    return jsonify({"issues": issues, "total": len(issues)})


@app.get("/api/issues")
def api_issues():
    lang = request.args.get("lang", "")
    section = request.args.get("section", "")
    rule = request.args.get("rule", "")
    sub = request.args.get("sub", "")
    domain = request.args.get("domain", "")
    kit = request.args.get("kit", "")
    kind = request.args.get("kind", "")
    status = request.args.get("status", "open")
    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    size = min(200, max(5, int(request.args.get("size", 50))))

    where, args = ["1=1"], []
    if lang:
        where.append("i.lang=?"); args.append(lang)
    if section:
        where.append("i.section=?"); args.append(section)
    if rule:
        where.append("i.rule=?"); args.append(rule)
    if sub:
        parts = sub.split("|")
        if len(parts) == 3:
            where.append("i.doc_key IN (SELECT c.lang||'/'||c.section||'/'||c.slug FROM doc_cat c"
                         " WHERE c.lang=? AND c.section=? AND c.sub=?)")
            args += parts
    # 指南与API参考：按侧边栏二级（领域）与三级（Kit）筛选
    for val, col in ((domain, "domain"), (kit, "kit")):
        if not val:
            continue
        parts = val.split("|")
        if len(parts) == 3:
            where.append(f"i.doc_key IN (SELECT c.lang||'/'||c.section||'/'||c.slug"
                         f" FROM doc_cat c WHERE c.lang=? AND c.section=? AND c.{col}=?)")
            args += parts
    if kind in ("api", "guide"):
        # COUNT 查询没有 join docs，统一用子查询，避免两边 SQL 不一致
        where.append("i.doc_key IN (SELECT doc_key FROM docs WHERE is_api=?)")
        args.append(1 if kind == "api" else 0)
    if status and status != "all":
        where.append("i.status=?"); args.append(status)
    if q:
        where.append("(i.message LIKE ? OR i.doc_key LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    w = " AND ".join(where)

    total = conn().execute(f"SELECT COUNT(*) n FROM issues i WHERE {w}", args).fetchone()["n"]
    rows = conn().execute(
        f"""SELECT i.id, i.doc_key, i.lang, i.section, i.rule, i.severity, i.locator, i.message,
                   i.first_seen, i.last_seen, i.status, d.url AS doc_url, d.is_api
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
        official = r["doc_url"] or (ds_db.official_url(r["lang"], r["section"], parts[-1] or "",
                                                      int(r["is_api"] or 0)) if len(parts) == 3 else "")
        items.append({**{k: r[k] for k in r.keys()},
                      "rule_label": checks.rule_label(r["rule"]),
                      "anchor_text": loc.get("anchor_text", ""),
                      "anchorId": loc.get("anchorId", ""),
                      "official_url": official})
    return jsonify({"total": total, "page": page, "size": size,
                    "pages": (total + size - 1) // size, "items": items})


def _section_opts(rows):
    """板块下拉用文档侧边栏的板块名（版本说明/工具…），一个板块一项、中英文合并计数。

    中英文同名板块共用同一个筛选值，分成两项反而会互相串，所以合并。
    """
    agg: dict[str, int] = {}
    for r in rows:
        agg[r["section"]] = agg.get(r["section"], 0) + (r["n_all"] or 0)
    out = [{"section": k, "n": v, "name": SECTION_NAMES.get(k, k)} for k, v in agg.items()]
    out.sort(key=lambda s: SECTION_ORDER.index(s["section"]) if s["section"] in SECTION_ORDER else 99)
    return out


def _cat_opts(col: str, sort_col: str):
    """doc_cat 某目录列 → 选项清单（0 条问题的也列出来，置灰显示）。"""
    try:
        rows = conn().execute(
            f"""SELECT c.lang, c.section, c.{col} AS v, MIN(c.{sort_col}) s,
                       COUNT(i.id) n_all, SUM(CASE WHEN i.status='open' THEN 1 ELSE 0 END) n
                FROM doc_cat c LEFT JOIN issues i
                  ON i.doc_key = c.lang || '/' || c.section || '/' || c.slug
                WHERE c.{col} IS NOT NULL AND c.{col} != ''
                GROUP BY 1,2,3 ORDER BY 1,2,s""").fetchall()
    except Exception:  # noqa: BLE001  doc_cat 还没建
        return []
    return [{"lang": r["lang"], "section": r["section"], "name": r["v"],
             "n": r["n"] or 0, "n_all": r["n_all"] or 0} for r in rows]


def _sub_opts():
    return _cat_opts("sub", "sort")


def _domain_opts():
    return _cat_opts("domain", "sort_domain")


def _kit_opts():
    """Kit 选项要带上所属领域，前端选中领域后才列出该领域下的 Kit。"""
    try:
        rows = conn().execute(
            """SELECT c.lang, c.section, c.domain, c.kit AS v, MIN(c.sort_kit) s,
                      COUNT(i.id) n_all, SUM(CASE WHEN i.status='open' THEN 1 ELSE 0 END) n
               FROM doc_cat c LEFT JOIN issues i
                 ON i.doc_key = c.lang || '/' || c.section || '/' || c.slug
               WHERE c.kit IS NOT NULL AND c.kit != ''
               GROUP BY 1,2,3,4 ORDER BY 1,2,MIN(c.sort_domain),s""").fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [{"lang": r["lang"], "section": r["section"], "domain": r["domain"], "name": r["v"],
             "n": r["n"] or 0, "n_all": r["n_all"] or 0} for r in rows]


@app.get("/api/facets")
def api_facets():
    by_rule = conn().execute(
        """SELECT rule, COUNT(*) n_all, SUM(status='open') n FROM issues
           GROUP BY rule ORDER BY n DESC""").fetchall()
    by_lang = conn().execute(
        """SELECT lang, COUNT(*) n FROM issues WHERE status='open' GROUP BY lang""").fetchall()
    by_section = conn().execute(
        """SELECT lang, section, COUNT(*) n_all, SUM(status='open') n FROM issues
           GROUP BY lang, section ORDER BY n DESC""").fetchall()
    return jsonify({
        "rules": [{"rule": r["rule"], "label": checks.rule_label(r["rule"]),
                   "n": r["n"], "n_all": r["n_all"]} for r in by_rule],
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


@app.get("/api/tree")
def api_tree():
    """整站目录树（层级与官网一致）+ 各页问题数，供左侧侧边栏一次取用。"""
    lang = request.args.get("lang", "cn")
    stamp = conn().execute("SELECT COALESCE(MAX(id),0) m FROM nodes").fetchone()["m"]
    cached = _TREE_CACHE.get(lang)
    if cached and cached[0] == stamp:
        return jsonify(cached[1])

    rows = conn().execute(
        "SELECT section, node_id, parent, name, slug, sort, id FROM nodes "
        "WHERE lang=? AND hided=0 ORDER BY section, sort, id", (lang,)).fetchall()
    nodes: dict[str, dict] = {}
    children: dict[str, list[str]] = {}
    roots: dict[str, list[str]] = {}
    for r in rows:
        key = f"{r['section']}:{r['node_id']}"
        nodes[key] = {"i": key, "n": r["name"], "s": r["slug"] or None, "k": r["section"]}
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

    issues = {}
    for r in conn().execute(
            "SELECT lang, section, doc_key, COUNT(*) n FROM issues WHERE status='open' "
            "AND lang=? GROUP BY section, doc_key", (lang,)):
        issues[f"{r['section']}/{r['doc_key'].rsplit('/', 1)[-1]}"] = r["n"]

    payload = {"lang": lang, "sections": sections, "issues": issues}
    _TREE_CACHE[lang] = (stamp, payload)
    return jsonify(payload)


# ---------------- 全量问题页 ----------------
@app.get("/issues")
def issues_page():
    return render_template("issues.html", rule_labels={k: v["label"] for k, v in checks.REGISTRY.items()})


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(sys.argv[1]) if len(sys.argv) > 1 else 3009, debug=False)
