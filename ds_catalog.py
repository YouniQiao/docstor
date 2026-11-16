#!/usr/bin/python3
"""每篇文档在官网的真实 catalogName（官网地址的路径段）。

docs.url 是按「板块 → 目录」推算出来的（ds_db.official_url），碰到
板块和目录不一一对应的情况就会拼错——例如 games 相关文档正文在
`games-guides` 目录下，却按板块拼成了 `harmonyos-guides`，官网列的
链接点开直接 404，想人工核对都做不到。

这里按 getDocumentById 的实测结果缓存真实目录，TTL 半年。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import ds_api
import ds_official

BASE = Path(__file__).resolve().parent
DB = BASE / "data" / "docstor.db"
TTL = 180 * 24 * 3600

SCHEMA = """CREATE TABLE IF NOT EXISTS doc_catalog(
    lang TEXT, slug TEXT, catalog TEXT, checked_at TEXT,
    PRIMARY KEY(lang, slug))"""

_local = threading.local()


def _db() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB), timeout=30)
        conn.execute(SCHEMA)
        conn.commit()
        _local.conn = conn
    return conn


def _api() -> ds_api.DocCenter:
    api = getattr(_local, "api", None)
    if api is None:
        api = ds_api.DocCenter()
        _local.api = api
    return api


def get(lang: str, slug: str) -> str | None:
    """缓存里的目录名；None 表示没查过，"" 表示查过但哪儿都没有。"""
    row = _db().execute("SELECT catalog, checked_at FROM doc_catalog WHERE lang=? AND slug=?",
                        (lang, slug)).fetchone()
    if not row:
        return None
    try:
        if time.time() - time.mktime(time.strptime(row[1], "%Y-%m-%d %H:%M:%S")) < TTL:
            return row[0]
    except Exception:  # noqa: BLE001
        pass
    return None


def put(lang: str, slug: str, catalog: str) -> None:
    db = _db()
    db.execute("INSERT OR REPLACE INTO doc_catalog(lang,slug,catalog,checked_at) VALUES(?,?,?,?)",
               (lang, slug, catalog, time.strftime("%Y-%m-%d %H:%M:%S")))
    db.commit()


def _from_index(lang: str, slug: str) -> str:
    """官网目录树索引里直接就有 (lang, slug) → 目录名，最可靠、零请求。"""
    db = _db()
    try:
        db.execute("CREATE TABLE IF NOT EXISTS catalog_index(lang TEXT, catalog TEXT, slug TEXT)")
    except sqlite3.Error:
        return ""
    row = db.execute("SELECT catalog FROM catalog_index WHERE lang=? AND slug=? LIMIT 1",
                     (lang, slug)).fetchone()
    if not row:
        row = db.execute("SELECT catalog FROM catalog_index WHERE slug=? LIMIT 1", (slug,)).fetchone()
    return (row[0] or "") if row else ""


def resolve(lang: str, section: str, slug: str, is_api: int = 0) -> str:
    """这篇属于哪个目录：目录树索引 → 板块对应目录 → 全目录实测。"""
    cat = _from_index(lang, slug)
    if cat:
        put(lang, slug, cat)
        return cat
    cats: list[str] = []
    if is_api:
        cats.append("harmonyos-references")
    if section:
        cats += list(ds_official.SECTION_CATS.get(section, []))
    cats += list(ds_official.ALL_CATS)
    api = _api()
    for cat in dict.fromkeys(cats):
        try:
            d = api._post("getDocumentById",
                          {"objectId": slug, "catalogName": cat, "language": lang})
        except Exception:  # noqa: BLE001
            continue
        if d.get("code") == 0:
            put(lang, slug, cat)
            return cat
    put(lang, slug, "")
    return ""


def url_for(lang: str, section: str, slug: str, is_api: int = 0) -> str:
    """官网原文地址：用实测目录，查不到再退回按板块推算。"""
    cat = get(lang, slug)
    if cat is None:
        cat = resolve(lang, section, slug, is_api)
    if cat == "":
        return ""            # 实测官网已无此篇（还有空目录缓存），别硬拼一个必然 404 的地址
    if not cat:
        import ds_db
        return ds_db.official_url(lang, section, slug, is_api)
    return f"https://developer.huawei.com/consumer/{lang}/doc/{cat}/{slug}"
