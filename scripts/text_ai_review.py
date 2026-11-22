#!/usr/bin/python3
"""文本检查：把候选逐条交 AI 复核，结论写进 ai_notes（不联网的检查时只读库）。

用法：
  text_ai_review.py --rule dup_char --lang cn --workers 5
  text_ai_review.py --rule dup_char --lang cn --limit 20 --dry-run

流程：
  1) 从 docs 读该语言的 md，跑 checks.text_check.find_dup_candidates() 出候选；
  2) 候选键 dup_key(pattern, context, source) 已在 ai_notes(kind='dup_char_review') 的跳过；
  3) 调 AI 判定 real / false_positive / uncertain，写库；再次运行只补新增的。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ai  # noqa: E402
from checks.text_check import NOTE_KIND_DUP, dup_key, find_dup_candidates  # noqa: E402

_print = print
_lock = threading.Lock()


def _p(*a):
    with _lock:
        _print(*a, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", default="dup_char", choices=["dup_char"])
    ap.add_argument("--lang", default="cn", choices=["cn", "en"])
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--select", default="all",
                    choices=["all", "word4", "func", "other"],
                    help="word4=双字词重复(4字)；func=功能字单字重复；other=其余单字重复")
    ap.add_argument("--sample", type=int, default=0, help="从待判里随机抽 N 条先试点")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path = ai.load_config().db_path
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    done_keys = {r[0] for r in conn.execute(
        "SELECT item_key FROM ai_notes WHERE kind=?", (NOTE_KIND_DUP,))}
    docs = conn.execute("SELECT doc_key, md_path FROM docs WHERE lang=? AND md_path<>''",
                        (args.lang,)).fetchall()
    _p(f"{args.lang} 文档 {len(docs):,} 篇，已有 AI 结论 {len(done_keys):,} 条")

    # 单字重复里绝大多数是「时间间隔」「参数数据」这种词界重合，按字的性质先分桶，
    # 便于先试点高价值的那部分，别一上来就把整周额度烧光。
    FUNC = set("的了是在和就都也很与及或我你他她它这那个们把被对为以于而但并且所之着过"
               "吗呢吧啊么有无不没可要会能到从向给让使得地得上下来去出进当初还再又只更最太真已经正")

    def bucket(pattern: str) -> str:
        if len(pattern) >= 4:
            return "word4"
        return "func" if pattern[0] in FUNC else "other"

    todo = []          # [(key, fields, doc_key)]
    seen = set()
    for d in docs:
        p = BASE / d["md_path"]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        for c in find_dup_candidates(text):
            k = dup_key(c["pattern"], c["context"], c["source"])
            if k in done_keys or k in seen:
                continue
            seen.add(k)
            if args.select != "all" and bucket(c["pattern"]) != args.select:
                continue
            where = f"{d['doc_key']} 第 {c['line']} 行（{'正文' if c['source'] == 'prose' else '代码注释'}）"
            todo.append((k, {"pattern": c["pattern"], "where": where, "context": c["context"]}, d["doc_key"]))
    if args.sample and args.sample < len(todo):
        import random
        random.seed(20261122)
        todo = random.sample(todo, args.sample)
    if args.limit:
        todo = todo[:args.limit]
    _p(f"本次待判 {len(todo):,} 条")
    if args.dry_run or not todo:
        for k, f, _dk in todo[:5]:
            _p(f"  {f['pattern']} · {f['where']} · {f['context'][:60]}")
        return 0

    dist: dict[str, int] = {}
    fails = 0
    t0 = time.time()

    def work(item):
        key, fields, _doc = item
        try:
            out = ai.review("dup_char_review", fields, module="text",
                            item_key=key, actor="系统脚本")
            return key, out.get("verdict") or "uncertain", None
        except Exception as e:  # noqa: BLE001
            return key, None, str(e)[:120]

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for i, (key, verdict, err) in enumerate(pool.map(work, todo), 1):
            if err or verdict is None:
                fails += 1
                if fails <= 5:
                    _p(f"  ! {err}")
            else:
                dist[verdict] = dist.get(verdict, 0) + 1
            if i % 50 == 0 or i == len(todo):
                el = time.time() - t0
                _p(f"  {i}/{len(todo)}  {dist}  失败 {fails}  {i / el:.1f} 条/秒")

    _p(f"完成 {len(todo)} 条，失败 {fails}；结论分布：{dist}；用时 {time.time() - t0:.0f}s")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
