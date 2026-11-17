#!/usr/bin/python3
"""docstor 抓取：目录树 → 正文 → Markdown + 图片/动图本地化 → docsify 站点。

用法：
  python3 ds_ingest.py --section capabilities --lang cn            # 整板块
  python3 ds_ingest.py --section capabilities --lang cn --limit 5  # 只跑 5 篇（试跑）
  python3 ds_ingest.py --section capabilities --lang cn --full     # 忽略内容哈希强制重抓
  python3 ds_ingest.py --section capabilities --lang cn --dry-run  # 只看目录，不下载
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import requests
import yaml

import ds_api
import ds_catalog
import ds_convert
import ds_db

BASE = Path(__file__).resolve().parent

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def load_config() -> dict:
    return yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))


def build_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": cfg["api"]["user_agent"],
                      "Referer": cfg["api"]["referer"]})
    return s


def flatten_nodes(root: ds_api.TreeNode) -> list[dict]:
    out: list[dict] = []

    def walk(n: ds_api.TreeNode):
        for c in n.children:
            out.append({"node_id": c.node_id, "parent": c.parent, "name": c.name,
                        "slug": c.slug, "is_leaf": c.is_leaf, "is_api": c.is_api,
                        "hided": c.hided, "depth": c.depth, "sort": c.sort,
                        "alias": c.alias})
            walk(c)

    walk(root)
    return out


def sidebar_md(nodes: list[dict], link_prefix: str) -> str:
    """按节点顺序生成 docsify 侧边栏（层级与官网一致）。"""
    lines: list[str] = []
    stack: list[int] = []
    for n in nodes:
        if n["hided"]:
            continue
        while stack and stack[-1] >= n["depth"]:
            stack.pop()
        indent = "  " * len(stack)
        if n["slug"]:
            href = f'{link_prefix}/{n["slug"]}.md' if link_prefix else f'{n["slug"]}.md'
            lines.append(f'{indent}- [{n["name"]}]({href})')
        else:
            lines.append(f'{indent}- {n["name"]}')
        stack.append(n["depth"])
    return "\n".join(lines) + "\n"


_IMG_REF_RE = re.compile(r"images/([A-Za-z0-9._%\-]+)")


def prune_gone_files(db, doc_dir: Path, lang: str, section: str,
                     gone: list, anchors_map: dict) -> int:
    """官网已撤稿的文档：从本地镜像真正删掉（移进 data/trash，可回收）。

    · md 文件、以及只被它引用的图片/视频一起移走（共享资源保留）
    · docs 行保留（status='gone'），只清路径与哈希 —— 官网若又挂回来，
      下一轮抓取会重新写盘并恢复 status='ok'
    · 侧边栏每次抓取都按官网目录树重建，本来就不含这些文档
    """
    stage = BASE / "data" / "trash" / time.strftime("%Y%m%d_%H%M") / lang / section
    gone_slugs = {g["slug"] for g in gone if g["slug"]}

    alive_refs: set[str] = set()
    for p in doc_dir.glob("*.md"):
        if p.stem in gone_slugs:
            continue
        try:
            alive_refs.update(_IMG_REF_RE.findall(p.read_text(encoding="utf-8")))
        except OSError:
            continue

    moved = 0
    for g in gone:
        md = BASE / (g["md_path"] or "")
        own_refs: set[str] = set()
        if g["md_path"] and md.is_file():
            try:
                own_refs = set(_IMG_REF_RE.findall(md.read_text(encoding="utf-8")))
            except OSError:
                pass
            stage.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(md), str(stage / md.name))
                moved += 1
            except OSError as e:
                log(f"   ⚠ 删除失败 {md.name}: {e}")
        for name in sorted(own_refs - alive_refs):
            f = doc_dir / "images" / name
            if f.is_file():
                (stage / "images").mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(f), str(stage / "images" / name))
                except OSError:
                    pass
        anchors_map.pop(g["slug"], None)
        db.conn.execute(
            "UPDATE docs SET md_path=NULL, content_hash=NULL, anchors_json=NULL,"
            " images=0, videos=0, bytes=0 WHERE doc_key=?", (g["doc_key"],))
    db.conn.commit()
    return moved


def ingest(args) -> int:
    cfg = load_config()
    lang, section = args.lang, args.section
    out_root = BASE / cfg["site"]["output_dir"]
    docs_root = BASE / cfg["site"]["docs_dir"]
    doc_dir = docs_root / lang / section
    img_dir = doc_dir / "images"
    doc_dir.mkdir(parents=True, exist_ok=True)

    api = ds_api.DocCenter(base=cfg["api"]["base"],
                           center_prefix=cfg["api"]["center_prefix"],
                           referer=cfg["api"]["referer"],
                           timeout=cfg["api"]["timeout"],
                           retries=cfg["api"]["retries"],
                           ua=cfg["api"]["user_agent"])
    db = ds_db.DB(str(BASE / cfg["db"]["path"]))

    t0 = time.time()
    run_id = db.start_run("ingest", f"{lang}/{section}")
    log(f"📂 拉取目录树 {lang}/{section} …")
    # 英文站的板块别名与中文不完全相同（如「问题处理与运维」在 en 叫 dev-faq）
    EN_SECTION_ALIAS = {"tools-faq": "dev-faq"}
    api_section = EN_SECTION_ALIAS.get(section, section) if lang == "en" else section
    root = api.tree(api_section, lang)
    nodes = flatten_nodes(root)
    db.replace_section_nodes(lang, section, nodes)
    # 注意：有子节点的节点也可能是一篇文档（如「ExtensionAbility组件」概述页），
    # 官网左侧同时可点击又可展开，所以只要带 relateDocument 就算文档。
    leaves = [n for n in nodes if n["slug"]]
    seen, uniq = set(), []
    for n in leaves:
        if n["slug"] in seen:
            continue
        seen.add(n["slug"])
        uniq.append(n)
    hidden = sum(1 for n in nodes if n["hided"])
    log(f"   节点 {len(nodes)} | 文档 {len(uniq)} | 隐藏节点 {hidden}")

    # 侧边栏（先落盘，即使抓取中断站点也能看）
    link_prefix = f"/{lang}/{section}"
    (doc_dir / "_sidebar.md").write_text(sidebar_md(nodes, link_prefix), encoding="utf-8")
    log(f"   侧边栏 → {doc_dir/'_sidebar.md'}")

    if args.sidebar_only:
        log("   只重建侧边栏，未抓取文档")
        db.finish_run(run_id, {"sidebar_only": True})
        db.close()
        return 0

    if args.dry_run:
        log("   [dry-run] 只写了侧边栏")
        db.finish_run(run_id, {"dry_run": True, "docs": len(uniq)})
        db.close()
        return 0

    if args.limit and not getattr(args, "only_missing", False):
        uniq = uniq[: args.limit]
        log(f"   ⚠ 仅处理前 {len(uniq)} 篇（--limit）")

    # 增量：内容哈希未变则跳过
    todo = []
    for n in uniq:
        doc_key = f"{lang}/{section}/{n['slug']}"
        row = None if args.full else db.get_doc(doc_key)
        if getattr(args, "only_missing", False) and row is not None and row["status"] == "ok":
            continue  # 只补抓失败/缺失的
        todo.append((n, doc_key, row))
    if args.limit and getattr(args, "only_missing", False):
        todo = todo[: args.limit]
        log(f"   ⚠ 仅处理前 {len(todo)} 篇待抓（--limit）")
    log(f"   待抓取 {len(todo)} 篇（并发 {cfg['ingest']['workers']}，"
        f"内容未变的会跳过写盘）")

    session = build_session(cfg)
    delay = cfg["ingest"]["request_delay"]
    stats = {"ok": 0, "fail": 0, "skip": 0, "images": 0, "videos": 0, "bytes": 0,
             "added": 0, "modified": 0, "deleted": 0}
    changes: list[dict] = []  # 每日增量明细（写入 sync_items）
    failures: list[str] = []
    anchors_map: dict[str, list[dict]] = {}

    # 已有锚点先继承（增量时只更新变了的）
    old_anchors_path = doc_dir / "_anchors.json"
    if old_anchors_path.exists():
        try:
            anchors_map.update(json.loads(old_anchors_path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass

    def work(item):
        n, doc_key, row = item
        slug = n["slug"]
        try:
            res = api.document(slug, n.get("alias") or section, lang, is_api=n["is_api"])
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "slug": slug, "err": f"接口异常 {e}"}
        if not res.get("ok"):
            return {"ok": False, "slug": slug, "err": f"code={res.get('code')}"}
        html = res["html"]
        if not html.strip():
            return {"ok": False, "slug": slug, "err": "正文为空"}
        conv = ds_convert.convert(
            html, img_dir, session,
            gif_to_video=cfg["media"]["gif_to_video"],
            crf=cfg["media"]["video_crf"],
            preset=cfg["media"]["video_preset"],
            gif_min_bytes=cfg["media"]["gif_min_bytes"])
        md = ds_convert.normalize_headings(conv["markdown"], res.get("anchors") or [])
        path = doc_dir / f"{slug}.md"
        new_hash = ds_convert.stable_hash(md)
        if row is not None and row["content_hash"] == new_hash and path.exists():
            return {"ok": True, "unchanged": True, "slug": slug, "path": str(path),
                    "title": row["title"], "doc_id": row["doc_id"],
                    "version": row["version"], "alias": row["alias"],
                    "is_api": bool(row["is_api"]),
                    "anchors": json.loads(row["anchors_json"] or "[]"),
                    "hash": new_hash, "images": row["images"], "videos": row["videos"],
                    "bytes": row["bytes"], "failed_imgs": 0,
                    "updated": res.get("updated")}
        path.write_text(md, encoding="utf-8")
        return {"ok": True, "slug": slug, "path": str(path), "title": res.get("title"),
                "doc_id": res.get("doc_id"), "version": res.get("version"),
                "alias": res.get("alias"), "is_api": res.get("is_api"),
                "anchors": ds_convert.anchors_for_docsify(res.get("anchors")),
                "hash": hashlib.md5(md.encode("utf-8")).hexdigest(),
                "images": conv["images"], "videos": conv["videos"],
                "bytes": conv["bytes"], "failed_imgs": conv["failed"],
                "updated": res.get("updated")}

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg["ingest"]["workers"]) as ex:
        futs = {ex.submit(work, it): it for it in todo}
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            done += 1
            n, doc_key, _row = futs[fut]
            if r["ok"]:
                stats["skip" if r.get("unchanged") else "ok"] += 1
                if not r.get("unchanged"):
                    # 只有真正变更的文档才累计图片/流量（早期写法把存量全算进「新增数据」，单板块虚报 1GB）
                    kind = "added" if _row is None else "modified"
                    stats[kind] += 1
                    stats["images"] += r["images"]
                    stats["videos"] += r["videos"]
                    stats["bytes"] += r["bytes"]
                    changes.append({
                        "doc_key": doc_key, "kind": kind, "lang": lang, "section": section,
                        "slug": r["slug"], "title": r.get("title") or "",
                        "version": r.get("version"),
                        "old_version": (_row["version"] if _row is not None else None),
                    })
                    if len(changes) >= 100:
                        db.add_sync_items(run_id, changes)
                        changes.clear()
                anchors_map[r["slug"]] = r["anchors"]
                db.upsert_doc({
                    "doc_key": doc_key, "lang": lang, "section": section,
                    "slug": r["slug"], "title": r["title"], "doc_id": r["doc_id"],
                    "version": r["version"], "is_api": int(bool(r["is_api"])),
                    "alias": r["alias"], "md_path": os.path.relpath(r["path"], BASE),
                    "content_hash": r["hash"],
                    "anchors_json": json.dumps(r["anchors"], ensure_ascii=False),
                    "images": r["images"], "videos": r["videos"], "bytes": r["bytes"],
                    "fetched_at": ds_db.now(), "status": "ok",
                })
                db.set_updated(doc_key, r.get("updated"))
                db.set_url(doc_key, ds_catalog.url_for(lang, section, r["slug"], int(bool(r["is_api"]))))
                if r["failed_imgs"]:
                    log(f"   ⚠ {r['slug']}: {r['failed_imgs']} 张图下载失败")
            else:
                stats["fail"] += 1
                failures.append(f"{r['slug']}: {r['err']}")
                db.upsert_doc({
                    "doc_key": doc_key, "lang": lang, "section": section,
                    "slug": r["slug"], "title": None, "doc_id": None, "version": None,
                    "is_api": int(bool(n["is_api"])), "alias": None, "md_path": None,
                    "content_hash": None, "anchors_json": None, "images": 0, "videos": 0,
                    "bytes": 0, "fetched_at": ds_db.now(), "status": "fail",
                })
            if done % 25 == 0 or done == len(todo):
                el = time.time() - t0
                rate = done / el if el else 0
                eta = (len(todo) - done) / rate / 60 if rate else 0
                log(f"   进度 {done}/{len(todo)}  ok={stats['ok']} fail={stats['fail']} "
                    f"图={stats['images']} 视频={stats['videos']} "
                    f"{stats['bytes']/1048576:.0f}MB  {rate:.1f}篇/s  ETA {eta:.0f}min")
            if delay:
                time.sleep(delay / max(1, cfg["ingest"]["workers"]))

    db.add_sync_items(run_id, changes)
    changes.clear()

    # 官网已撤稿的文档：本次目录树里没有的，保守判定（超过阈值就整批跳过，
    # 防止目录接口返回不完整时一次标掉半个板块）
    if not args.limit and not getattr(args, "only_missing", False) and uniq:
        cur_slugs = {n["slug"] for n in uniq}
        rows = db.conn.execute(
            "SELECT doc_key, slug, title, md_path FROM docs"
            " WHERE lang=? AND section=? AND status='ok'",
            (lang, section)).fetchall()
        gone = [r for r in rows if r["slug"] not in cur_slugs]
        limit_ = max(50, int(len(rows) * 0.05))
        if len(gone) > limit_:
            log(f"   ⚠ 本次判定删除 {len(gone)} 篇（>{limit_}），疑似目录接口不完整，跳过")
        elif gone:
            db.mark_docs_gone([g["doc_key"] for g in gone])
            db.add_sync_items(run_id, [{
                "doc_key": g["doc_key"], "kind": "deleted", "lang": lang,
                "section": section, "slug": g["slug"], "title": g["title"] or "",
            } for g in gone])
            n_moved = prune_gone_files(db, doc_dir, lang, section, gone, anchors_map)
            stats["deleted"] = len(gone)
            log(f"   🗑  官网已撤稿 {len(gone)} 篇：已从镜像移除 {n_moved} 个 md"
                f"（回收站 data/trash/）")

    old_anchors_path.write_text(json.dumps(anchors_map, ensure_ascii=False, indent=0),
                                encoding="utf-8")
    stats["elapsed_s"] = round(time.time() - t0, 1)
    stats["failures"] = failures[:50]
    db.finish_run(run_id, stats)
    rec = db.export_meta(lang, section, doc_dir)   # 供前端「更新时间」使用
    db.close()

    log(f"\n📈 本次变化: 新增 {stats['added']} 修改 {stats['modified']} 删除 {stats['deleted']}")
    log(f"\n✅ 抓取完成 {lang}/{section}: ok={stats['ok']} fail={stats['fail']} "
        f"图={stats['images']} 视频={stats['videos']} "
        f"新增数据={stats['bytes']/1048576:.1f}MB 用时={stats['elapsed_s']}s")
    if failures:
        log(f"   失败样例: {failures[:5]}")
    # 抓完把官网目录索引与「文档→目录」映射跟上，否则新文档会被误判死链/已下架
    if not getattr(args, "skip_index", False):
        try:
            ds_catalog.refresh(log=log)
        except Exception as e:  # noqa: BLE001
            log(f"   ⚠ 目录索引刷新失败（不影响本次抓取）：{e}")
    return 0 if stats["fail"] == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="docstor 抓取")
    ap.add_argument("--section", required=True, help="板块 alias，如 capabilities")
    ap.add_argument("--lang", default="cn", choices=["cn", "en"])
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 篇")
    ap.add_argument("--full", action="store_true", help="忽略内容哈希，强制重抓")
    ap.add_argument("--dry-run", action="store_true", help="只拉目录树 + 写侧边栏")
    ap.add_argument("--sidebar-only", action="store_true", help="按库中节点重建侧边栏")
    ap.add_argument("--only-missing", action="store_true", help="只补抓失败/缺失的文档")
    ap.add_argument("--skip-index", action="store_true", help="抓完不重建官网目录索引")
    return ingest(ap.parse_args())


if __name__ == "__main__":
    sys.exit(main())
