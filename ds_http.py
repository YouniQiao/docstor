#!/usr/bin/python3
"""外链（第三方 http/https）可达性探测 + 缓存。

官网侧栏/正文里有几千个外链（github、gitee、厂商文档…），HTTP 能查。
判据：
  · 404 / 410 → 真失效
  · 403 / 429 / 5xx / 超时 / DNS 失败 → 不可达（多半是反爬或临时故障，不算死链）
先 HEAD，HEAD 被拒（403/405/501）再 GET 读一小段。
结论存 link_http 表，保质期跟着结论走：2xx 30 天、404/410 3 天、其它 7 天。
"""
from __future__ import annotations

import sqlite3
import threading
import urllib.parse
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
DB = BASE / "data/docstor.db"
TTL = 7 * 24 * 3600             # 说不清的（403/429/5xx/超时/反爬）：7 天再试
TTL_OK = 30 * 24 * 3600         # 200 一类：外链多半长期有效，30 天
TTL_DEAD = 3 * 24 * 3600        # 404/410：真失效，官网/仓库随时可能补上，3 天重探


def _ttl_for(status: int) -> int:
    if 200 <= (status or 0) < 300:
        return TTL_OK
    if status in (404, 410):
        return TTL_DEAD
    return TTL
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


BLOCK_CODES = {400, 401, 403, 405, 406, 407, 429, 451, 567}   # 反爬 / WAF / 需登录 / 不支持 HEAD


def _probe(url: str) -> tuple[int, str, str]:
    s = _session()
    u = urllib.parse.quote(url, safe=":/?#[]@!$&'()*,;=%~")     # 中文/空格链接别直接发
    for method in ("head", "get"):
        try:
            kw = {"timeout": 15, "allow_redirects": True}
            if method == "get":
                kw["stream"] = True
            r = getattr(s, method)(u, **kw)
            if method == "get":
                r.close()
            if method == "head" and (r.status_code in BLOCK_CODES or r.status_code >= 400):
                continue          # HEAD 被拦或不支持，换 GET 再确认
            return r.status_code, r.url or url, ""
        except requests.exceptions.SSLError:
            return 0, url, "ssl"
        except requests.exceptions.Timeout:
            return 0, url, "timeout"
        except Exception as e:  # noqa: BLE001
            if method == "head":
                continue
            return 0, url, f"{type(e).__name__}: {str(e)[:120]}"
    return 0, url, "blocked"


def status(url: str, refresh: bool = False) -> tuple[int, str, str]:
    db = _db()
    if not refresh:
        row = db.execute("SELECT status, final, err, checked_at FROM link_http WHERE url=?",
                         (url,)).fetchone()
        if row:
            try:
                if time.time() - time.mktime(time.strptime(row[3], "%Y-%m-%d %H:%M:%S")) < _ttl_for(row[0]):
                    return row[0], row[1], row[2]
            except Exception:  # noqa: BLE001
                pass
    st, final, err = _probe(url)
    db.execute("INSERT OR REPLACE INTO link_http(url,status,final,err,checked_at)"
               " VALUES(?,?,?,?,?)",
               (url, st, final, err, time.strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()
    return st, final, err


DNS_HINTS = ("nameresolution", "getaddrinfo", "name or service not known",
             "nodename nor servname", "no address associated", "temporary failure in name resolution")


def verdict(st: int, err: str) -> str:
    """dead = 真失效（404/410、域名解析不了）；blocked = 反爬/需登录/临时故障，不报问题。"""
    if st in (404, 410):
        return "dead"
    if st == 0:
        e = (err or "").lower()
        return "dead" if any(h in e for h in DNS_HINTS) else "blocked"
    if st in BLOCK_CODES or st >= 500:
        return "blocked"
    return "ok"


if __name__ == "__main__":
    import sys
    for u in sys.argv[1:]:
        st, final, err = status(u, refresh=True)
        print(f"  {verdict(st, err):11s} {st:3d} {u} -> {final if final != u else ''}")
