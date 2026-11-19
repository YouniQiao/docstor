#!/usr/bin/python3
"""检查项开关（站点规则）的读写。

规则定义在 checks/REGISTRY（代码里的 default_on / langs 是出厂默认），
运行时以本模块的 rule_settings 表为准；表里没有的规则按出厂默认。

表：rule_settings(rule TEXT PRIMARY KEY, enabled INT, visible INT, langs TEXT, updated_at TEXT)
langs 用逗号分隔，如 "cn,en"。

两个开关各管一件事，不要混：
- enabled：跑不跑（ds_check / daily_sync 取数用）
- visible：露不露（全量问题、卡片数字、facets、导出、订阅预览、日报取数用）
组合成三态：对外 = 1/1、调试中 = 1/0、停用 = 0/0；enabled=0 时 visible 恒按 0 处理。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Iterable

SCHEMA = """CREATE TABLE IF NOT EXISTS rule_settings(
    rule TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    visible INTEGER NOT NULL DEFAULT 1,
    langs TEXT NOT NULL DEFAULT 'cn,en',
    updated_at TEXT)"""


def _migrate(conn: sqlite3.Connection) -> None:
    """老库没有 visible 列时补上：默认 1，等于「行为不变」。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(rule_settings)")}
    if "visible" not in cols:
        conn.execute("ALTER TABLE rule_settings ADD COLUMN visible INTEGER NOT NULL DEFAULT 1")
        conn.commit()


def ensure(conn: sqlite3.Connection, registry: dict) -> None:
    """建表 + 用出厂默认补齐缺失的规则（不覆盖用户已改过的）。"""
    conn.execute(SCHEMA)
    _migrate(conn)
    have = {r[0] for r in conn.execute("SELECT rule FROM rule_settings")}
    rows = []
    for rule, meta in registry.items():
        if rule in have:
            continue
        on = 1 if meta.get("default_on", True) else 0
        # 新规则可以在 checks 里声明 default_visible=False：天生「调试中」，不用手动去后台关
        vis = 1 if meta.get("default_visible", bool(on)) else 0
        langs = ", ".join(meta.get("langs") or ("cn", "en"))
        rows.append((rule, on, vis, langs, time.strftime("%Y-%m-%d %H:%M:%S")))
    if rows:
        conn.executemany("INSERT OR REPLACE INTO rule_settings"
                         "(rule,enabled,visible,langs,updated_at) VALUES(?,?,?,?,?)", rows)
        conn.commit()


def all_settings(conn: sqlite3.Connection) -> dict[str, dict]:
    """{rule: {enabled: bool, langs: [..]}}"""
    out: dict[str, dict] = {}
    try:
        for rule, enabled, visible, langs, upd in conn.execute(
                "SELECT rule, enabled, visible, langs, updated_at FROM rule_settings"):
            out[rule] = {"enabled": bool(enabled),
                         "visible": bool(enabled) and bool(visible),
                         "langs": [x for x in (langs or "").split(",") if x],
                         "updated_at": upd}
    except sqlite3.OperationalError:
        return {}
    return out


def save(conn: sqlite3.Connection, payload: dict[str, dict]) -> int:
    """批量保存：{rule: {"enabled": bool?, "visible": bool?, "langs": [..]?}}，返回改动条数。

    没传的字段保持原值（调接口时只想改一个开关不会被别的默认值覆盖）。
    停用（enabled=0）时强制 visible=0，界面上的「停用+对外」这种组合不会落库。
    """
    n = 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for rule, val in payload.items():
        cur = conn.execute("SELECT enabled, visible, langs FROM rule_settings WHERE rule=?",
                           (rule,)).fetchone()
        if cur is None:
            continue
        enabled = (1 if val.get("enabled") else 0) if "enabled" in val else int(cur[0])
        visible = (1 if val.get("visible") else 0) if "visible" in val else int(cur[1])
        langs = ",".join(val.get("langs") or []) if "langs" in val else cur[2]
        if not enabled:
            visible = 0
        conn.execute("UPDATE rule_settings SET enabled=?, visible=?, langs=?, updated_at=?"
                     " WHERE rule=?", (enabled, visible, langs, now, rule))
        n += 1
    conn.commit()
    return n


def visible_rules(conn: sqlite3.Connection, registry: dict,
                  lang: str | None = None) -> list[str]:
    """对外可见的规则（enabled & visible & 语言匹配）。

    所有展示口径都必须走这里：全量问题、卡片数字、facets、导出、订阅预览、日报。
    「调试中」的规则能在后台看到数字，但不从这里出来。
    """
    cfg = all_settings(conn)
    out: list[str] = []
    for rule, meta in registry.items():
        c = cfg.get(rule)
        if c is not None:
            if not c["enabled"] or not c["visible"]:
                continue
            langs = c["langs"] or ["cn", "en"]
        else:
            if not meta.get("default_on", True):
                continue
            if not meta.get("default_visible", meta.get("default_on", True)):
                continue
            langs = list(meta.get("langs") or ("cn", "en"))
        if lang and lang not in langs:
            continue
        out.append(rule)
    return out


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
