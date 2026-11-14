#!/usr/bin/python3
"""就地修复已生成的 md：去掉标题里的 [hN] 标记，并按锚点表重排标题层级。

用于修 markitdown 把 <h2> 原样带成「[h2]标题」的历史产物（约 3.3 万行），
不需要重新下载文档。

用法：
  python3 scripts/fix_headings.py            # 试跑，只统计
  python3 scripts/fix_headings.py --apply    # 真改文件
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from ds_convert import normalize_headings  # noqa: E402

TAG_RE = re.compile(r"^(#{1,6}\s+)\[h([1-6])\]\s*", re.M)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(BASE / "data/docstor.db")
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT doc_key, md_path, anchors_json FROM docs "
                      "WHERE status='ok' AND md_path IS NOT NULL").fetchall()

    touched = tagged = 0
    for r in rows:
        p = BASE / r["md_path"]
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        new = TAG_RE.sub(r"\1", text)
        n_tags = len(TAG_RE.findall(text))
        if n_tags:
            tagged += n_tags
        try:
            anchors = json.loads(r["anchors_json"] or "[]")
        except Exception:  # noqa: BLE001
            anchors = []
        new = normalize_headings(new, anchors)
        if new != text:
            touched += 1
            if args.apply:
                p.write_text(new, encoding="utf-8")

    print(f"  文档 {len(rows)} 篇 | 含 [hN] 标记 {tagged} 处 | 需改写 {touched} 篇"
          f"{'（已写入）' if args.apply else '（试跑，未写入）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
