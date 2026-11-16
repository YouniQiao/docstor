#!/usr/bin/python3
"""检查项开关（站点规则）的读写。

规则定义在 checks/REGISTRY（代码里的 default_on / langs 是出厂默认），
运行时以本模块的 rule_settings 表为准；表里没有的规则按出厂默认。

表：rule_settings(rule TEXT PRIMARY KEY, enabled INT, langs TEXT, updated_at TEXT)
langs 用逗号分隔，如 "cn,en"。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

SCHEMA = """CREATE TABLE IF NOT EXISTS rule_settings(
    rule TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    langs TEXT NOT NULL DEFAULT 'cn,en',
    updated_at TEXT)"""


def ensure(conn: sqlite3.Connection, registry: dict) -> None:
    """建表 + 用出厂默认补齐缺失的规则（不覆盖用户已改过的）。"""
    conn.execute(SCHEMA)
    have = {r[0] for r in conn.execute("SELECT rule FROM rule_settings")}
    rows = []
    for rule, meta in registry.items():
        if rule in have:
            continue
        langs = ",".join(meta.get("langs") or ("cn", "en"))
        rows.append((rule, 1 if meta.get("default_on", True) else 0, langs,
                     time.strftime("%Y-%m-%d %H:%M:%S")))
    if rows:
        conn.executemany("INSERT OR REPLACE INTO rule_settings(rule,enabled,langs,updated_at)"
                         " VALUES(?,?,?,?)", rows)
        conn.commit()


def all_settings(conn: sqlite3.Connection) -> dict[str, dict]:
    """{rule: {enabled: bool, langs: [..]}}"""
    out: dict[str, dict] = {}
    try:
        for rule, enabled, langs, upd in conn.execute(
                "SELECT rule, enabled, langs, updated_at FROM rule_settings"):
            out[rule] = {"enabled": bool(enabled),
                         "langs": [x for x in (langs or "").split(",") if x],
                         "updated_at": upd}
    except sqlite3.OperationalError:
        return {}
    return out


def save(conn: sqlite3.Connection, payload: dict[str, dict]) -> int:
    """批量保存：{rule: {"enabled": bool, "langs": [..]}}，返回改动条数。"""
    n = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for rule, val in payload.items():
        conn.execute("UPDATE rule_settings SET enabled=?, langs=?, updated_at=? WHERE rule=?",
                     (1 if val.get("enabled") else 0,
                      ",".join(val.get("langs") or []), now, rule))
        n += 1
    conn.commit()
    return n


def effective(conn: sqlite3.Connection, registry: dict, lang: str) -> list[str]:
    """某语言下当前启用的规则：出厂默认 + 表内配置（表内优先），再按语言过滤。"""
    cfg = all_settings(conn)
    out = []
    for rule, meta in registry.items():
        c = cfg.get(rule)
        if c is not None:
            if not c["enabled"]:
                continue
            if lang and lang not in c["langs"]:
                continue
        else:
            if not meta.get("default_on", True):
                continue
            if lang and lang not in (meta.get("langs") or ("cn", "en")):
                continue
        out.append(rule)
    return out
