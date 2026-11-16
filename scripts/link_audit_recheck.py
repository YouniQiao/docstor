#!/usr/bin/python3
"""第三轮：把 dead / cn_only 的旧结论用当前判据全部重判一遍，回写 data/link_audit.json。"""
from __future__ import annotations

import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_official  # noqa: E402

OUT = ROOT / "data" / "link_audit.json"


def main() -> int:
    res = json.load(open(OUT))
    todo = [r for r in res if r["verdict"] in ("dead", "cn_only")]
    print(f"  重判 {len(todo)} 个目标", flush=True)
    off = ds_official.Official()

    def probe(r):
        t = tuple(r["target"])
        v = off.verdict(t[0], t[1], t[2], refresh=True)
        return r, v, off.detail(*t)

    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, fut in enumerate(as_completed([ex.submit(probe, r) for r in todo]), 1):
            r, v, d = fut.result()
            r["verdict"], r["detail"] = v, d
            if i % 50 == 0:
                print(f"   {i}/{len(todo)}", flush=True)
    json.dump(res, open(OUT, "w"), ensure_ascii=False)
    print("  结论:", dict(Counter(r["verdict"] for r in res)), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
