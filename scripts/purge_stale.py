#!/usr/bin/env python3
"""清理「官网目录树里已经没有」的陈旧镜像文档（默认干跑，--apply 才动手）。

背景：官网会悄悄把一批文档从目录树里摘掉（内容接口随后返回 92511002/92531031），
但撤稿逻辑有保险——一次判定删除超过 max(50, 5%) 就整批跳过，于是这些文档长期留在镜像里：
既不刷新内容，又每晚被检查，问题数虚高、侧栏却没有入口。

本脚本做的事：
  1. 找 docs.status='ok' 且 slug 不在本板块 nodes 里的文档（默认只处理 --lang en）
  2. 逐篇用内容接口复核：只有明确返回「文档不存在」的错误码才认定可删；
     仍能取到正文、或请求异常（网络/超时）的一律保留并记日志
  3. --apply 时：md 与独占图片移入 data/trash/<时间>/、docs 置 gone、
     其未解决问题置 resolved（否则会变成永远清不掉的僵尸问题）、
     并写 sync_items(kind=deleted)，让 /sync 能看到这次清理

用法：
  python3 scripts/purge_stale.py --lang en            # 干跑，只打印
  python3 scripts/purge_stale.py --lang en --apply    # 真删（先进回收站，可回滚）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_api          # noqa: E402
import ds_db           # noqa: E402
import ds_ingest       # noqa: E402

# 内容接口「文档不存在」的两种返回码（目录不对 = 92531031，文档没了 = 92511002）
GONE_CODES = {92511002, 92531031}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", action="append", choices=["cn", "en"], default=None)
    ap.add_argument("--section", action="append", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    langs = args.lang or ["en"]

    db_path = BASE / "data" / "docstor.db"
    doc_dir = BASE / "site" / "docs"
    db = ds_db.DB(str(db_path))
    cfg = ds_ingest.load_config()
    dc = ds_api.DocCenter(base=cfg["api"]["base"], center_prefix=cfg["api"]["center_prefix"],
                          referer=cfg["api"]["referer"], timeout=cfg["api"]["timeout"],
                          retries=cfg["api"]["retries"], ua=cfg["api"]["user_agent"])

    total_gone, total_kept, total_unknown, total_issues = 0, 0, 0, 0
    run_id = None
    if args.apply:
        run_id = db.start_run("ingest", "purge-stale")

    for lang in langs:
        q = ("SELECT d.doc_key, d.section, d.slug, d.title, d.alias, d.md_path FROM docs d "
             "WHERE d.lang=? AND d.status='ok' AND NOT EXISTS ("
             "  SELECT 1 FROM nodes n WHERE n.lang=d.lang AND n.section=d.section AND n.slug=d.slug)")
        rows = db.conn.execute(q, (lang,)).fetchall()
        if args.section:
            rows = [r for r in rows if r["section"] in set(args.section)]
        print(f"\n  {lang}: 待复核 {len(rows)} 篇（不在目录树里）")

        by_sec: dict[str, list] = defaultdict(list)
        for r in rows:
            res = dc.document(r["slug"], r["alias"] or r["section"], lang)
            code = res.get("code")
            if res.get("ok"):
                total_kept += 1
                print(f"    = 仍可取正文，保留：{r['doc_key']}")
                continue
            if code not in GONE_CODES:
                total_unknown += 1
                print(f"    ? 接口返回 code={code}，无法确认，保留：{r['doc_key']}")
                continue
            total_gone += 1
            by_sec[r["section"]].append(dict(r))
            print(f"    - 官网已无（code={code}）：{r['doc_key']}")

        for section, gone in by_sec.items():
            print(f"\n  {lang}/{section}: 确认删除 {len(gone)} 篇")
            if not args.apply:
                continue
            anchors_path = doc_dir / lang / section / "_anchors.json"
            anchors_map = {}
            if anchors_path.is_file():
                try:
                    anchors_map = json.loads(anchors_path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    anchors_map = {}
            db.mark_docs_gone([g["doc_key"] for g in gone])
            db.add_sync_items(run_id, [{
                "doc_key": g["doc_key"], "kind": "deleted", "lang": lang, "section": section,
                "slug": g["slug"], "title": g["title"] or "",
                "extra": json.dumps({"why": "不在官网目录树且内容接口返回不存在"}, ensure_ascii=False),
            } for g in gone])
            n_moved = ds_ingest.prune_gone_files(db, doc_dir / lang / section, lang, section,
                                                 gone, anchors_map)
            anchors_path.write_text(json.dumps(anchors_map, ensure_ascii=False, indent=0),
                                    encoding="utf-8")
            # 这些文档不会再被检查，未解决问题不能留着当僵尸
            n_iss = 0
            for g in gone:
                cur = db.conn.execute(
                    "UPDATE issues SET status='resolved', resolved_at=?, run_id=? "
                    "WHERE doc_key=? AND status='open'", (ds_db.now(), run_id, g["doc_key"]))
                n_iss += cur.rowcount
            db.conn.commit()
            total_issues += n_iss
            print(f"     移动 md {n_moved} 个；关闭未解决问题 {n_iss} 条")

    print(f"\n  合计：确认删除 {total_gone} 篇 | 保留（仍可访问）{total_kept} | 无法确认 {total_unknown}"
          + (f" | 关闭问题 {total_issues} 条" if args.apply else ""))
    if not args.apply:
        print("  这是干跑，什么都没动；确认无误后加 --apply")
    elif run_id:
        db.conn.execute(
            "UPDATE runs SET stats=? WHERE id=?",
            (json.dumps({"added": 0, "modified": 0, "deleted": total_gone,
                         "elapsed_s": 0, "note": "清理不在目录树的历史陈旧文档"}, ensure_ascii=False),
             run_id))
        db.conn.commit()
        db.finish_run(run_id, {"deleted": total_gone, "note": "清理陈旧文档"})
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
