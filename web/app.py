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
    status = request.args.get("status", "open")
    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    size = min(200, max(5, int(request.args.get("size", 50))))

    where, args = ["1=1"], []
    if lang:
        where.append("lang=?"); args.append(lang)
    if section:
        where.append("section=?"); args.append(section)
    if rule:
        where.append("rule=?"); args.append(rule)
    if status and status != "all":
        where.append("status=?"); args.append(status)
    if q:
        where.append("(message LIKE ? OR doc_key LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    w = " AND ".join(where)

    total = conn().execute(f"SELECT COUNT(*) n FROM issues WHERE {w}", args).fetchone()["n"]
    rows = conn().execute(
        f"""SELECT id, doc_key, lang, section, rule, severity, locator, message,
                   first_seen, last_seen, status
            FROM issues WHERE {w}
            ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     last_seen DESC, id DESC
            LIMIT ? OFFSET ?""", args + [size, (page - 1) * size]).fetchall()
    items = []
    for r in rows:
        loc = {}
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            pass
        items.append({**{k: r[k] for k in r.keys()},
                      "rule_label": checks.rule_label(r["rule"]),
                      "anchor_text": loc.get("anchor_text", ""),
                      "anchorId": loc.get("anchorId", "")})
    return jsonify({"total": total, "page": page, "size": size,
                    "pages": (total + size - 1) // size, "items": items})


@app.get("/api/facets")
def api_facets():
    by_rule = conn().execute(
        """SELECT rule, COUNT(*) n FROM issues WHERE status='open'
           GROUP BY rule ORDER BY n DESC""").fetchall()
    by_lang = conn().execute(
        """SELECT lang, COUNT(*) n FROM issues WHERE status='open' GROUP BY lang""").fetchall()
    by_section = conn().execute(
        """SELECT lang, section, COUNT(*) n FROM issues WHERE status='open'
           GROUP BY lang, section ORDER BY n DESC""").fetchall()
    return jsonify({
        "rules": [{"rule": r["rule"], "label": checks.rule_label(r["rule"]), "n": r["n"]} for r in by_rule],
        "langs": [{"lang": r["lang"], "n": r["n"]} for r in by_lang],
        "sections": [{"lang": r["lang"], "section": r["section"], "n": r["n"]} for r in by_section],
    })


SECTION_ORDER = ["release-notes", "deveco-studio", "ux-design", "capabilities", "getting-started",
                 "submission", "tools-faq", "advanced-features", "industry-solutions"]
SECTION_NAMES = {
    "release-notes": "版本说明", "deveco-studio": "工具", "ux-design": "设计",
    "capabilities": "指南与API参考", "getting-started": "开发与测试",
    "submission": "上架分发与运营", "tools-faq": "问题处理与运维",
    "advanced-features": "最佳实践", "industry-solutions": "行业解决方案",
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
        sections.append({"k": sec, "n": SECTION_NAMES.get(sec, sec),
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
