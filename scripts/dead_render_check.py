#!/usr/bin/python3
"""把当前 link.dead 的每个唯一目标用无头 Chromium 真渲染一遍，找出误报。

输出 data/dead_render_check.json：[{target, verdict, title, bytes, issues}]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_render  # noqa: E402

DB = ROOT / "data" / "docstor.db"
OUT = ROOT / "data" / "dead_render_check.json"


def targets():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    seen = {}
    for r in c.execute("select id, doc_key, locator from issues where rule='link.dead' and status='open'"):
        t = json.loads(r["locator"]).get("target", "")
        p = [x for x in t.split("/") if x]
        if len(p) < 3:
            continue
        key = (p[0], p[1], p[2].split("#")[0])
        seen.setdefault(key, []).append((r["id"], r["doc_key"]))
    return seen


def main() -> int:
    ts = targets()
    print(f"  唯一目标 {len(ts)} 个", flush=True)
    rows = []

    def job(k):
        lang, cat, slug = k
        slug = slug[:-3] if slug.endswith(".md") else slug
        try:
            v = ds_render.check(lang, cat, slug, refresh=True)
        except Exception as e:                     # noqa: BLE001
            v = f"error:{type(e).__name__}"
        return k, v

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(job, k): k for k in ts}
        for i, fut in enumerate(as_completed(futs), 1):
            k, v = fut.result()
            rows.append({"target": list(k), "verdict": v, "issues": ts[k]})
            print(f"   {i}/{len(ts)} {'/'.join(k)} → {v}", flush=True)
    json.dump(rows, open(OUT, "w"), ensure_ascii=False, indent=1)
    ok = [r for r in rows if r["verdict"] == "ok"]
    print(f"  渲染结论：可打开 {len(ok)} 个目标 / 共 {len(rows)}，涉及 "
          f"{sum(len(r['issues']) for r in ok)} 条问题", flush=True)
    for r in ok:
        print("   ✔", "/".join(r["target"]), f"（{len(r['issues'])} 条）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
