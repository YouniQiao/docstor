#!/usr/bin/python3
"""把「官网」列用得上的文档真实目录补齐（doc_catalog 表）。

默认只补有未解决问题的文档（列表里点得最多的）；--all 补全库。
"""
from __future__ import annotations

import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_catalog  # noqa: E402

DB = ROOT / "data" / "docstor.db"


def rows(all_docs: bool) -> list[tuple]:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    if all_docs:
        q = ("SELECT DISTINCT d.lang, d.section, d.slug, d.is_api FROM docs d WHERE d.status='ok'")
    else:
        q = ("""SELECT DISTINCT d.lang, d.section, d.slug, d.is_api
                FROM issues i JOIN docs d ON d.doc_key = i.doc_key
                WHERE i.status='open'""")
    return [(r["lang"], r["section"], r["slug"], int(r["is_api"] or 0)) for r in c.execute(q)]


def main() -> int:
    todo = [r for r in rows("--all" in sys.argv) if ds_catalog.get(r[0], r[2]) is None]
    print(f"  待补目录 {len(todo)} 篇（已有缓存跳过）", flush=True)

    def job(r):
        lang, section, slug, is_api = r
        return r, ds_catalog.resolve(lang, section, slug, is_api)

    hit = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, fut in enumerate(as_completed([ex.submit(job, r) for r in todo]), 1):
            r, cat = fut.result()
            hit += bool(cat)
            if i % 100 == 0:
                print(f"   {i}/{len(todo)}  命中 {hit}", flush=True)
            if not cat:
                print(f"   ✗ 查不到目录：{r[0]}/{r[1]}/{r[2]}", flush=True)
    print(f"  完成：{len(todo)} 篇，命中 {hit}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
