#!/usr/bin/python3
"""建 doc_cat 表：把每篇文档归到侧边栏的目录层级，供「全量问题」按侧边栏筛选。

列含义：
  sub     一级目录（capabilities 下是 指南 / API参考；其余板块是它们唯一的一级目录）
  domain  二级目录（只有 capabilities 用：应用框架 / 系统 / 媒体 …）
  kit     三级目录（只有 capabilities 用：ArkUI（方舟UI框架） / Ability Kit（程序框架服务） …）

问题表只有 doc_key（语言/板块/slug），没有目录信息，所以要靠 nodes 表沿 parent 往上走。
未归类（nodes 里没有这个 slug，例如早期从老目录补抓的文档）不写进表。

用法：python3 scripts/build_doc_cat.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data/docstor.db"

# 版本说明/工具/设计/行业解决方案只有一个一级目录，不需要细分
SUBS_SECTIONS = ("getting-started", "submission", "tools-faq", "advanced-features",
                 "capabilities")

SCHEMA = """CREATE TABLE IF NOT EXISTS doc_cat(
    lang TEXT, section TEXT, slug TEXT,
    sub TEXT, domain TEXT, kit TEXT,
    sort INTEGER, sort_domain INTEGER, sort_kit INTEGER,
    sub_id TEXT, domain_id TEXT, kit_id TEXT,
    PRIMARY KEY(lang, section, slug))"""


def main() -> int:
    db = sqlite3.connect(DB)
    db.execute("DROP TABLE IF EXISTS doc_cat")
    db.execute(SCHEMA)
    nodes: dict[tuple, dict] = {}
    for r in db.execute("SELECT lang, section, node_id, parent, name, slug, depth, is_leaf, sort"
                        " FROM nodes"):
        lang, section, node_id, parent, name, slug, depth, is_leaf, sort = r
        nodes[(lang, section, node_id)] = dict(node_id=node_id, parent=parent, name=name,
                                               slug=slug, depth=depth, is_leaf=is_leaf,
                                               sort=sort or 0)

    def anc(lang: str, section: str, node_id: str, depth: int):
        cur = nodes.get((lang, section, node_id))
        hops = 0
        while cur and hops < 12:
            if cur["depth"] == depth:
                return cur
            cur = nodes.get((lang, section, cur["parent"]))
            hops += 1
        return None

    rows = []
    for (lang, section, node_id), n in nodes.items():
        if section not in SUBS_SECTIONS or not n["is_leaf"] or not n["slug"]:
            continue
        d1 = anc(lang, section, node_id, 1)
        if not d1:
            continue
        d2 = anc(lang, section, node_id, 2) if section == "capabilities" else None
        d3 = anc(lang, section, node_id, 3) if section == "capabilities" else None
        rows.append((lang, section, n["slug"],
                     d1["name"], d2["name"] if d2 else None, d3["name"] if d3 else None,
                     d1["sort"], d2["sort"] if d2 else 0, d3["sort"] if d3 else 0,
                     d1["node_id"], d2["node_id"] if d2 else None, d3["node_id"] if d3 else None))

    db.executemany("INSERT OR REPLACE INTO doc_cat(lang,section,slug,sub,domain,kit,"
                   "sort,sort_domain,sort_kit,sub_id,domain_id,kit_id)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    for r in db.execute("SELECT lang, section, sub, COUNT(*) FROM doc_cat"
                        " GROUP BY 1,2,3 ORDER BY 1,2,MIN(sort)"):
        print(f"  {r[0]} {r[1]:18s} {r[2]:32s} {r[3]:5d}")
    print(f"  ✅ doc_cat：{len(rows)} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
