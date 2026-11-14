#!/usr/bin/python3
"""按数据库实际抓取情况重建首页 `site/docs/_home.md`。

板块顺序、名称与官网左侧一致；卡片入口取该板块目录树里第一个文档页。
每次抓取新板块后跑一次即可（幂等）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "site/docs/_home.md"

# 与官网左侧目录（及 /api/tree 的 SECTION_ORDER）保持同一顺序
SECTION_ORDER = [
    ("release-notes", "版本说明"),
    ("deveco-studio", "工具"),
    ("ux-design", "设计"),
    ("capabilities", "指南与API参考"),
    ("getting-started", "开发与测试"),
    ("submission", "上架分发与运营"),
    ("tools-faq", "问题处理与运维"),
    ("advanced-features", "最佳实践"),
    ("industry-solutions", "行业解决方案"),
]


def main() -> int:
    db = sqlite3.connect(BASE / "data/docstor.db")
    db.row_factory = sqlite3.Row

    rows = db.execute(
        """SELECT section, COUNT(*) n FROM docs WHERE lang='cn' AND status='ok'
           GROUP BY section""").fetchall()
    counts = {r["section"]: r["n"] for r in rows}

    cards = []
    total = 0
    for sec, name in SECTION_ORDER:
        n = counts.get(sec, 0)
        if not n:
            continue
        total += n
        first = db.execute(
            """SELECT slug FROM nodes WHERE lang='cn' AND section=? AND slug IS NOT NULL
               AND hided=0 ORDER BY sort, id LIMIT 1""", (sec,)).fetchone()
        href = f"#/cn/{sec}/{first['slug']}" if first else f"#/cn/{sec}/"
        cards.append(
            f'  <a class="ds-card" href="{href}">\n'
            f'    <h3>{name}</h3>\n'
            f'    <div class="n">中文 · {n:,} 篇</div>\n'
            f'  </a>')

    body = f"""<div class="ds-hero">
  <h1>docstor</h1>
  <p>HarmonyOS 开发者文档镜像站 · 与官网层级一致 · 每日同步 · 内置质量检查</p>
</div>

<div class="ds-cards">
{chr(10).join(cards)}
</div>

<p style="margin-top:26px">
  <span style="color:#6b7280;font-size:12px">已镜像中文 {total:,} 篇</span>
</p>
"""
    OUT.write_text(body, encoding="utf-8")
    print(f"  首页已重建：{len(cards)} 个板块，共 {total:,} 篇 → {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
