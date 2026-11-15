#!/usr/bin/python3
"""外链（第三方 http/https）可达性探测 + 缓存。

官网侧栏/正文里有几千个外链（github、gitee、厂商文档…），HTTP 能查。
判据：
  · 404 / 410 → 真失效
  · 403 / 429 / 5xx / 超时 / DNS 失败 → 不可达（多半是反爬或临时故障，不算死链）
先 HEAD，HEAD 被拒（403/405/501）再 GET 读一小段。
结论存 link_http 表，缓存 7 天。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DB = BASE / "data/docstor.db"
TTL = 7 * 24 * 3600
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SCHEMA = """CREATE TABLE IF NOT EXISTS link_http(
    url TEXT PRIMARY KEY, status INTEGER, final TEXT, err TEXT, checked_at TEXT)"""

_local = threading.local()


def _db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB), timeout=30)
        conn.execute(SCHEMA)
        conn.commit()
        _local.conn = conn
    return conn


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        _local.s = s
    return s


def _probe(url: str) -> tuple[int, str, str]:
    s = _session()
    for method in ("head", "get"):
        try:
            kw = {"timeout": 12, "allow_redirects": True}
            if method == "get":
                kw["stream"] = True
            r = getattr(s, method)(url, **kw)
            if method == "get":
                r.close()
            if r.status_code in (403, 405, 501) and method == "head":
                continue
            return r.status_code, r.url or url, ""
        except requests.exceptions.SSLError:
            return 0, url, "ssl"
        except requests.exceptions.Timeout:
            return 0, url, "timeout"
        except Exception as e:  # noqa: BLE001
            err = type(e).__name__
            if method == "head":
                continue
            return 0, url, err
    return 0, url, "blocked"


def status(url: str, refresh: bool = False) -> tuple[int, str, str]:
    db = _db()
    if not refresh:
        row = db.execute("SELECT status, final, err, checked_at FROM link_http WHERE url=?",
                         (url,)).fetchone()
        if row:
            try:
                if time.time() - time.mktime(time.strptime(row[3], "%Y-%m-%d %H:%M:%S")) < TTL:
                    return row[0], row[1], row[2]
            except Exception:  # noqa: BLE001
                pass
    st, final, err = _probe(url)
    db.execute("INSERT OR REPLACE INTO link_http(url,status,final,err,checked_at)"
               " VALUES(?,?,?,?,?)",
               (url, st, final, err, time.strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    return st, final, err


def verdict(st: int, err: str) -> str:
    if st in (404, 410):
        return "dead"
    if st == 0 or st in (401, 403, 429) or st >= 500:
        return "unreachable"
    return "ok"


if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        st, final, err = status(u, refresh=True)
        print(f"  {verdict(st, err):11s} {st:3d} {u} -> {final if final != u else ''}")
