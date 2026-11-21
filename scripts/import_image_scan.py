#!/usr/bin/python3
"""把全量 OCR 结果（JSONL）导入 image_scan 表（按图片内容哈希唯一，可重复导入）。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent

DDL = """
CREATE TABLE IF NOT EXISTS image_scan (
  img_hash   TEXT PRIMARY KEY,
  path       TEXT NOT NULL,
  w          INTEGER,
  h          INTEGER,
  bytes      INTEGER,
  han        INTEGER,
  chars      INTEGER,
  ratio      REAL,
  text       TEXT,
  scores     TEXT,
  ms         INTEGER,
  engine     TEXT,
  scanned_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_image_scan_path ON image_scan(path);

-- 同一内容可能有多个路径（不同板块各存一份），路径 → 哈希 单独一张表
CREATE TABLE IF NOT EXISTS image_path (
  path     TEXT PRIMARY KEY,
  img_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_image_path_hash ON image_path(img_hash);
"""


def main() -> None:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/full_ocr.jsonl")
    db = sqlite3.connect(BASE / "data/docstor.db", timeout=60)
    db.executescript(DDL)
    rows, seen, now = [], set(), datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    paths: list[tuple[str, str]] = []
    for ln in src.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("err"):
            continue
        h = r.get("img_hash")
        if not h:
            continue
        paths.append((r["path"], h))     # 每个路径都要记住（同图可能散在多个板块）
        if h in seen:
            continue
        seen.add(h)
        chars = max(1, r.get("chars") or 1)
        rows.append((h, r["path"], r.get("w"), r.get("h"), r.get("bytes"), r.get("han") or 0,
                     r.get("chars") or 0, (r.get("han") or 0) / chars,
                     (r.get("text") or "")[:2000],
                     json.dumps(r.get("scores") or [])[:2000], r.get("ms"), "paddleocr-mobile",
                     now))
    with db:
        db.executemany(
            "INSERT OR REPLACE INTO image_scan(img_hash,path,w,h,bytes,han,chars,ratio,text,"
            "scores,ms,engine,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.executemany("INSERT OR REPLACE INTO image_path(path,img_hash) VALUES(?,?)", paths)
    n, cand = db.execute("SELECT COUNT(*), SUM(han>=3 AND ratio>=0.02) FROM image_scan").fetchone()
    np_ = db.execute("SELECT COUNT(*) FROM image_path").fetchone()[0]
    print(f"导入 {len(rows):,} 张去重图 / {len(paths):,} 个路径；"
          f"image_scan {n:,} 行，候选 {cand or 0:,} 张")


if __name__ == "__main__":
    main()
