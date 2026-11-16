#!/usr/bin/python3
"""清掉「其实能打开」的 link.dead 误报。

口径（用户定）：链接最终能访问到的，就不算问题，直接删数据，不留在全量问题里。
这里把 link.dead 逐条按官网判定复核，verdict 是 ok / renamed 的行删掉。
（cn_only 不删——那是「英文链到中文页」，由 link.no_translation 单独管。）

用法：python3 scripts/purge_false_dead.py [--dry-run]
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ds_official  # noqa: E402

DB = Path(__file__).resolve().parent.parent / "data" / "docstor.db"
TARGET_RE = re.compile(r"^/(cn|en)/([A-Za-z0-9_\-]+)/([A-Za-z0-9_.\-]+)")


def main() -> int:
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, doc_key, locator FROM issues"
                        " WHERE status='open' AND rule='link.dead'").fetchall()
    off = ds_official.Official()
    cache: dict[tuple, str] = {}
    dropped = 0
    for r in rows:
        try:
            loc = json.loads(r["locator"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        m = TARGET_RE.match(loc.get("target") or "")
        if not m:
            continue
        key = m.groups()
        if key not in cache:
            cache[key] = off.verdict(*key)
        if cache[key] not in ("ok", "renamed"):
            continue
        if not dry:
            conn.execute("DELETE FROM issues WHERE id=?", (r["id"],))
        dropped += 1
        print(f"  {'[dry] ' if dry else ''}删除 {r['doc_key']} #{r['id']}（{cache[key]}）")
    if not dry:
        conn.commit()
    print(f"  删除 {dropped} 条误报，保留 link.dead {len(rows) - dropped} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
