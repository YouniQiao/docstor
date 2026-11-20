#!/usr/bin/python3
"""后台「数据缓存」看板的数据源。

所有判定类缓存都是懒过期：过期不等于被清掉，而是「下次用到时重判一次」。
这里只读——除了把每天的条数快照写进 cache_stat（用来算日增量）。

保质期口径必须和 ds_official / ds_http / ds_render / ds_catalog 里的常量一致，
改那边记得改这里，否则看板会骗人。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "data/docstor.db"
SITEMAP = BASE / "data/sitemap_doc_cn.xml"
INDEX_STAMP = BASE / "data" / ".catalog_index_at"
DAY = 86400

STAT_SCHEMA = """CREATE TABLE IF NOT EXISTS cache_stat(
    day TEXT, name TEXT, n INTEGER, expired INTEGER,
    PRIMARY KEY(day, name))"""


def _ago(days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - days * DAY))


def _fmt(n: int) -> str:
    return f"{n:,}"


def _short(ts: str | None) -> str:
    """2026-11-16 15:37:35 → 11-16 15:37（表格里省地方）。"""
    if not ts:
        return "—"
    try:
        return ts[5:16]
    except Exception:  # noqa: BLE001
        return ts


def _mtime(p: Path) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))
    except OSError:
        return "—"


def _tables() -> list[dict]:
    """7 张判定/索引缓存。expired 是「现在去重判会命中多少条」。"""
    return [
        {
            "name": "doc_catalog",
            "use": "文档在官网的目录名（拼地址用）",
            "ttl": "90 天",
            "count": "SELECT COUNT(*) FROM doc_catalog",
            "where": f"checked_at < '{_ago(90)}'",
            "range": "SELECT MIN(checked_at), MAX(checked_at) FROM doc_catalog",
            "recheck": None,
        },
        {
            "name": "link_official",
            "use": "官网有没有这篇的判断",
            "ttl": "ok 30 天 · 有问题 3 天",
            "count": "SELECT COUNT(*) FROM link_official",
            "where": (f"(verdict='ok' AND checked_at < '{_ago(30)}') OR "
                      f"(verdict!='ok' AND checked_at < '{_ago(3)}')"),
            "range": "SELECT MIN(checked_at), MAX(checked_at) FROM link_official",
            "recheck": "official",
        },
        {
            "name": "link_http",
            "use": "第三方外链可达性",
            "ttl": "2xx 30 · 404/410 3 · 其它 7 天",
            "count": "SELECT COUNT(*) FROM link_http",
            "where": (f"(status BETWEEN 200 AND 299 AND checked_at < '{_ago(30)}') OR "
                      f"(status IN (404,410) AND checked_at < '{_ago(3)}') OR "
                      f"(status NOT BETWEEN 200 AND 299 AND status NOT IN (404,410) "
                      f" AND checked_at < '{_ago(7)}')"),
            "range": "SELECT MIN(checked_at), MAX(checked_at) FROM link_http",
            "recheck": "http",
        },
        {
            "name": "link_render",
            "use": "真渲染复核（疑难死链）",
            "ttl": "ok 30 · missing 3 · timeout 1 天",
            "count": "SELECT COUNT(*) FROM link_render",
            "where": (f"(verdict='ok' AND checked_at < '{_ago(30)}') OR "
                      f"(verdict='missing' AND checked_at < '{_ago(3)}') OR "
                      f"(verdict='timeout' AND checked_at < '{_ago(1)}')"),
            "range": "SELECT MIN(checked_at), MAX(checked_at) FROM link_render",
            "recheck": None,
        },
        {
            "name": "url_render",
            "use": "任意地址渲染（外链兜底）",
            "ttl": "ok 30 · missing 3 · timeout 1 天",
            "count": "SELECT COUNT(*) FROM url_render",
            "where": (f"(verdict='ok' AND checked_at < '{_ago(30)}') OR "
                      f"(verdict='missing' AND checked_at < '{_ago(3)}') OR "
                      f"(verdict='timeout' AND checked_at < '{_ago(1)}')"),
            "range": "SELECT MIN(checked_at), MAX(checked_at) FROM url_render",
            "recheck": None,
        },
        {
            "name": "catalog_index",
            "use": "官网目录树索引（查表，不打网络）",
            "ttl": "每天重建",
            "count": "SELECT COUNT(*) FROM catalog_index",
            "where": "0",
            "range": None,          # 没有 checked_at，看重建时间
            "recheck": None,
        },
    ]


def expired_where(name: str) -> str:
    """某张表的「过期」条件（SQL 片段），重判脚本与看板共用一份。"""
    for spec in _tables():
        if spec["name"] == name:
            return spec["where"]
    raise KeyError(name)


STATE = BASE / "data" / ".cache_recheck.json"


def _running() -> dict | None:
    """正在跑的「重判」进度（30 分钟内、且没有 finished_at 才算在跑）。"""
    import json
    try:
        d = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if d.get("finished_at"):
        # 结束后保留 10 分钟给页面显示「上次重判完成」
        try:
            t = time.mktime(time.strptime(d["finished_at"], "%Y-%m-%d %H:%M:%S"))
            return None if time.time() - t > 600 else d
        except Exception:  # noqa: BLE001
            return None
    try:
        t = time.mktime(time.strptime(d["started_at"], "%Y-%m-%d %H:%M:%S"))
        if time.time() - t > 1800:
            return None
    except Exception:  # noqa: BLE001
        return None
    return d


def stats() -> dict:
    """给 /admin 用：每张缓存的条数 / 日增量 / 最老最新 / 过期数 + 合计。"""
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute(STAT_SCHEMA)
        con.commit()
        rows: list[dict] = []
        for spec in _tables():
            n = con.execute(spec["count"]).fetchone()[0]
            expired = con.execute(f"SELECT COUNT(*) FROM {spec['name']} WHERE {spec['where']}").fetchone()[0]
            single = False
            if spec["range"]:
                oldest, newest = con.execute(spec["range"]).fetchone()
            else:                      # catalog_index：用重建时间戳
                oldest = newest = _mtime(INDEX_STAMP)
                single = True
            rows.append({
                "name": spec["name"], "use": spec["use"], "ttl": spec["ttl"],
                "n": n, "n_text": _fmt(n),
                "oldest": _short(oldest), "newest": _short(newest),
                "oldest_full": oldest or "", "newest_full": newest or "",
                "expired": expired, "recheck": spec["recheck"],
                "single": single,
            })
        # sitemap 是文件，不是表
        try:
            size = SITEMAP.stat().st_size
            mt = SITEMAP.stat().st_mtime
            rows.append({
                "name": "sitemap", "use": "官网 sitemap 文件",
                "ttl": "7 天",
                "n": size, "n_text": f"{size / 1048576:.1f} MB",
                "oldest": _short(_mtime(SITEMAP)), "newest": _short(_mtime(SITEMAP)),
                "oldest_full": "", "newest_full": "", "single": True,
                "expired": 1 if time.time() - mt > 7 * DAY else 0,
                "recheck": None,
            })
        except OSError:
            pass
        for r in rows:
            r["delta"] = None
        # ── 日增量：跟昨天的快照比 ──────────────────────────────────
        today = time.strftime("%Y-%m-%d")
        prev = {r["name"]: r["n"] for r in con.execute(
            "SELECT name, n FROM cache_stat WHERE day=(SELECT MAX(day) FROM cache_stat WHERE day<?)",
            (today,))}
        for r in rows:
            if r["name"] in prev:
                r["delta"] = r["n"] - prev[r["name"]]
        con.executemany("INSERT OR REPLACE INTO cache_stat(day,name,n,expired) VALUES(?,?,?,?)",
                        [(today, r["name"], r["n"], r["expired"]) for r in rows])
        con.commit()
        return {
            "rows": rows,
            "running": _running(),
            "total": sum(r["n"] for r in rows if r["name"] != "sitemap"),
            "total_expired": sum(r["expired"] or 0 for r in rows),
            "day": today,
            "at": time.strftime("%H:%M"),
        }
    finally:
        con.close()


if __name__ == "__main__":
    import json
    print(json.dumps(stats(), ensure_ascii=False, indent=1))
