#!/usr/bin/python3
"""核对「官网」列：有未解决问题的文档，其官网地址是不是真能打开。

「官网」列地址来自 doc_catalog（目录树索引/板块/全目录实测三级解析）。
索引是抓目录树时生成的，官网撤稿或改目录后索引会滞后，于是列表里
仍显示一个 404 地址。这里对「有问题的这批文档」逐个问内容接口核实：

  · 原目录能用        → 不动
  · 换目录能用        → 改 doc_catalog（地址随之修正）
  · 全目录都查不到    → 置空（UI 显示「已下架」，不再给死链）

用法：python3 scripts/verify_official.py [--apply]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_api      # noqa: E402
import ds_catalog  # noqa: E402
import ds_official  # noqa: E402

DB = ROOT / "data" / "docstor.db"
OUT = ROOT / "data" / "official_check.json"


def candidates(lang: str, section: str, slug: str) -> list[str]:
    cats: list[str] = []
    idx = ds_catalog._from_index(lang, slug)
    if idx:
        cats.append(idx)
    if section:
        cats += list(ds_official.SECTION_CATS.get(section, []))
    cats += [c for pre, c in ds_catalog.PREFIX_CATS if slug.startswith(pre)]
    cats += list(ds_official.ALL_CATS)
    cats += ds_catalog._all_catalogs()
    return list(dict.fromkeys([c for c in cats if c]))


def main() -> int:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT DISTINCT doc_key FROM issues WHERE status='open'").fetchall()
    print(f"  待核对文档 {len(rows)} 篇", flush=True)

    def job(doc_key: str):
        lang, section, slug = doc_key.split("/", 2)
        api = ds_api.DocCenter()
        cur = ds_catalog.get(lang, slug)
        if cur:
            d = api._post("getDocumentById",
                          {"objectId": slug, "catalogName": cur, "language": lang})
            if d.get("code") == 0:
                return doc_key, "ok", cur
        for cand in candidates(lang, section, slug):
            if cand == cur:
                continue
            d = api._post("getDocumentById",
                          {"objectId": slug, "catalogName": cand, "language": lang})
            if d.get("code") == 0:
                return doc_key, "moved", cand
        return doc_key, "gone", ""

    res: dict[str, list] = {"ok": [], "moved": [], "gone": []}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(job, r["doc_key"]): r["doc_key"] for r in rows}
        for i, f in enumerate(as_completed(futs), 1):
            try:
                doc_key, st, cat = f.result()
            except Exception as e:  # noqa: BLE001
                print(f"   ! {futs[f]}: {e}", flush=True)
                continue
            res[st].append({"doc_key": doc_key, "catalog": cat})
            if i % 100 == 0:
                print(f"   {i}/{len(rows)} ok={len(res['ok'])} moved={len(res['moved'])}"
                      f" gone={len(res['gone'])}", flush=True)

    print(f"  结论：能打开 {len(res['ok'])}，目录变了 {len(res['moved'])}，"
          f"官网查不到 {len(res['gone'])}", flush=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    if apply:
        n = 0
        for st in ("moved", "gone"):
            for item in res[st]:
                lang, _section, slug = item["doc_key"].split("/", 2)
                ds_catalog.put(lang, slug, item["catalog"])
                n += 1
        conn.commit()
        print(f"  已写回 doc_catalog {n} 条", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
