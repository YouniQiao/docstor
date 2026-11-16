#!/usr/bin/python3
"""逐目标复核 link.dead，能打开（渲染 ok 或接口判 ok/renamed）的直接删掉问题行。

判据优先级：
  1. data/dead_render_check.json 里渲染结论为 ok（最硬，页面真打开了）
  2. ds_official.verdict(refresh=True) 为 ok / renamed
其余保留。结果打印后由 --apply 落库。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_official  # noqa: E402

DB = ROOT / "data" / "docstor.db"
RENDER = ROOT / "data" / "dead_render_check.json"


def main(apply: bool) -> int:
    rendered = {}
    if RENDER.exists():
        for r in json.load(open(RENDER)):
            rendered[tuple(r["target"])] = r["verdict"]
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    by_target = defaultdict(list)
    for r in c.execute("select id, doc_key, locator from issues where rule='link.dead' and status='open'"):
        t = json.loads(r["locator"]).get("target", "")
        p = [x for x in t.split("/") if x]
        if len(p) >= 3:
            by_target[(p[0], p[1], p[2].split("#")[0])].append((r["id"], r["doc_key"]))
    off = ds_official.Official()
    stat = Counter()
    drop = []
    for t, ids in by_target.items():
        v = rendered.get(t)
        if v != "ok":
            v = off.verdict(t[0], t[1], t[2], refresh=True)
        if v in ("ok", "renamed"):
            stat[v] += len(ids)
            drop += [i for i, _ in ids]
            print(f"   ✂ {'/'.join(t)} → {v}（{len(ids)} 条）", flush=True)
        else:
            stat["keep"] += len(ids)
    print(f"  合计: 删除 {len(drop)} 条，保留 {stat['keep']} 条", flush=True)
    if apply and drop:
        c.executemany("DELETE FROM issues WHERE id=?", [(i,) for i in drop])
        c.commit()
        left = c.execute("select count(*) from issues where rule='link.dead' and status='open'").fetchone()[0]
        print(f"  已删除，link.dead 剩余 {left} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
