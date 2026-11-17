#!/usr/bin/python3
"""用无头 Chromium 真渲染一页，作为「官网这篇到底打不打得开」的最终判据。

官网是 SPA，HTTP 永远 200；目录索引只能说明「目录里登记过这篇」，
页面被下线后目录条目可能还在。真渲染看标题和正文长度最可靠：
  · 正常文档：<title>「文档名 - 华为HarmonyOS开发者」</title>，DOM 几百 KB 以上
  · 不存在：<title>文档中心</title>，正文区为空，DOM 明显更小
结论写入 link_render 表缓存 30 天。

用法：
  python3 ds_render.py cn/harmonyos-guides/some-slug
  python3 -m scripts.verify_dead --workers 4     # 批量复核当前 link.dead
"""
from __future__ import annotations

import glob
import html
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "data/docstor.db"
URL = "https://developer.huawei.com/consumer/{lang}/doc/{cat}/{slug}"
GENERIC = {"文档中心", "Document Center", "Documentation Center", "华为HarmonyOS开发者"}
TTL = 30 * 24 * 3600

SCHEMA = """CREATE TABLE IF NOT EXISTS link_render(
    lang TEXT, cat TEXT, slug TEXT, verdict TEXT, title TEXT, bytes INTEGER, checked_at TEXT,
    PRIMARY KEY(lang, cat, slug))"""

_CHROME: str | None = None
_lock = threading.Lock()


def chrome() -> str | None:
    global _CHROME
    with _lock:
        if _CHROME is None:
            cands = sorted(glob.glob(os.path.expanduser(
                "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")))
            _CHROME = cands[-1] if cands else ""
    return _CHROME or None


def _run(url: str, budget: int = 25000, timeout: int = 150) -> tuple[str, str, int]:
    exe = chrome()
    if not exe:
        return "unknown", "", 0
    cmd = [exe, "--headless=new", "--no-sandbox", "--disable-gpu",
           "--disable-dev-shm-usage", f"--virtual-time-budget={budget}",
           "--dump-dom", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout).stdout.decode("utf-8", "ignore")
    except subprocess.TimeoutExpired:
        return "timeout", "", 0
    if not out:
        return "timeout", "", 0
    m = re.search(r"<title>(.*?)</title>", out, re.S)
    title = html.unescape(m.group(1)).strip() if m else ""
    txt = html.unescape(re.sub(r"<[^>]+>", " ", out))
    missing = ("文档不存在" in txt or "页面不存在" in txt or "内容不存在" in txt)
    # 页面存在但需授权（仓颉等受限文档）：正文只有一句「需使用经授权的账号查阅」，
    # 没有 h1、正文长度也不够，按长度判会被误判成 missing —— 链接其实是好的。
    gated = "需使用经授权的账号查阅" in txt
    core = title.split(" - ")[0].strip().lstrip("🐴 ").strip()
    # 正文标题：AGC 这类老文档 <title> 停在「文档中心」，只有 h1 是文档名
    h = re.search(r"<h1[^>]*>(.*?)</h1>", out, re.S)
    h1 = html.unescape(re.sub(r"<[^>]+>", " ", h.group(1))).strip() if h else ""
    blank_h1 = not h1 or re.fullmatch(r"404|页?面?不存在|文档不存在|Not Found", h1)
    # 论坛/控制台这类站点没有 h1，但标题正常、正文过万 → 也算能打开
    bad_title = (not core) or core in GENERIC or bool(re.search(r"404|Not Found|不存在", core))
    ok = gated or ((not missing) and ((not blank_h1) or ((not bad_title) and len(txt) > 1500)))
    return ("ok" if ok else "missing"), (title or h1), len(out)


def check(lang: str, cat: str, slug: str, refresh: bool = False,
          db_path: str | None = None) -> str:
    db = sqlite3.connect(db_path or str(DB), timeout=30)
    db.execute(SCHEMA)
    if not refresh:
        row = db.execute("SELECT verdict, checked_at FROM link_render"
                         " WHERE lang=? AND cat=? AND slug=?", (lang, cat, slug)).fetchone()
        if row:
            try:
                if time.time() - time.mktime(time.strptime(row[1], "%Y-%m-%d %H:%M:%S")) < TTL:
                    return row[0]
            except Exception:  # noqa: BLE001
                pass
    verdict, title, size = _run(URL.format(lang=lang, cat=cat, slug=slug))
    db.execute("INSERT OR REPLACE INTO link_render(lang,cat,slug,verdict,title,bytes,checked_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (lang, cat, slug, verdict, title, size, time.strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    db.close()
    return verdict


URL_SCHEMA = """CREATE TABLE IF NOT EXISTS url_render(
    url TEXT PRIMARY KEY, verdict TEXT, title TEXT, bytes INTEGER, checked_at TEXT)"""


def check_url(url: str, refresh: bool = False, db_path: str | None = None) -> str:
    """任意地址真渲染（外链用）：返回 ok / missing / timeout。

    有些站点（docs.openharmony.cn 这类）对任何路径都回 404，正文靠 JS 渲染，
    单看 HTTP 码会把能打开的页面判死。
    """
    db = sqlite3.connect(db_path or str(DB), timeout=30)
    db.execute(URL_SCHEMA)
    if not refresh:
        row = db.execute("SELECT verdict, checked_at FROM url_render WHERE url=?",
                         (url,)).fetchone()
        if row:
            try:
                if time.time() - time.mktime(time.strptime(row[1], "%Y-%m-%d %H:%M:%S")) < TTL:
                    db.close()
                    return row[0]
            except Exception:  # noqa: BLE001
                pass
    verdict, title, size = _run(url)
    db.execute("INSERT OR REPLACE INTO url_render(url,verdict,title,bytes,checked_at)"
               " VALUES(?,?,?,?,?)",
               (url, verdict, title, size, time.strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    db.close()
    return verdict


if __name__ == "__main__":
    import sys
    for arg in sys.argv[1:]:
        lang, cat, slug = arg.split("/", 2)
        print(f"  {arg} -> {check(lang, cat, slug)}")
