#!/usr/bin/env python3
"""回填每篇文档的“更新时间”（官网 displayUpdateTime，UTC→北京时间），
并为每个板块导出 _meta.json 供前端在标题下显示「更新时间」。

只调用接口写库，不改动任何 md / 图片文件。
用法：python3 scripts/backfill_update.py [--lang cn] [--workers 6] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_api  # noqa: E402
import ds_db    # noqa: E402

CFG = None
CST = timezone(timedelta(hours=8))


def to_cst(s: str) -> str:
    """'2026-09-09 06:54:05' (UTC 字符串) → '2026-09-09 14:54'"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s.strip()[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
        except Exception:
            continue
    return s.strip()[:16]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="cn")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    import yaml
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    db = ds_db.DB(str(BASE / cfg["db"]["path"]))
    api = ds_api.DocCenter(base=cfg["api"]["base"])

    rows = db.conn.execute(
        "SELECT doc_key, section, slug, alias, is_api FROM docs WHERE lang=? AND status='ok'",
        (args.lang,)).fetchall()
    todo = [dict(r) for r in rows]
    if args.limit:
        todo = todo[: args.limit]
    print(f"  待回填 {len(todo)} 篇（workers={args.workers}）", flush=True)

    meta: dict[str, dict[str, str]] = {}
    t0 = time.time()
    done = ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(api.document, d["slug"], d["alias"] or d["section"],
                          args.lang, bool(d["is_api"])): d for d in todo}
        for fut in as_completed(futs):
            d = futs[fut]
            done += 1
            try:
                res = fut.result()
            except Exception as e:                      # noqa: BLE001
                print(f"    ! {d['doc_key']}: {e}", flush=True)
                continue
            upd = res.get("updated") if res.get("ok") else ""
            if upd:
                val = to_cst(upd)
                db.conn.execute("UPDATE docs SET updated_at=? WHERE doc_key=?", (val, d["doc_key"]))
                meta.setdefault(d["section"], {})[d["slug"]] = val
                ok += 1
            if done % 500 == 0:
                sp = done / max(time.time() - t0, 0.001)
                print(f"    进度 {done}/{len(todo)} ok={ok} {sp:.1f}篇/s", flush=True)
    db.conn.commit()

    # 每个板块导出 _meta.json
    for section, kv in meta.items():
        p = BASE / "site" / "docs" / args.lang / section / "_meta.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(kv, ensure_ascii=False), encoding="utf-8")
        print(f"  → {p.relative_to(BASE)}（{len(kv)} 篇）", flush=True)

    print(f"  ✅ 完成：{ok}/{len(todo)} 篇写入更新时间，用时 {time.time()-t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
