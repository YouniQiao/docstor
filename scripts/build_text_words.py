#!/usr/bin/python3
"""用文档语料统计二字词，生成 data/text_words_cn.txt（重复字检查判词界用）。

为什么用文档自己的词表：鸿蒙文档里的「时间间隔」「参数数据」「动作动画」都是两个正常词
挨在一起、中间那个字重复。通用词典判不了这些专业词，用本语料的高频二字串最准。
词表变了才需要重跑（语料大改后跑一次即可）。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ROOT = BASE / "site" / "docs" / "cn"
OUT = BASE / "data" / "text_words_cn.txt"
MIN_COUNT = 20
HAN = __import__("re").compile(r"^[\u4e00-\u9fff]{2}$")


def main() -> int:
    cnt: Counter[str] = Counter()
    scope = sys.argv[1:] or [str(ROOT)]
    for arg in scope:
        for p in Path(arg).rglob("*.md"):
            t = p.read_text(encoding="utf-8", errors="replace")
            prev = ""
            for ch in t:
                if prev:
                    cnt[prev + ch] += 1
                prev = ch
    words = sorted(w for w, n in cnt.items() if n >= MIN_COUNT and HAN.match(w) and w[0] != w[1])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(words) + "\n", encoding="utf-8")
    print(f"词表 {len(words):,} 个 → {OUT}（{OUT.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
