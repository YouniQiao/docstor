#!/usr/bin/python3
"""按渲染复核结果删除「其实能打开」的 link.dead 行（--apply 落库）。"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "docstor.db"
RES = ROOT / "data" / "dead_render_check2.json"


def main(apply: bool) -> int:
    rows = json.load(open(RES))
    drop = [i for r in rows if r["verdict"] == "ok" for i, _ in r["issues"]]
    print(f"  待删除 {len(drop)} 条（{sum(1 for r in rows if r['verdict'] == 'ok')} 个目标）", flush=True)
    if not apply:
        return 0
    c = sqlite3.connect(DB)
    c.executemany("DELETE FROM issues WHERE id=?", [(i,) for i in drop])
    c.commit()
    left = c.execute("select count(*) from issues where rule='link.dead' and status='open'").fetchone()[0]
    print(f"  已删除，link.dead 剩余 {left} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
