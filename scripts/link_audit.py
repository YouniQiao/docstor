#!/usr/bin/python3
"""全量链接体检：把镜像里所有站内链接的目标逐一对官网核实。

背景：check_dead 对「镜像里有这篇」的目标直接短路，所以官网已下架、
但我们镜像里还留着的页面，指向它的链接永远不会被查。这个脚本不看本地有没有，
直接把 32,044 个唯一目标全部过一遍官方接口。

用法：python3 scripts/link_audit.py [--limit N] [--workers 6] [--out 文件]
结果写 data/link_audit.json（增量落盘，可中断续跑：同文件已有结论会跳过）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ds_official  # noqa: E402

OUT = ROOT / "data" / "link_audit.json"
_lock = threading.Lock()


def main() -> int:
    argv = sys.argv[1:]
    limit = 0
    workers = 6
    out = OUT
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    if "--workers" in argv:
        workers = int(argv[argv.index("--workers") + 1])
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])

    items = json.load(open("/tmp/link_targets.json"))
    targets = [tuple(k) for k, _n in items]
    done = {}
    if out.exists():
        done = {tuple(v["target"]): v for v in json.load(open(out))}
    todo = [t for t in targets if t not in done]
    if limit:
        todo = todo[:limit]
    print(f"  目标 {len(targets)}，已完成 {len(done)}，本次 {len(todo)}，线程 {workers}", flush=True)

    off = ds_official.Official()
    lock = _lock
    results = list(done.values())
    t0 = time.time()

    def probe(t):
        v = off.verdict(t[0], t[1], t[2], refresh=True)
        return {"target": list(t), "verdict": v, "detail": off.detail(*t)}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(probe, t): t for t in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001
                r = {"target": list(futs[fut]), "verdict": "error", "detail": str(e)[:80]}
            with lock:
                results.append(r)
                if i % 200 == 0 or i == len(todo):
                    json.dump(results, open(out, "w"), ensure_ascii=False)
                    rate = i / (time.time() - t0)
                    left = (len(todo) - i) / rate if rate else 0
                    print(f"   {i}/{len(todo)}  {rate:.1f}/s  剩余约 {left/60:.0f} 分钟", flush=True)
    json.dump(results, open(out, "w"), ensure_ascii=False)

    from collections import Counter
    c = Counter(r["verdict"] for r in results)
    print("  结论分布:", dict(c), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
