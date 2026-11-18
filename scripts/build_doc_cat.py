#!/usr/bin/python3
"""建 doc_cat 表：把每篇文档归到侧边栏的目录层级，供「全量问题」按侧边栏筛选。

列含义：
  sub     细分（侧边栏里的«第一层有意义的分组»）
            - 大多数板块：一级目录（如 工具→开发工具 只有一个，就往下取「DevEco Studio / DevEco CLI …」）
            - capabilities：指南 / API参考
  domain  二级目录（只有 capabilities 用：应用框架 / 系统 / 媒体 …）
  kit     三级目录（只有 capabilities 用：ArkUI（方舟UI框架） / Ability Kit（程序框架服务） …）

「第一层有意义的分组」判定：自 depth=1 往下找第一个「不同名字 ≥ 2」的层级；
只有一层（像「工具」下面唯一的一级目录「开发工具」）就取它下面那层，
避免细分下拉里只出现一个没信息量的选项。cn/en 各自判定，所以中英文层级可以不同。

例外：NO_SUB_SECTIONS 里的板块不做细分（设计——用户 2026-11-18 定），sub 写空，
问题页的细分下拉自动置灰。

问题表只有 doc_key（语言/板块/slug），没有目录信息，所以要靠 nodes 表沿 parent 往上走。
未归类（nodes 里没有这个 slug）不写进表。

用法：python3 scripts/build_doc_cat.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path


def kit_key(name: str | None) -> str | None:
    """Kit 归一化名：去掉「（程序框架服务）」这类中英文括注 + 统一大小写。

    中文站 `Ability Kit（程序框架服务）` 与英文站 `Ability Kit` 是同一个 Kit，
    归一后都是 `ability kit`，前端才能一个 Kit 只列一条、跨语言一起筛。
    """
    if not name:
        return None
    s = name
    for _ in range(3):
        nxt = re.sub(r"[（(][^）)]*[）)]", "", s)
        if nxt == s:
            break
        s = nxt
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s or None

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "docstor.db"

SCHEMA = """CREATE TABLE IF NOT EXISTS doc_cat(
    lang TEXT, section TEXT, slug TEXT,
    sub TEXT, domain TEXT, kit TEXT, kit_key TEXT,
    sort INTEGER, sort_domain INTEGER, sort_kit INTEGER,
    sub_id TEXT, domain_id TEXT, kit_id TEXT,
    PRIMARY KEY(lang, section, slug))"""

# 不做细分的板块：设计（用户要求，细分对这一栏没意义）
NO_SUB_SECTIONS = {"ux-design"}


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

    # 每个 (lang, section) 的「细分取第几层」
    names_by_depth: dict[tuple, dict[int, set]] = {}
    for (lang, section, _nid), n in nodes.items():
        names_by_depth.setdefault((lang, section), {}).setdefault(n["depth"], set()).add(n["name"])
    sub_depth: dict[tuple, int] = {}
    for key, depths in names_by_depth.items():
        pick = 1
        for d in (1, 2):
            if len(depths.get(d, ())) >= 2:
                pick = d
                break
        else:
            # 前两层都只有一个名字：一级目录只是「开发工具」这种壳，取它下面那层（如
            # 英文站工具下只有 DevEco Studio (Windows/macOS Edition)），不要继续往内容章节沉
            pick = 2 if depths.get(2) else 1
        sub_depth[key] = pick

    def anc(lang: str, section: str, node_id: str, depth: int):
        cur = nodes.get((lang, section, node_id))
        hops = 0
        while cur and hops < 14:
            if cur["depth"] == depth:
                return cur
            cur = nodes.get((lang, section, cur["parent"]))
            hops += 1
        return None

    rows = []
    seen: dict[tuple, dict] = {}
    # 同一 slug 可能挂在多个节点下（目录页既是分组又带正文），取层最浅的那个，结果稳定
    for (lang, section, node_id), n in nodes.items():
        if not n["slug"]:
            continue
        key = (lang, section, n["slug"])
        cur_best = seen.get(key)
        if cur_best is None or (n["depth"], n["sort"]) < (cur_best["depth"], cur_best["sort"]):
            seen[key] = n

    for (lang, section, slug), n in seen.items():
        if section in NO_SUB_SECTIONS:
            rows.append((lang, section, n["slug"], None, None, None, None,
                         0, 0, 0, None, None, None))
            continue
        node_id = n["node_id"]
        sd = sub_depth.get((lang, section), 1)
        d1 = anc(lang, section, node_id, sd) or anc(lang, section, node_id, 1) or n
        if not d1:
            continue
        d2 = anc(lang, section, node_id, 2) if section == "capabilities" else None
        d3 = anc(lang, section, node_id, 3) if section == "capabilities" else None
        k3 = d3["name"] if d3 else (d2["name"] if d2 else None)
        rows.append((lang, section, n["slug"],
                     d1["name"], d2["name"] if d2 else None, d3["name"] if d3 else None,
                     kit_key(k3),
                     d1["sort"], d2["sort"] if d2 else 0, d3["sort"] if d3 else 0,
                     d1["node_id"], d2["node_id"] if d2 else None, d3["node_id"] if d3 else None))

    db.executemany("INSERT OR REPLACE INTO doc_cat(lang,section,slug,sub,domain,kit,kit_key,"
                   "sort,sort_domain,sort_kit,sub_id,domain_id,kit_id)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    for r in db.execute("SELECT lang, section, sub, COUNT(*) FROM doc_cat"
                        " GROUP BY 1,2,3 ORDER BY 1,2,MIN(sort)"):
        print(f"  {r[0]} {r[1]:18s} {str(r[2])[:34]:36s} {r[3]:5d}")
    print(f"  ✅ doc_cat：{len(rows)} 条 | 细分层级 {sub_depth}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
