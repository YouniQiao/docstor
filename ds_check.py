#!/usr/bin/python3
"""docstor 检查运行器。

流程（对应需求第 6 条）：
  · 首次全量跑出的问题 = 基线（baseline=1）
  · 增量发现问题 INSERT 并入基线，同时记住 first_seen
  · 每日复核：本次没再出现的问题 → status='resolved'

用法：
  python3 ds_check.py --lang cn --section capabilities                 # 全板块规则类检查
  python3 ds_check.py --lang cn --section capabilities --limit 20      # 试跑
  python3 ds_check.py --lang cn --section capabilities --external      # 同时探外链
  python3 ds_check.py --lang en --section capabilities --rules link.anchor
"""
from __future__ import annotations

import argparse
import concurrent.futures
import sys
import threading
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import checks  # noqa: E402
import ds_db  # noqa: E402
import ds_alias  # noqa: E402
import ds_rules  # noqa: E402
from checks.base import DocContext  # noqa: E402

BASE = Path(__file__).resolve().parent
_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(msg, flush=True)


class Ctx(DocContext):
    """检查上下文。外链探测统一走 ds_http（带分档缓存），这里不再自带一套缓存。"""


def run(args) -> int:
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    db = ds_db.DB(str(BASE / cfg["db"]["path"]))
    docs_root = BASE / cfg["site"]["docs_dir"]

    rows = db.docs_in_section(args.lang, args.section)
    if args.slug:
        rows = [r for r in rows if r["slug"] in set(args.slug)]
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        log("没有可检查的文档（先跑 ds_ingest.py）")
        return 1

    ready_sections = {(r["lang"], r["section"]) for r in db.conn.execute(
        "SELECT DISTINCT lang, section FROM docs WHERE status='ok'")}

    if args.rules:  # 命令行显式指定优先，忽略管理页开关
        rules = [r for r in dict.fromkeys(args.rules)
                 if r in checks.REGISTRY and checks.applicable(r, args.lang)]
    else:  # 以管理页配置为准（表里没有的按出厂默认）
        ds_rules.ensure(db.conn, checks.REGISTRY)
        rules = [r for r in ds_rules.effective(db.conn, checks.REGISTRY, args.lang)
                 if checks.applicable(r, args.lang)]
    log(f"🔍 检查 {args.lang}/{args.section}：{len(rows)} 篇，规则 {len(rules)} 个 → {', '.join(rules)}")

    run_id = db.start_run("check", f"{args.lang}/{args.section}")
    t0 = time.time()
    total_new = total_seen = 0
    by_rule: dict[str, int] = {}
    failures: list[str] = []

    def work(row):
        doc = dict(row)
        md_path = BASE / doc["md_path"] if doc["md_path"] else None
        if not md_path or not md_path.exists():
            return doc, [], -1
        ctx = Ctx(doc, md_path, db=db)
        ctx.external = args.external
        ctx.sections = ready_sections
        found = []
        for rule in rules:
            fn = checks.REGISTRY[rule]["fn"]
            try:
                for it in fn(doc, ctx) or []:
                    found.append(it)
            except Exception as e:  # noqa: BLE001
                failures.append(f"{doc['slug']}/{rule}: {e}")
        resolved = 0
        return doc, found, resolved

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, r) for r in rows]
        for fut in concurrent.futures.as_completed(futs):
            doc, found, _ = fut.result()
            done += 1
            keep = set()
            for it in found:
                payload = {"doc_key": f"{doc['lang']}/{doc['section']}/{doc['slug']}",
                           "lang": doc["lang"], "section": doc["section"],
                           "rule": it.rule, "severity": it.severity or checks.REGISTRY[it.rule]["severity"],
                           "locator": __import__("json").dumps(
                               {**it.locator, "anchor_text": it.anchor_text}, ensure_ascii=False),
                           "message": it.message,
                           "evidence": it.evidence}
                created = db.upsert_issue(payload, baseline=not args.incremental, run_id=run_id)
                total_new += 1 if created else 0
                total_seen += 1
                by_rule[it.rule] = by_rule.get(it.rule, 0) + 1
                keep.add(ds_db.fprint(payload["doc_key"], it.rule,
                                      payload["locator"], it.message))
            db.resolve_missing(f"{doc['lang']}/{doc['section']}/{doc['slug']}", keep, run_id,
                               rules=set(rules))
            if done % 100 == 0 or done == len(rows):
                log(f"   进度 {done}/{len(rows)}  本次问题 {total_seen}（新增 {total_new}）"
                    f"  {done/(time.time()-t0):.1f}篇/s")

    stats = {"docs": len(rows), "issues": total_seen, "new": total_new,
             "by_rule": by_rule, "elapsed_s": round(time.time() - t0, 1),
             "failures": failures[:20]}
    db.finish_run(run_id, stats)
    try:                              # 旧地址→新地址别名表，供镜像站跳转
        log(f"   🔗 地址别名 {ds_alias.build(db.conn)} 条")
    except Exception as e:  # noqa: BLE001
        log(f"   ⚠ 别名表更新失败：{e}")
    db.close()

    log(f"\n✅ 检查完成：{len(rows)} 篇，问题 {total_seen}（新增 {total_new}），"
        f"用时 {stats['elapsed_s']}s")
    for k, v in sorted(by_rule.items(), key=lambda x: -x[1]):
        log(f"   {checks.rule_label(k):14s} {v}")
    if failures:
        log(f"   ⚠ 检查异常 {len(failures)} 处，例：{failures[:3]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="docstor 检查")
    ap.add_argument("--lang", default="cn", choices=["cn", "en"])
    ap.add_argument("--section", default="ALL", help="板块别名；ALL=库里所有已抓板块")
    ap.add_argument("--slug", action="append", help="只查指定 slug（可多次）")
    ap.add_argument("--rules", action="append", help="只跑指定规则（可多次）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--external", action="store_true", help="启用到外网的链接探测")
    ap.add_argument("--incremental", action="store_true", help="增量模式（不标 baseline）")
    args = ap.parse_args()
    if args.section.upper() != "ALL":
        return run(args)
    import yaml
    cfg = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
    db = ds_db.DB(str(BASE / cfg["db"]["path"]))
    secs = [r["section"] for r in db.conn.execute(
        "SELECT DISTINCT section FROM docs WHERE lang=? AND status='ok' ORDER BY section", (args.lang,))]
    rc = 0
    log(f"🔎 依次检查 {args.lang} 全部 {len(secs)} 个板块：{', '.join(secs)}")
    for s in secs:
        args.section = s
        rc |= run(args)
    return rc


if __name__ == "__main__":
    sys.exit(main())
