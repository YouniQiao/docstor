#!/usr/bin/python3
"""把误判为撤稿的文档从 data/trash 恢复回本地镜像。

适用场景：文档其实还在官网（页面能打开），只是不在目录树里，被
「目录树里没有 → 判定撤稿」的规则误删。恢复后会：
- md 与图片移回 site/docs/<lang>/<section>/
- docs 行恢复 status='ok'，md_path / content_hash 重算
- sync_items 里该文档的 kind='deleted' 记录删掉，对应 run 的 deleted 计数归零

用法：python3 scripts/restore_gone.py [--apply] [--doc cn/capabilities/xxx ...]
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_convert  # noqa: E402
import ds_db  # noqa: E402

IMG_REF = re.compile(r"images/([A-Za-z0-9._%\-]+)")


def main() -> int:
    apply_ = "--apply" in sys.argv
    only = [a for a in sys.argv[1:] if "/" in a and not a.startswith("-")]

    db = ds_db.DB(str(BASE / "data" / "docstor.db"))
    db.conn.row_factory = sqlite3.Row
    q = "SELECT doc_key, lang, section, slug, title FROM docs WHERE status='gone'"
    args: list = []
    if only:
        q += " AND doc_key IN (" + ",".join("?" * len(only)) + ")"
        args = only
    rows = db.conn.execute(q, args).fetchall()

    restored, missing = [], []
    for r in rows:
        lang, section, slug = r["lang"], r["section"], r["slug"]
        src = sorted(BASE.glob(f"data/trash/*/{lang}/{section}/{slug}.md"))
        if not src:
            missing.append(r["doc_key"])
            continue
        src_md = src[-1]
        dest_dir = BASE / "site" / "docs" / lang / section
        dest_md = dest_dir / f"{slug}.md"
        md_rel = f"site/docs/{lang}/{section}/{slug}.md"
        restored.append((r, src_md, dest_md, md_rel, dest_dir))

    print(f"  待恢复 {len(restored)} 篇，回收站里找不到 {len(missing)} 篇")
    for dk in missing:
        print("    缺:", dk)
    if not apply_:
        for r, _, _, _, _ in restored:
            print("    ·", r["doc_key"])
        print("  （未写入，加 --apply 生效）")
        db.close()
        return 0

    affected_runs: set[int] = set()
    for r, src_md, dest_md, md_rel, dest_dir in restored:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_md), str(dest_md))
        # 图片：回收站里同板块的 images/ 全部搬回去（同名覆盖）
        for img_src in src_md.parent.glob("images/*"):
            (dest_dir / "images").mkdir(parents=True, exist_ok=True)
            shutil.move(str(img_src), str(dest_dir / "images" / img_src.name))
        text = dest_md.read_text(encoding="utf-8")
        refs = set(IMG_REF.findall(text))
        n_img = sum(1 for n in refs if (dest_dir / "images" / n).is_file())
        db.conn.execute(
            "UPDATE docs SET status='ok', md_path=?, content_hash=?, images=?, bytes=?"
            " WHERE doc_key=?",
            (md_rel, ds_convert.stable_hash(text), n_img, dest_md.stat().st_size,
             r["doc_key"]))
        for it in db.conn.execute("SELECT id, run_id FROM sync_items"
                                  " WHERE kind='deleted' AND doc_key=?", (r["doc_key"],)):
            affected_runs.add(it["run_id"])
        db.conn.execute("DELETE FROM sync_items WHERE kind='deleted' AND doc_key=?",
                        (r["doc_key"],))
    # 这些 run 的 deleted 计数归零（都是这 10 篇造成的）
    for rid in affected_runs:
        row = db.conn.execute("SELECT stats FROM runs WHERE id=?", (rid,)).fetchone()
        if not row:
            continue
        try:
            st = json.loads(row["stats"] or "{}")
        except Exception:  # noqa: BLE001
            st = {}
        st["deleted"] = 0
        db.conn.execute("UPDATE runs SET stats=? WHERE id=?",
                        (json.dumps(st, ensure_ascii=False), rid))
    db.conn.commit()
    db.close()
    print(f"  已恢复 {len(restored)} 篇；清理 deleted 明细，涉及 run {sorted(affected_runs)}")
    # 清理空的回收站目录
    for p in sorted(BASE.glob("data/trash/*")):
        shutil.rmtree(p, ignore_errors=True) if not any(p.rglob("*")) else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
