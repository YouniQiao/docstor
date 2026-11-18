#!/usr/bin/env python3
"""清理回收站与整库备份里超过 N 天的旧副本（默认 30 天，默认干跑）。

目的是防止 data/trash（撤稿/陈旧文档的 md + 图片）和 data/backup（整库快照）无限增长，
同时保留足够长的回滚窗口。

  · data/trash/<YYYYMMDD_HHMM>/   —— 按目录名（即清理发生的时间）判龄
  · data/backup/*.db              —— 按文件修改时间判龄

用法：
  python3 scripts/clean_trash.py              # 干跑，只打印
  python3 scripts/clean_trash.py --apply      # 真删
  python3 scripts/clean_trash.py --days 60 --apply
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
TRASH = BASE / "data" / "trash"
BACKUP = BASE / "data" / "backup"


def dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cutoff = datetime.now() - timedelta(days=args.days)

    victims: list[Path] = []
    if TRASH.is_dir():
        for d in sorted(TRASH.iterdir()):
            if not d.is_dir():
                continue
            try:
                stamp = datetime.strptime(d.name, "%Y%m%d_%H%M")
            except ValueError:
                stamp = datetime.fromtimestamp(d.stat().st_mtime)
            if stamp < cutoff:
                victims.append(d)
    if BACKUP.is_dir():
        for f in sorted(BACKUP.iterdir()):
            if f.is_file() and f.suffix == ".db" and datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                victims.append(f)

    freed = sum(dir_size(v) if v.is_dir() else v.stat().st_size for v in victims)
    print(f"  保留 {args.days} 天，待清理 {len(victims)} 项，可释放 {human(freed)}")
    for v in victims:
        sz = dir_size(v) if v.is_dir() else v.stat().st_size
        print(f"    - {v.relative_to(BASE)}  {human(sz)}")
        if args.apply:
            if v.is_dir():
                shutil.rmtree(v, ignore_errors=True)
            else:
                v.unlink(missing_ok=True)
    if not args.apply:
        print("  这是干跑，什么都没删；确认无误后加 --apply")
    else:
        print(f"  已清理 {len(victims)} 项，释放 {human(freed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
