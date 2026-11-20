#!/usr/bin/python3
"""把「已经过期」的缓存结论重判一遍（/admin 缓存页的「重判」按钮调这个）。

用法：
  python3 scripts/recheck_cache.py --kind official   # link_official 里过期的
  python3 scripts/recheck_cache.py --kind http       # link_http 里过期的

只重判、不清表：「过期」的定义直接复用 ds_cache.expired_where()，和看板上的
「已过期」列是同一份 SQL，不会出现「看板说 994 条、实际只跑 900 条」。
进度写进 data/.cache_recheck.json，/admin 缓存页轮询显示。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_cache  # noqa: E402

STATE = BASE / "data" / ".cache_recheck.json"
COLS = {"official": "lang, section, slug", "http": "url"}


def log(msg: str) -> None:
    print(msg, flush=True)


def _write_state(d: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["official", "http"], required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="只重判前 N 条（调试用）")
    args = ap.parse_args()

    table = "link_official" if args.kind == "official" else "link_http"
    con = sqlite3.connect(ds_cache.DB, timeout=30)
    con.row_factory = sqlite3.Row
    rows = con.execute(f"SELECT {COLS[args.kind]} FROM {table} "
                       f"WHERE {ds_cache.expired_where(table)}").fetchall()
    con.close()
    if args.limit:
        rows = rows[: args.limit]
    total = len(rows)

    state = {"kind": args.kind, "count": total, "done": 0, "ok": 0, "err": 0,
             "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "finished_at": None}
    _write_state(state)
    log(f"🔁 重判 {table}：{total} 条（{args.workers} 并发）")
    if not total:
        state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_state(state)
        return 0

    if args.kind == "official":
        import ds_official
        off = ds_official.Official()

        def work(r):
            return off.verdict(r["lang"], r["section"], r["slug"], refresh=True)
    else:
        import ds_http

        def work(r):
            st, _final, err = ds_http.status(r["url"], refresh=True)
            return ds_http.verdict(st, err)

    t0 = time.time()
    done = 0
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, r) for r in rows]
        for f in cf.as_completed(futs):
            done += 1
            try:
                f.result()
                state["ok"] += 1
            except Exception as e:  # noqa: BLE001
                state["err"] += 1
                if state["err"] <= 5:
                    log(f"   ⚠ {e}")
            if done % 25 == 0 or done == total:
                state["done"] = done
                _write_state(state)
                log(f"   进度 {done}/{total}  {done / max(time.time() - t0, 1e-6):.1f} 条/s")
    state["done"] = total
    state["seconds"] = round(time.time() - t0, 1)
    state["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_state(state)
    log(f"✅ 重判完成：{total} 条，{state['seconds']}s，失败 {state['err']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
