#!/usr/bin/python3
"""把库里已标记 status='gone' 的文档从本地镜像清掉（历史遗留清理）。

作用等同 ds_ingest 里的撤稿清理：md 与它独有的图片/视频移进 data/trash/，
docs 行保留（status='gone'）、路径与哈希置空，_anchors.json 里对应条目删掉。

用法：python3 scripts/prune_gone.py [--apply]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ds_db  # noqa: E402
import ds_ingest  # noqa: E402


def main() -> int:
    apply_ = "--apply" in sys.argv
    db = ds_db.DB(str(BASE / "data" / "docstor.db"))
    rows = db.conn.execute(
        "SELECT doc_key, lang, section, slug, title, md_path FROM docs"
        " WHERE status='gone' ORDER BY lang, section").fetchall()
    if not rows:
        print("  没有 status='gone' 的文档")
        return 0

    by_sec: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_sec.setdefault((r["lang"], r["section"]), []).append(dict(r))

    total = 0
    for (lang, section), items in by_sec.items():
        doc_dir = BASE / "site" / "docs" / lang / section
        anchors_path = doc_dir / "_anchors.json"
        anchors_map = {}
        if anchors_path.exists():
            try:
                anchors_map = json.loads(anchors_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                anchors_map = {}
        files_exist = sum(1 for i in items
                          if i["md_path"] and (BASE / i["md_path"]).is_file())
        print(f"  {lang}/{section}: gone {len(items)} 篇，其中 md 还在盘上 {files_exist} 个")
        if not apply_:
            continue
        moved = ds_ingest.prune_gone_files(db, doc_dir, lang, section, items, anchors_map)
        if anchors_path.exists():
            anchors_path.write_text(json.dumps(anchors_map, ensure_ascii=False, indent=0),
                                    encoding="utf-8")
        total += moved
    if apply_:
        print(f"  已移除 {total} 个 md 文件到 data/trash/")
    else:
        print("  （未写入，加 --apply 生效）")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
