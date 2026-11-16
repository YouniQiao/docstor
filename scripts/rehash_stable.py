#!/usr/bin/env python3
"""把 docs.content_hash 全量重算为「剔除时效签名后的稳定哈希」。

背景：早期哈希直接对 md 做 md5，而 md 里残留华为 CDN 的图片/视频/下载链接签名
（`?HW-CC-Date=…&HW-CC-Sign=…`、`:时间戳:2800:HEX…`），这些每天都会变，
导致没改过的文档每次抓取都被判「已修改」（实测 ux-design 单板块 24 篇假变更）。

用法：python3 scripts/rehash_stable.py [--apply]
不带 --apply 只统计会变的篇数。
"""

from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_convert  # noqa: E402
import ds_db  # noqa: E402


def main() -> int:
    apply_ = "--apply" in sys.argv
    db = ds_db.DB(str(BASE / "data" / "docstor.db"))
    rows = db.conn.execute(
        "SELECT doc_key, md_path, content_hash FROM docs WHERE status='ok' AND md_path IS NOT NULL"
    ).fetchall()
    changed = missing = 0
    for r in rows:
        p = BASE / r["md_path"]
        if not p.is_file():
            missing += 1
            continue
        h = ds_convert.stable_hash(p.read_text(encoding="utf-8"))
        if h != r["content_hash"]:
            changed += 1
            if apply_:
                db.conn.execute("UPDATE docs SET content_hash=? WHERE doc_key=?", (h, r["doc_key"]))
    if apply_:
        db.conn.commit()
    print(f"  文档 {len(rows)} 篇：哈希需更新 {changed}，md 缺失 {missing}"
          + ("" if apply_ else "（未写入，加 --apply 生效）"))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
