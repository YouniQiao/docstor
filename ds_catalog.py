#!/usr/bin/python3
"""每篇文档在官网的真实 catalogName（官网地址的路径段）。

docs.url 是按「板块 → 目录」推算出来的（ds_db.official_url），碰到
板块和目录不一一对应的情况就会拼错——例如 games 相关文档正文在
`games-guides` 目录下，却按板块拼成了 `harmonyos-guides`，官网列的
链接点开直接 404，想人工核对都做不到。

这里按 getDocumentById 的实测结果缓存真实目录，TTL 半年。
"""
from __future__ import annotations

import re
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
_put_fail = 0  # 缓存写失败次数（仅统计，不影响抓取）


def _db() -> sqlite3.Connection:
    """每线程一个连接：WAL + 自动提交 + 30s 忙等。

    自动提交很关键：以前留过未提交的写事务（如 _from_index 里的 CREATE TABLE），
    该线程就一直持有写锁，其它线程 put() 提交时撞上「database is locked」
    直接让整个板块抓取崩掉（cn/getting-started 就是这么挂的）。
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(DB), timeout=30, isolation_level=None)
        # 注意：不要在这里执行 PRAGMA journal_mode=WAL —— 它每次都要拿库级独占锁，
        # 几十个线程各建一次连接会把整批抓取拖到 2 篇/s，并频繁报 database is locked。
        # 库本身已经是 WAL（ds_db 建库时设的），这里只需设置忙等。
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(SCHEMA)
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
    """写缓存；并发下遇到锁就退避重试，不把整批抓取带崩。"""
    db = _db()
    sql = "INSERT OR REPLACE INTO doc_catalog(lang,slug,catalog,checked_at) VALUES(?,?,?,?)"
    args = (lang, slug, catalog, time.strftime("%Y-%m-%d %H:%M:%S"))
    for attempt in range(4):
        try:
            db.execute(sql, args)
            return
        except sqlite3.OperationalError as e:  # database is locked / busy
            if "lock" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            if attempt == 3:
                # 这里只是「目录名」缓存，写不进去不影响本次抓取结果（调用方已拿到
                # 算好的 catalog），绝不能因为缓存写失败把整批抓取带崩。
                global _put_fail
                _put_fail += 1
                if _put_fail <= 3:
                    print(f"   ⚠ doc_catalog 缓存写入失败（跳过）：{lang}/{slug} — {e}", flush=True)
                return
            time.sleep(0.3 * (attempt + 1))


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


# slug 前缀 → 目录的强提示（目录树索引里没有的那些老文档靠它少打几十次接口）
PREFIX_CATS = [
    ("gameobe", "games-guides"), ("gamemme", "games-guides"), ("games", "games-guides"),
    ("game-", "games-guides"), ("agc", "app"), ("quickgame", "quickApp-Guides"),
    ("cj-", "cangjie-guides"), ("rtm", "AppGallery-connect-References"),
    ("faqs-", "harmonyos-faqs"), ("card-", "atomic-guides"),
]


def _all_catalogs() -> list[str]:
    """官网目录树里出现过的全部目录名（含 games-guides 等不在 ALL_CATS 里的）。

    额外从 sitemap 里扫目录名：官网新开目录（新站点）时索引可能还没重建，
    扫一眼 sitemap 就能跟着走，不会把新目录下的页面误标成「已下架」。
    """
    db = _db()
    try:
        rows = db.execute("SELECT DISTINCT catalog FROM catalog_index").fetchall()
    except sqlite3.Error:
        rows = []
    names = [r[0] for r in rows if r[0]]
    for lang in ("cn", "en"):
        sm = BASE / "data" / f"sitemap_doc_{lang}.xml"
        try:
            if sm.exists() and sm.stat().st_size > 0:
                xml = sm.read_text(encoding="utf-8", errors="ignore")
                names += re.findall(rf"/consumer/{lang}/doc/([^/<]+)/", xml)
        except OSError:
            pass
    return list(dict.fromkeys(names))


def resolve(lang: str, section: str, slug: str, is_api: int = 0) -> str:
    """这篇属于哪个目录：目录树索引 → 板块对应目录 → 前缀提示 → 全目录实测。"""
    cat = _from_index(lang, slug)
    if cat:
        put(lang, slug, cat)
        return cat
    cats: list[str] = []
    if is_api:
        cats.append("harmonyos-references")
    if section:
        cats += list(ds_official.SECTION_CATS.get(section, []))
    cats += [c for pre, c in PREFIX_CATS if slug.startswith(pre)]
    cats += list(ds_official.ALL_CATS)
    cats += _all_catalogs()
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


INDEX_STAMP = BASE / "data" / ".catalog_index_at"


def fill_from_index() -> int:
    """把目录索引里的 (lang, slug) → 目录批量灌进 doc_catalog（纯本地 SQL，很快）。"""
    db = _db()
    try:
        db.execute("CREATE TABLE IF NOT EXISTS catalog_index(lang TEXT, catalog TEXT, slug TEXT)")
    except sqlite3.Error:
        return 0
    # 用 Python 做映射：SQL 关联写 3 万行要两分多钟，这里一秒内
    have = {(l, s) for l, s in db.execute("SELECT lang, slug FROM doc_catalog")}
    idx: dict[tuple[str, str], str] = {}
    any_lang: dict[str, str] = {}
    for lang, cat, slug in db.execute("SELECT lang, catalog, slug FROM catalog_index"):
        idx.setdefault((lang, slug), cat)
        any_lang.setdefault(slug, cat)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for lang, slug in db.execute("SELECT lang, slug FROM docs WHERE status='ok'"):
        if (lang, slug) in have:
            continue
        cat = idx.get((lang, slug)) or any_lang.get(slug)
        if cat:
            rows.append((lang, slug, cat, now))
    if rows:
        db.executemany("INSERT OR IGNORE INTO doc_catalog(lang, slug, catalog, checked_at)"
                       " VALUES(?,?,?,?)", rows)
        db.commit()
    return len(rows)


def resolve_missing(limit: int = 800, log=print) -> int:
    """新抓进来的文档在 doc_catalog 里还没有记录，逐个实测补上。"""
    import concurrent.futures

    db = _db()
    rows = db.execute("""SELECT d.lang, d.section, d.slug, COALESCE(d.is_api,0)
                         FROM docs d
                         WHERE d.status='ok'
                           AND NOT EXISTS(SELECT 1 FROM doc_catalog c
                                          WHERE c.lang=d.lang AND c.slug=d.slug)
                         LIMIT ?""", (limit,)).fetchall()
    if not rows:
        return 0
    done = 0

    def job(r):
        return resolve(r[0], r[1], r[2], int(r[3] or 0))

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(job, r) for r in rows]):
            try:
                if fut.result():
                    done += 1
            except Exception:  # noqa: BLE001
                pass
    log(f"   目录实测补全 {done}/{len(rows)} 篇")
    return done


def refresh(force: bool = False, log=print) -> dict:
    """抓取收尾调用：重建目录索引（12 小时内不重复）+ 灌 doc_catalog + 补新文档。

    目录索引决定「官网到底有没有这篇」，新文档/新目录不进来，检查就会
    误判死链、官网列也会显示「已下架」，所以抓完必须跟上。
    """
    import subprocess
    import sys
    import time as _t

    out = {"rebuilt": False, "filled": 0, "resolved": 0}
    stale = True
    try:
        if INDEX_STAMP.exists() and not force:
            stale = (_t.time() - INDEX_STAMP.stat().st_mtime) > 12 * 3600
    except OSError:
        stale = True
    if stale:
        log("   重建官网目录索引（catalog_index）…")
        try:
            r = subprocess.run([sys.executable, str(BASE / "scripts" / "build_catalog_index.py")],
                               cwd=str(BASE), capture_output=True, text=True, timeout=3600)
            if r.returncode == 0:
                INDEX_STAMP.write_text(_t.strftime("%Y-%m-%d %H:%M:%S"))
                out["rebuilt"] = True
            else:
                log(f"   ⚠ 目录索引重建失败：{(r.stderr or '')[-300:]}")
        except Exception as e:  # noqa: BLE001
            log(f"   ⚠ 目录索引重建异常：{e}")
    out["filled"] = fill_from_index()
    out["resolved"] = resolve_missing(log=log)
    log(f"   目录索引：新增 {out['filled']} 条映射，实测补全 {out['resolved']} 篇")
    return out


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
