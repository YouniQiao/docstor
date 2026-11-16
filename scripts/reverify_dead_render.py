#!/usr/bin/python3
"""用修正后的判据（看 h1 + 正文长度）重渲染当前 link.dead 的全部目标。

输出 data/dead_render_check2.json，并把能打开的目标列出来。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_render  # noqa: E402

DB = ROOT / "data" / "docstor.db"
OUT = ROOT / "data" / "dead_render_check2.json"


def targets() -> dict:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    seen = defaultdict(list)
    for r in c.execute("select id, doc_key, locator from issues where rule='link.dead' and status='open'"):
        t = json.loads(r["locator"]).get("target", "")
        p = [x for x in t.split("/") if x]
        if len(p) >= 3:
            seen[(p[0], p[1], p[2].split("#")[0])].append((r["id"], r["doc_key"]))
    return seen


def main() -> int:
    ts = targets()
    print(f"  待渲染目标 {len(ts)} 个", flush=True)

    def job(k):
        lang, cat, slug = k
        if slug.endswith(".md"):
            slug = slug[:-3]
        try:
            return k, ds_render.check(lang, cat, slug, refresh=True)
        except Exception as e:                       # noqa: BLE001
            return k, f"error:{type(e).__name__}"

    rows = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for i, fut in enumerate(as_completed([ex.submit(job, k) for k in ts]), 1):
            k, v = fut.result()
            rows.append({"target": list(k), "verdict": v, "issues": ts[k]})
            print(f"   {i}/{len(ts)} {'/'.join(k)} → {v}", flush=True)
            json.dump(rows, open(OUT, "w"), ensure_ascii=False, indent=1)
    ok = [r for r in rows if r["verdict"] == "ok"]
    print(f"  可打开 {len(ok)} 个目标，涉及 {sum(len(r['issues']) for r in ok)} 条问题", flush=True)
    for r in ok:
        print("   ✔", "/".join(r["target"]), f"（{len(r['issues'])} 条）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
