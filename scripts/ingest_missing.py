#!/usr/bin/python3
"""补抓官网有、镜像缺的文档（link.mirror_missing 的来源）。

用 SPA 自己的接口 getDocumentById({objectId: slug, catalogName: cat, lang}) 取正文，
catalogName 从 ds_official.SECTION_CATS 里按板块逐个试，其余流程与 ds_ingest 一致：
图片落到 <板块>/images、正文转换、写 _meta.json（更新时间 + 原文链接）。

用法：
  python3 scripts/ingest_missing.py --lang cn --dry-run
  python3 scripts/ingest_missing.py --lang cn --limit 20
  python3 scripts/ingest_missing.py --lang cn --lang en
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_api            # noqa: E402
import ds_convert        # noqa: E402
import ds_db             # noqa: E402
import ds_ingest         # noqa: E402
import ds_url            # noqa: E402
from ds_official import SECTION_CATS   # noqa: E402


def targets(conn, lang: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT locator FROM issues WHERE rule='link.mirror_missing' AND lang=? AND status='open'",
        (lang,)).fetchall()
    out = {}
    for (loc,) in rows:
        t = json.loads(loc).get("target") or ""
        parts = t.lstrip("/").split("#")[0].split("/")
        if len(parts) == 3 and parts[0] == lang:
            out[(parts[1], parts[2])] = True
    return sorted(out)


def fetch(api, lang: str, section: str, slug: str):
    for cat in SECTION_CATS.get(section, [section]):
        try:
            d = api._post("getDocumentById",
                          {"objectId": slug, "catalogName": cat, "language": lang})
        except Exception:  # noqa: BLE001
            continue
        if d.get("code") == 0 and (d.get("value") or {}).get("content"):
            return cat, d["value"]
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", action="append", choices=["cn", "en"])
    ap.add_argument("--section")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = ds_ingest.load_config()
    db = ds_db.DB(BASE / "data/docstor.db")
    session = ds_ingest.build_session(cfg)
    api = ds_api.DocCenter()

    langs = args.lang or ["cn", "en"]
    total = {"ok": 0, "skip": 0, "fail": 0}
    for lang in langs:
        todo = [t for t in targets(db.conn, lang) if not args.section or t[0] == args.section]
        if args.limit:
            todo = todo[: args.limit]
        print(f"  == {lang}: 待补 {len(todo)} 篇", flush=True)
        for section, slug in todo:
            cat, v = fetch(api, lang, section, slug)
            if not v:
                print(f"   ✗ {lang}/{section}/{slug} 接口取不到", flush=True)
                total["fail"] += 1
                continue
            if args.dry_run:
                print(f"   · {lang}/{section}/{slug} <- {cat} 「{v.get('title')}」", flush=True)
                total["ok"] += 1
                continue
            doc_dir = BASE / "site/docs" / lang / section
            img_dir = doc_dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            html = (v.get("content") or {}).get("content") or ""
            conv = ds_convert.convert(
                html, img_dir, session,
                gif_to_video=cfg["media"]["gif_to_video"], crf=cfg["media"]["video_crf"],
                preset=cfg["media"]["video_preset"], gif_min_bytes=cfg["media"]["gif_min_bytes"])
            md = ds_convert.normalize_headings(conv["markdown"], v.get("anchorList") or [])
            path = doc_dir / f"{slug}.md"
            path.write_text(md, encoding="utf-8")
            doc_key = f"{lang}/{section}/{slug}"
            db.upsert_doc({
                "doc_key": doc_key, "lang": lang, "section": section, "slug": slug,
                "title": v.get("title"), "doc_id": v.get("docId"), "version": v.get("version"),
                "is_api": 0, "alias": cat, "md_path": os.path.relpath(path, BASE),
                "content_hash": hashlib.md5(md.encode("utf-8")).hexdigest(),
                "anchors_json": json.dumps(ds_convert.anchors_for_docsify(v.get("anchorList") or []),
                                           ensure_ascii=False),
                "images": conv["images"], "videos": conv["videos"], "bytes": conv["bytes"],
                "fetched_at": None, "status": "ok"})
            db.conn.execute(
                "UPDATE docs SET status='ok', fetched_at=datetime('now') WHERE doc_key=?", (doc_key,))
            db.set_updated(doc_key, v.get("displayUpdateTime"))
            db.set_url(doc_key, ds_url.url_for(lang, cat, slug))
            db.conn.commit()
            total["ok"] += 1
            print(f"   ✓ {lang}/{section}/{slug} <- {cat} 图 {conv['images']} {len(md)}字", flush=True)

        if not args.dry_run:
            for section in {s for s, _ in todo}:
                db.export_meta(lang, section, BASE / "site/docs" / lang / section)
    print(f"  ✅ 完成：新增 {total['ok']}，失败 {total['fail']}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
