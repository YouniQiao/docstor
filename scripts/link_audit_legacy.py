#!/usr/bin/python3
"""补判「老板块名」目标的结论。

link_audit.py 用的是「板块 → 官网目录」映射，遇到 /cn/games、/cn/multi-device
这类已经改名的板块会查不到。本脚本对这些目标：
  ① 镜像里同 slug 有这篇 → 直接记 local_ok（站内已能跳到该篇，见 Flask 跨板块兜底）
  ② 否则用全目录候选重新问官网
结果写回 data/link_audit.json，并打印汇总。
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_official  # noqa: E402

MIRRORED = {"advanced-features", "capabilities", "deveco-studio", "getting-started",
            "industry-solutions", "release-notes", "submission", "tools-faq", "ux-design"}
OUT = ROOT / "data" / "link_audit.json"


def local_sections(lang: str, slug: str) -> bool:
    base = ROOT / "site" / "docs" / lang
    if not base.is_dir():
        return False
    return any((d / f"{slug}.md").exists() for d in base.iterdir() if d.is_dir())


def main() -> int:
    results = json.load(open(OUT))
    by_t = {tuple(r["target"]): r for r in results}
    legacy = [t for t in by_t if t[1] not in MIRRORED]
    print(f"  老板块名目标 {len(legacy)}", flush=True)
    off = ds_official.Official()
    todo = []
    for t in legacy:
        if local_sections(t[0], t[2]):
            by_t[t]["verdict"] = "local_ok"
            by_t[t]["detail"] = "镜像里有同篇"
        else:
            todo.append(t)
    print(f"  镜像里有同篇 {len(legacy) - len(todo)}，需问官网 {len(todo)}", flush=True)

    def probe(t):
        v = off.verdict(t[0], t[1], t[2], refresh=True)
        return t, v, off.detail(*t)

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(probe, t) for t in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            t, v, d = fut.result()
            by_t[t]["verdict"], by_t[t]["detail"] = v, d
            if i % 100 == 0:
                print(f"   {i}/{len(todo)}", flush=True)

    json.dump(list(by_t.values()), open(OUT, "w"), ensure_ascii=False)
    c = Counter(r["verdict"] for r in by_t.values())
    print("  全部结论:", dict(c), flush=True)
    dead = [t for t, r in by_t.items() if r["verdict"] == "dead"]
    print(f"  判死 {len(dead)}；其中老板块名 {sum(1 for t in dead if t[1] not in MIRRORED)}", flush=True)
    leg_dead = Counter(t[1] for t in dead if t[1] not in MIRRORED)
    print("  老板块名死链分布:", leg_dead.most_common(10), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
