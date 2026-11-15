#!/usr/bin/python3
"""按数据库实际抓取情况重建首页（`site/docs/_home.md` 中文、`site/docs/_home_en.md` 英文）。

板块顺序、名称与官网左侧一致；卡片入口取该板块目录树里第一个文档页。
每次抓取新板块后跑一次即可（幂等）。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

# 与官网左侧目录（及 /api/tree 的 SECTION_ORDER）保持同一顺序
SECTIONS = [
    ("release-notes", "版本说明", "Release Notes"),
    ("deveco-studio", "工具", "Tools"),
    ("ux-design", "设计", "Design"),
    ("capabilities", "指南与API参考", "Guides & API References"),
    ("getting-started", "开发与测试", "Develop & Test"),
    ("submission", "上架分发与运营", "Publish & Distribute"),
    ("tools-faq", "问题处理与运维", "Support & FAQ"),
    ("advanced-features", "最佳实践", "Best Practices"),
    ("industry-solutions", "行业解决方案", "Industry Solutions"),
]

LANGS = {
    "cn": dict(
        out="_home.md",
        h1="鸿蒙开发者文档",
        sub="HarmonyOS 开发者文档镜像站 · 与官网层级一致 · 每日同步 · 内置质量检查",
        card_note="中文 · {n:,} 篇",
        total="已镜像中文 {n:,} 篇",
        pick=1,
    ),
    "en": dict(
        out="_home_en.md",
        h1="HarmonyOS Developer Documentation",
        sub="Mirror of the official HarmonyOS developer documentation · Same structure as the official site · Synced daily · Built-in quality checks",
        card_note="English · {n:,} docs",
        total="Mirrored English docs: {n:,}",
        pick=2,
    ),
}


def build(db, lang: str) -> int:
    m = LANGS[lang]
    counts = {r["section"]: r["n"] for r in db.execute(
        "SELECT section, COUNT(*) n FROM docs WHERE lang=? AND status='ok' GROUP BY section",
        (lang,))}

    cards = []
    total = 0
    for row in SECTIONS:
        sec = row[0]
        name = row[m["pick"]]
        n = counts.get(sec, 0)
        if not n:
            continue
        total += n
        first = db.execute(
            "SELECT slug FROM nodes WHERE lang=? AND section=? AND slug IS NOT NULL "
            "AND hided=0 ORDER BY sort, id LIMIT 1", (lang, sec)).fetchone()
        href = f"#/{lang}/{sec}/{first['slug']}" if first else f"#/{lang}/{sec}/"
        cards.append(
            f'  <a class="ds-card" href="{href}">\n'
            f'    <h3>{name}</h3>\n'
            f'    <div class="n">{m["card_note"].format(n=n)}</div>\n'
            f'  </a>')

    body = f"""<div class="ds-hero">
  <h1>{m["h1"]}</h1>
  <p>{m["sub"]}</p>
</div>

<div class="ds-cards">
{chr(10).join(cards)}
</div>

<p style="margin-top:26px">
  <span style="color:#6b7280;font-size:12px">{m["total"].format(n=total)}</span>
</p>
"""
    out = BASE / "site/docs" / m["out"]
    out.write_text(body, encoding="utf-8")
    # docsify 把 `#/en` 这样的路由解析成 `<basePath>/en.md`（不是 en/README.md），
    # 所以语言首页要各写一份 <lang>.md；<lang>/README.md 一并保留，兼容带斜杠的写法
    docs_dir = BASE / "site/docs"
    (docs_dir / f"{lang}.md").write_text(body, encoding="utf-8")
    lang_home = docs_dir / lang / "README.md"
    lang_home.parent.mkdir(parents=True, exist_ok=True)
    lang_home.write_text(body, encoding="utf-8")
    print(f"  首页已重建（{lang}）：{len(cards)} 个板块，共 {total:,} 篇 → {out} / {lang}.md")
    return total


def main() -> int:
    db = sqlite3.connect(BASE / "data/docstor.db")
    db.row_factory = sqlite3.Row
    for lang in LANGS:
        if db.execute("SELECT 1 FROM docs WHERE lang=? AND status='ok' LIMIT 1", (lang,)).fetchone():
            build(db, lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
