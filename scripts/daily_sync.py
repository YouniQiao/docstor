#!/usr/bin/env python3
"""每日全量同步 + 全量检查（C 方案）。

流程：
  1) 逐板块抓取（cn 9 个 / en 8 个），内容未变的文档跳过写盘
  2) 每个语言跑一遍全量检查（全部板块、全部已开启规则、带外链探测）
  3) 汇总耗时与问题增删，写 data/last_sync.json 供页面展示

用法：
  python3 scripts/daily_sync.py                      # 全量（约 20~50 分钟）
  python3 scripts/daily_sync.py --lang cn             # 只跑中文
  python3 scripts/daily_sync.py --skip-check          # 只同步不检查
  python3 scripts/daily_sync.py --section ux-design --limit 5   # 冒烟测试
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DB = BASE / "data" / "docstor.db"
LAST = BASE / "data" / "last_sync.json"
LOCK = BASE / "data" / ".daily_sync.lock"
LOGDIR = BASE / "logs" / "daily"

SECTIONS = {
    "cn": ["advanced-features", "capabilities", "deveco-studio", "getting-started",
           "industry-solutions", "release-notes", "submission", "tools-faq", "ux-design"],
    "en": ["advanced-features", "capabilities", "deveco-studio", "getting-started",
           "release-notes", "submission", "tools-faq", "ux-design"],
}


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now()}] {msg}", flush=True)


def _enabled_rules(con: sqlite3.Connection) -> tuple[str, list]:
    """与全量问题页同口径：只统计「已开启」的规则（关闭的规则数据仍在库里，但不上报）。"""
    try:
        import checks
        cfg = {}
        try:
            for rule, enabled in con.execute("SELECT rule, enabled FROM rule_settings"):
                cfg[rule] = bool(enabled)
        except sqlite3.OperationalError:
            pass
        enabled = [r for r, meta in checks.REGISTRY.items()
                   if cfg.get(r, meta.get("default_on", True))]
        if not enabled:
            return "", []
        return (" AND rule IN (" + ",".join("?" * len(enabled)) + ")", enabled)
    except Exception:  # noqa: BLE001
        return "", []


def counts() -> dict:
    con = sqlite3.connect(DB)
    try:
        cond, args = _enabled_rules(con)
        q = lambda s, a=(): con.execute(s, a).fetchone()[0]  # noqa: E731
        return {
            "docs": q("SELECT COUNT(*) FROM docs WHERE status='ok'"),
            "open": q(f"SELECT COUNT(*) FROM issues WHERE status='open'{cond}", args),
            "resolved": q(f"SELECT COUNT(*) FROM issues WHERE status='resolved'{cond}", args),
        }
    finally:
        con.close()


def run_step(cmd: list[str], log_path: Path, timeout: int) -> tuple[int, float]:
    t0 = time.time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n===== {now()} $ {' '.join(cmd)}\n")
        f.flush()
        try:
            p = subprocess.run(cmd, cwd=str(BASE), stdout=f,
                               stderr=subprocess.STDOUT, timeout=timeout)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            f.write(f"\n!! 超时（>{timeout}s）\n")
            rc = 124
    return rc, time.time() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", action="append", choices=["cn", "en"])
    ap.add_argument("--section", default="", help="只跑指定板块（冒烟用）")
    ap.add_argument("--limit", type=int, default=0, help="每个板块只抓前 N 篇（冒烟用）")
    ap.add_argument("--skip-ingest", action="store_true")
    ap.add_argument("--skip-check", action="store_true")
    ap.add_argument("--workers", type=int, default=8, help="检查并发")
    ap.add_argument("--new-docs-only", action="store_true", help="兼容旧参数，无作用")
    args = ap.parse_args()

    # 单实例锁：cron 与手动补跑撞车时会互相抢 SQLite 写锁，
    # 两边都崩（历史上 cn/getting-started 连续失败就是这么来的）。
    lock_fp = open(LOCK, "w")
    try:
        fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("⚠️  已有同步任务在跑，本次退出")
        return 2
    lock_fp.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} pid={__import__('os').getpid()}\n")
    lock_fp.flush()

    langs = args.lang or ["cn", "en"]
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    started = now()
    t_all = time.time()
    before = counts()
    log(f"🚀 每日任务开始 | 语言 {'+'.join(langs)} | 抓前：文档 {before['docs']:,} 篇、"
        f"未解决 {before['open']:,}、已解决 {before['resolved']:,}")

    failed: list[str] = []
    steps: list[dict] = []

    # ── 1. 逐板块抓取 ────────────────────────────────────────────────
    if not args.skip_ingest:
        for lang in langs:
            secs = [args.section] if args.section else SECTIONS[lang]
            for sec in secs:
                cmd = [sys.executable, "ds_ingest.py", "--lang", lang, "--section", sec]
                if args.limit:
                    cmd += ["--limit", str(args.limit)]
                lp = LOGDIR / f"{stamp}_ingest_{lang}_{sec}.log"
                for attempt in (1, 2):
                    rc, dt = run_step(cmd, lp, timeout=3600)
                    steps.append({"step": f"ingest {lang}/{sec}", "rc": rc,
                                  "seconds": round(dt, 1), "attempt": attempt})
                    if rc == 0:
                        log(f"   ✅ 抓取 {lang}/{sec} 完成，{dt:.0f}s")
                        break
                    log(f"   ⚠️  抓取 {lang}/{sec} 失败（rc={rc}，{dt:.0f}s），"
                        f"{'重试一次' if attempt == 1 else '跳过'}；日志 {lp}")
                    if attempt == 2:
                        failed.append(f"ingest {lang}/{sec}")
    else:
        log("   ⏭  跳过抓取")

    # ── 2. 全量检查 ─────────────────────────────────────────────────
    if not args.skip_check:
        for lang in langs:
            cmd = [sys.executable, "ds_check.py", "--lang", lang,
                   "--section", (args.section or "ALL"),
                   "--external", "--workers", str(args.workers), "--incremental"]
            lp = LOGDIR / f"{stamp}_check_{lang}.log"
            rc, dt = run_step(cmd, lp, timeout=7200)
            steps.append({"step": f"check {lang}", "rc": rc, "seconds": round(dt, 1)})
            if rc == 0:
                log(f"   ✅ 检查 {lang} 完成，{dt / 60:.1f} 分钟")
            else:
                log(f"   ⚠️  检查 {lang} 失败（rc={rc}）；日志 {lp}")
                failed.append(f"check {lang}")
    else:
        log("   ⏭  跳过检查")

    # ── 3. 汇总 ─────────────────────────────────────────────────────
    after = counts()
    total = time.time() - t_all
    summary = {
        "started_at": started,
        "finished_at": now(),
        "seconds": round(total),
        "langs": langs,
        "docs": {"before": before["docs"], "after": after["docs"]},
        "issues": {
            "open_before": before["open"], "open_after": after["open"],
            "resolved_before": before["resolved"], "resolved_after": after["resolved"],
            "new": max(0, after["resolved"] + after["open"] - before["resolved"] - before["open"]),
            "resolved": max(0, after["resolved"] - before["resolved"]),
        },
        "failed": failed,
        "ok": not failed,
        "steps": steps,
    }
    LAST.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"🏁 结束，用时 {total / 60:.1f} 分钟 | 文档 {before['docs']:,} → {after['docs']:,} | "
        f"未解决 {before['open']:,} → {after['open']:,}（新增 {summary['issues']['new']}、"
        f"解决 {summary['issues']['resolved']}）"
        + (f" | ⚠️ 失败 {len(failed)} 项: {', '.join(failed)}" if failed else ""))
    # ── 4. 重建 doc_cat（问题页按侧边栏细分筛选要用，必须跟着最新目录树）──
    try:
        def _subs() -> set:
            try:
                with sqlite3.connect(str(DB)) as c:
                    return {(l, s, sb) for l, s, sb in c.execute(
                        "SELECT lang, section, sub FROM doc_cat WHERE sub IS NOT NULL AND sub<>''")}
            except sqlite3.OperationalError:
                return set()

        subs_before = _subs()
        r = subprocess.run([sys.executable, str(BASE / "scripts" / "build_doc_cat.py")],
                           capture_output=True, text=True, timeout=900, cwd=str(BASE))
        tail = [l.strip() for l in (r.stdout or "").strip().splitlines() if "doc_cat" in l]
        log("🗂 目录归类重建：" + (tail[-1] if tail else (r.stderr or "").strip()[:160] or "无输出"))
        # 细分增减单独记一行：官网新挂出产品分支（如英文「工具」的 HarmonyOS PC Edition）时能一眼看到
        added = _subs() - subs_before
        for l, s, sb in sorted(added)[:20]:
            log(f"🆕 新增细分 {l}/{s}：{sb}")
    except Exception as e:  # noqa: BLE001
        log(f"🗂 目录归类重建失败：{e}")

    # ── 5. 清理回收站与整库备份（保留 30 天）────────────────────
    try:
        r = subprocess.run([sys.executable, str(BASE / "scripts" / "clean_trash.py"),
                            "--days", "30", "--apply"],
                           capture_output=True, text=True, timeout=900, cwd=str(BASE))
        out = (r.stdout or "").strip().splitlines()
        head = [l.strip() for l in out if "待清理" in l or "已清理" in l]
        log("🧹 回收站/备份清理：" + ("；".join(head) if head else (out[-1].strip() if out else "无输出")))
    except Exception as e:  # noqa: BLE001  清理失败不能影响主流程
        log(f"🧹 回收站/备份清理失败：{e}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
