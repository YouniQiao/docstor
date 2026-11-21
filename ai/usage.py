"""AI 用量汇总：官方额度（opencode-go /usage，60s 缓存）+ 本项目累计（ai_usage_log 聚合）。"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import requests

from .cache import _query, stats, tokens_today
from .config import ROOT, key_fingerprint, load_config

CACHE_FILE = ROOT / ".ai_usage_cache.json"
TTL = 60
_CST = timezone(timedelta(hours=8))
_LABELS = (("rolling", "滚动窗口"), ("weekly", "本周"), ("monthly", "本月"))


def _fmt_tokens(n: int) -> str:
    if n >= 1e8:
        return f"{n/1e8:.1f}亿"
    if n >= 1e4:
        return f"{n/1e4:.1f}万"
    return str(n)


def _fmt_reset(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(_CST)
        return dt.strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "—"


def provider_usage(force: bool = False) -> dict:
    now = time.time()
    cfg = load_config()
    fp = key_fingerprint()
    cached: dict | None = None
    try:
        cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        cached = None
    same = bool(cached) and cached.get("_fp") == fp
    if cached and same and not force and now - float(cached.get("_ts", 0)) < TTL:
        return cached

    out: dict = {"_ts": now, "stale": False, "error": "", "_fp": fp}
    try:
        r = requests.get(f"{cfg.base_url}/usage",
                         headers={"Authorization": f"Bearer {cfg.api_key}", **cfg.extra_headers},
                         timeout=15)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        out.update(r.json().get("usage", {}) or {})
    except Exception as ex:  # noqa: BLE001
        if cached and same:
            cached["stale"] = True
            return cached
        out["stale"] = True
        out["error"] = str(ex)[:120]
    try:
        CACHE_FILE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return out


def local_usage() -> dict:
    s = stats()
    calls = sum(v["calls"] for v in s.values())
    tin = sum(v["tokens_in"] for v in s.values())
    tout = sum(v["tokens_out"] for v in s.values())
    failed = sum(v["failed"] for v in s.values())
    return {"calls": calls, "failed": failed, "tokens_in": tin, "tokens_out": tout,
            "tokens": tin + tout, "tokens_h": _fmt_tokens(tin + tout),
            "tokens_in_h": _fmt_tokens(tin), "tokens_out_h": _fmt_tokens(tout),
            "today_tokens": tokens_today(), "today_tokens_h": _fmt_tokens(tokens_today()),
            "kinds": s}


def recent_calls(limit: int = 20, offset: int = 0) -> list[dict]:
    rows = _query(
        "SELECT u.id, u.ts, u.kind, u.module, u.item_key, u.model, u.tokens_in, u.tokens_out, "
        "       u.latency_ms, u.ok, u.error, n.verdict, n.confidence "
        "FROM ai_usage_log u "
        "LEFT JOIN ai_notes n ON n.module=u.module AND n.item_key=u.item_key AND n.kind=u.kind "
        "ORDER BY u.id DESC LIMIT ? OFFSET ?", (limit, offset))
    return [dict(r) for r in rows]


def calls_total() -> int:
    return int(_query("SELECT COUNT(*) FROM ai_usage_log")[0][0] or 0)


def usage_summary() -> dict:
    p = provider_usage()
    gauges = []
    for key, label in _LABELS:
        d = p.get(key) or {}
        if not isinstance(d, dict) or d.get("percent") is None:
            continue
        pct = float(d["percent"])
        rem = round(max(0.0, 100.0 - pct), 1)
        color = "#d13438" if rem <= 5 else "#c2570a" if rem <= 20 else "#128c4a"
        gauges.append({"key": key, "label": label, "remaining": rem, "rem_color": color,
                       "percent": round(pct, 1), "reset": _fmt_reset(d.get("resetsAt", "")),
                       "status": d.get("status", "")})
    return {"ok": bool(gauges), "stale": bool(p.get("stale")), "error": p.get("error", ""),
            "gauges": gauges, "local": local_usage()}
