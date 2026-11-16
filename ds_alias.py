#!/usr/bin/python3
"""站内旧地址 → 新地址的别名跳转。

华为改过 slug 的文档，旧地址在官网会自动跳转，但我们只镜像了新 slug，
用户从旧链接进镜像站会 404。本模块维护一张别名表：

  slug_alias(lang, section, old_slug, new_lang, new_section, new_slug, updated_at)

数据来源（都不发网络请求）：`link_official` 里 verdict='renamed' 的缓存，
以及 `issues` 中 rule='link.renamed' 的 locator.new。
Flask 的 /docs/<path> 在文件缺失时查表并 302 到新路径（nginx 用 error_page 打进来）。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time

import ds_db

SCHEMA = """CREATE TABLE IF NOT EXISTS slug_alias(
    lang TEXT, section TEXT, old_slug TEXT,
    new_lang TEXT, new_section TEXT, new_slug TEXT, updated_at TEXT,
    PRIMARY KEY(lang, section, old_slug))"""

URL_RE = re.compile(r"/consumer/(cn|en)/doc/([^/]+)/([^/?#]+)")

# 官网目录 → 镜像板块（SECTION_CAT 是多对一，不能直接反转）
CAT_SECTION = {
    "harmonyos-guides": "capabilities",
    "harmonyos-references": "capabilities",
    "best-practices": "advanced-features",
    "harmonyos-faqs": "tools-faq",
    "harmonyos-releases": "release-notes",
    "app": "submission",
    "design-guides": "ux-design",
    "architecture-guides": "industry-solutions",
    "deveco-studio": "deveco-studio",
}


def cat_section(cat: str, fallback: str = "") -> str:
    return CAT_SECTION.get(cat, fallback)


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(SCHEMA)
    conn.commit()


def _parse(url: str):
    m = URL_RE.search(url or "")
    if not m:
        return None
    lang, cat, slug = m.groups()
    return lang, CAT_SECTION.get(cat, ""), slug   # section 未知时留空，resolve() 会用原板块兜底


def build(conn: sqlite3.Connection) -> int:
    """从已缓存的判定结果重建别名表（幂等，只读库不发请求）。"""
    ensure(conn)
    rows: list[tuple] = []
    # ① 官网判定缓存
    for r in conn.execute("SELECT lang, section, slug, detail FROM link_official"
                          " WHERE verdict='renamed' AND detail LIKE 'http%'"):
        p = _parse(r[3])
        if p and p[2] != r[2]:
            rows.append((r[0], r[1], r[2], p[0], p[1], p[2]))
    # ② 已改判的问题（locator.new）
    for r in conn.execute("SELECT lang, section, locator FROM issues"
                          " WHERE rule='link.renamed' AND status='open'"):
        loc = {}
        try:
            loc = json.loads(r[2] or "{}")
        except Exception:  # noqa: BLE001
            continue
        p = _parse(loc.get("new") or "")
        old = (loc.get("target") or "").split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        if p and old and p[2] != old:
            rows.append((r[0], r[1], old, p[0], p[1], p[2]))
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn.executemany(
        "INSERT OR REPLACE INTO slug_alias(lang,section,old_slug,new_lang,new_section,new_slug,updated_at)"
        " VALUES(?,?,?,?,?,?,?)", [r + (now,) for r in rows])
    conn.commit()
    return len(rows)


def resolve(conn: sqlite3.Connection, lang: str, section: str, slug: str,
            exists=None):
    """旧 (lang, section, slug) → 新三元组；exists(rel_path) 用来验证哪种板块拼法真有文件。"""
    try:
        row = conn.execute(
            "SELECT new_lang, new_section, new_slug FROM slug_alias"
            " WHERE lang=? AND section=? AND old_slug=?", (lang, section, slug)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row:
        return None
    nl, nsec, nslug = row[0], row[1] or section, row[2]
    cands = [(nl, nsec, nslug), (nl, section, nslug)]
    if exists:
        for c in dict.fromkeys(cands):
            if exists("/".join(c) + ".md"):
                return c
    return cands[0]
