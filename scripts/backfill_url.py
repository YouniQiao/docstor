#!/usr/bin/env python3
"""回填 docs.url（官网原文地址）并重写各板块 _meta.json。

URL 的目录段（catname）用 ds_url 解析：官网 sitemap 锚点 + 目录树投票，
比「板块 → 目录」硬映射准（同一板块下会挂多个目录）。
不需要重新抓取文档，纯本地计算。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_db  # noqa: E402
import ds_url  # noqa: E402


def main() -> int:
    cfg = yaml.safe_load(open(BASE / "config.yaml", encoding="utf-8"))
    db = ds_db.DB(str(BASE / cfg["db"]["path"]))
    langs = [r["lang"] for r in db.conn.execute(
        "SELECT DISTINCT lang FROM docs WHERE status='ok'")]

    for lang in langs:
        cat = ds_url.resolve(db.conn, lang, BASE / "data")
        n = 0
        for doc_key, c in cat.items():
            slug = doc_key.split("/", 2)[2]
            db.conn.execute("UPDATE docs SET url=? WHERE doc_key=?",
                            (ds_url.url_for(lang, c, slug), doc_key))
            n += 1
        db.conn.commit()
        print(f"  {lang}: 写入 url {n} 篇，目录段分布：")
        for c, k in db.conn.execute(
                "SELECT substr(url, instr(url,'/doc/')+5, instr(substr(url,instr(url,'/doc/')+5),'/')-1) cat, count(*) "
                "FROM docs WHERE lang=? AND status='ok' GROUP BY cat ORDER BY 2 DESC", (lang,)):
            print(f"    {c:32s} {k:6d}")

        total = 0
        for section, in db.conn.execute(
                "SELECT DISTINCT section FROM docs WHERE lang=? AND status='ok'", (lang,)):
            out = BASE / cfg["site"]["docs_dir"] / lang / section
            total += db.export_meta(lang, section, out)
        print(f"  {lang}: _meta.json 合计 {total} 条")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
