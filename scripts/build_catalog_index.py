#!/usr/bin/python3
"""建立「官网目录索引」：每个 catalogName 的完整目录树 → 表 catalog_index。

为什么需要它：官网的链接是否失效，只能看官网自己有没有这篇文档。
文档中心有**两套**目录接口：
  · getCenterCatalogTree(level2NodeAlias) —— 新版开发者文档中心，我们抓正文用的
  · getCatalogTree(catalogName)          —— 老版目录（harmonyos-guides 等），
       官网对新版没有的页面会回退到这套老目录，页面照样能打开
只按新版目录判断，会把「老目录里还有、官网能正常打开」的页面误判成死链
（例如被引用 125 次的 application-dev-overview，它只在老目录里）。

索引每天/每周重建一次即可，检查阶段查表，不打网络。
"""
from __future__ import annotations

import collections
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent   # 仓库根
sys.path.insert(0, str(BASE))                    # 允许从 scripts/ 目录直接运行

import ds_api  # noqa: E402
DB = BASE / "data/docstor.db"
SITEMAP = {"cn": BASE / "data/sitemap_doc_cn.xml"}

SCHEMA = """CREATE TABLE IF NOT EXISTS catalog_index(
    lang TEXT, catalog TEXT, slug TEXT, title TEXT, node_id TEXT, path TEXT,
    PRIMARY KEY(lang, catalog, slug))"""

# 老目录里额外的、sitemap 不一定覆盖的
EXTRA = ["harmonyos-guides", "harmonyos-references", "harmonyos-faqs", "harmonyos-releases",
         "best-practices", "atomic-guides", "atomic-faqs", "atomic-releases", "app"]


def candidates(lang: str) -> list[str]:
    names = list(EXTRA)
    sm = SITEMAP.get(lang)
    if sm and sm.exists() and sm.stat().st_size > 0:
        xml = sm.read_text(encoding="utf-8", errors="ignore")
        names += re.findall(rf"/consumer/{lang}/doc/([^/<]+)/", xml)
    # 历史版本目录（harmonyos-guides-V13 等）官网也还在服务，旧链接能打开 → 一并收进索引
    return list(dict.fromkeys(names))


def walk(nodes, trail=()):
    for n in nodes or []:
        name = (n.get("nodeName") or "").strip()
        slug = n.get("relateDocument")
        if isinstance(slug, str) and slug.strip():
            yield slug.strip(), name, str(n.get("nodeId") or ""), "/".join(trail)
        kids = n.get("catalogTreeList") or n.get("children") or []
        yield from walk(kids, trail + (name,) if name else trail)


def main(argv: list[str]) -> int:
    langs = [a.split("=", 1)[1] for a in argv if a.startswith("--lang=")] or ["cn", "en"]
    api = ds_api.DocCenter()
    db = sqlite3.connect(str(DB))
    db.execute(SCHEMA)
    for lang in langs:
        total = 0
        for name in candidates(lang):
            try:
                d = api._post("getCatalogTree", {"catalogName": name, "lang": lang})
            except Exception:  # noqa: BLE001
                continue
            if d.get("code") != 0 or not d.get("value"):
                continue
            v = d["value"]
            rows = collections.OrderedDict()
            for slug, title, nid, path in walk(v.get("catalogTreeList") or []):
                rows.setdefault(slug, (title, nid, path))
            if not rows:
                continue
            db.executemany(
                "INSERT OR REPLACE INTO catalog_index(lang,catalog,slug,title,node_id,path)"
                " VALUES(?,?,?,?,?,?)",
                [(lang, name, s, t, i, p) for s, (t, i, p) in rows.items()])
            db.commit()
            total += len(rows)
            print(f"  {lang}/{name:32s} {len(rows):5d} 条", flush=True)
        print(f"  ✅ {lang} 共 {total} 条索引", flush=True)
    cnt = db.execute("SELECT lang, COUNT(DISTINCT slug) FROM catalog_index GROUP BY 1").fetchall()
    print("  索引唯一 slug:", cnt)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
