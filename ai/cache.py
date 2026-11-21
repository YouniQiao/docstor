"""AI 结果缓存与用量记账（都在 data/docstor.db）。

ai_cache     ：cache_key(=kind|model|prompt_version|输入哈希) → 模型输出，未变永不重复调用
ai_notes     ：面向页面的结论（module+item_key+kind 幂等覆盖），列表 join 用
ai_usage_log ：每次真实调用一条（含失败），页签的调用量/趋势/成本都靠它
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

from .config import load_config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_cache (
  cache_key TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  model TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  input_hash TEXT NOT NULL,
  output_json TEXT NOT NULL,
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_cache_kind ON ai_cache(kind);

CREATE TABLE IF NOT EXISTS ai_notes (
  module TEXT NOT NULL,
  item_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  verdict TEXT,
  confidence REAL,
  reason TEXT,
  suggestion TEXT,
  model TEXT,
  prompt_version TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (module, item_key, kind)
);
CREATE INDEX IF NOT EXISTS ix_ai_notes_kind ON ai_notes(kind);

CREATE TABLE IF NOT EXISTS ai_usage_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  module TEXT,
  item_key TEXT,
  model TEXT,
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  latency_ms INTEGER,
  cached INTEGER DEFAULT 0,
  ok INTEGER DEFAULT 1,
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_ai_usage_ts ON ai_usage_log(ts);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(load_config().db_path, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
    except Exception:  # noqa: BLE001
        pass
    return c


def ensure_schema() -> None:
    c = _conn()
    try:
        with c:
            c.executescript(_SCHEMA)
    finally:
        c.close()


def _write(sql: str, args: tuple) -> None:
    c = _conn()
    try:
        with c:
            c.execute(sql, args)
    finally:
        c.close()


def _query(sql: str, args: tuple = ()) -> list:
    c = _conn()
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def item_hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def cache_key(kind: str, model: str, pver: str, payload) -> str:
    return hashlib.sha256(f"{kind}|{model}|{pver}|{item_hash(payload)}".encode()).hexdigest()


def cache_get(key: str) -> dict | None:
    rows = _query("SELECT output_json FROM ai_cache WHERE cache_key=?", (key,))
    return json.loads(rows[0][0]) if rows else None


def cache_put(key: str, kind: str, model: str, pver: str, ihash: str,
              output: dict, usage: dict | None = None) -> None:
    u = usage or {}
    _write("INSERT OR REPLACE INTO ai_cache(cache_key,kind,model,prompt_version,input_hash,"
           "output_json,tokens_in,tokens_out,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
           (key, kind, model, pver, ihash, json.dumps(output, ensure_ascii=False),
            int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0), _now()))


def log_usage(kind: str, module: str, item_key: str, model: str, tokens_in: int,
              tokens_out: int, latency_ms: int, ok: bool = True, error: str = "") -> None:
    _write("INSERT INTO ai_usage_log(ts,kind,module,item_key,model,tokens_in,tokens_out,"
           "latency_ms,cached,ok,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
           (_now(), kind, module, item_key, model, tokens_in, tokens_out,
            latency_ms, 0, 1 if ok else 0, error[:300]))


def note_put(module: str, item_key: str, kind: str, data: dict, model: str, pver: str) -> None:
    _write("INSERT OR REPLACE INTO ai_notes(module,item_key,kind,verdict,confidence,reason,"
           "suggestion,model,prompt_version,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
           (module, item_key, kind, data.get("verdict"), data.get("confidence"),
            data.get("reason"), data.get("suggestion"), model, pver, _now()))


def note_get(module: str, kind: str = "") -> dict:
    if kind:
        rows = _query("SELECT item_key,verdict,confidence,reason,suggestion FROM ai_notes "
                      "WHERE module=? AND kind=?", (module, kind))
    else:
        rows = _query("SELECT item_key,verdict,confidence,reason,suggestion FROM ai_notes "
                      "WHERE module=?", (module,))
    return {r[0]: {"verdict": r[1], "confidence": r[2], "reason": r[3], "suggestion": r[4]}
            for r in rows}


def tokens_today() -> int:
    row = _query("SELECT COALESCE(SUM(tokens_in+tokens_out),0) FROM ai_usage_log "
                 "WHERE ts >= date('now','+8 hours')")[0]
    return int(row[0] or 0)


def stats() -> dict:
    """按 kind 汇总：调用数、token、失败数。"""
    out: dict = {}
    for r in _query("SELECT kind, COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0),"
                    " SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) FROM ai_usage_log GROUP BY kind"):
        out[r[0]] = {"calls": r[1], "tokens_in": r[2], "tokens_out": r[3], "failed": r[4] or 0}
    return out
