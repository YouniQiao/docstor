#!/usr/bin/python3
"""官网站点「这条文档链接到底还能不能打开」的判定。

为什么不能用 HTTP 状态码：developer.huawei.com 是 SPA，任何不存在的
`/consumer/<lang>/doc/<cat>/<slug>` 也返回 200 + 同一个空壳 HTML。

真正的判据是文档中心接口 `getDocumentById({objectId: <slug>, catalogName: <cat>,
language: <lang>})` —— SPA 自己就是用它加载正文的：
  · code=0 + title → 这篇存在（还能顺带拿到正文，用来判是不是「迁移公告」）
  · code=92531031   → 官网没有这篇
catalogName 用链接所在板块对应的几个老/新目录名逐个试。

verdict:
  ok        官网有这篇
  moved     官网有，但正文只剩「内容已迁移/已下线」公告 → 链接该改，不是 404
  cn_only   英文站没有、中文站有 → 英文缺译
  dead      官网查不到 → 死链

结论缓存在 `link_official` 表（30 天）；离网时回退到 catalog_index / sitemap。
ds_check 用线程池，数据库连接和 requests 会话按线程各持一份。
"""
from __future__ import annotations

import html as htmllib
import re
import sqlite3
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB = BASE / "data/docstor.db"
SITEMAP_CN = BASE / "data/sitemap_doc_cn.xml"
TTL = 30 * 24 * 3600

# 板块 → 官网可能的 catalogName（新版 doccenter-* 与老版目录名）
SECTION_CATS = {
    "capabilities": ["doccenter-capabilities", "harmonyos-guides", "harmonyos-references",
                     "doccenter-references", "doccenter-doccenter"],
    "tools-faq": ["doccenter-tools-faq", "doccenter-dev-faq", "harmonyos-faqs"],
    "release-notes": ["doccenter-release-notes", "harmonyos-releases"],
    "advanced-features": ["doccenter-advanced-features", "best-practices"],
    "getting-started": ["doccenter-getting-started"],
    "deveco-studio": ["doccenter-deveco-studio", "deveco-studio"],
    "ux-design": ["doccenter-ux-design"],
    "submission": ["doccenter-submission"],
    "industry-solutions": ["doccenter-industry-solutions"],
}

MOVE_RE = re.compile(r"(已迁移|已下线|已下架|内容迁移|计划于.{0,8}下架)")

_SCHEMA = """CREATE TABLE IF NOT EXISTS link_official(
    lang TEXT, section TEXT, slug TEXT,
    verdict TEXT, detail TEXT, checked_at TEXT,
    PRIMARY KEY(lang, section, slug))"""


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


class Official:
    def __init__(self, db_path: Path | str | None = None):
        self._db_path = str(db_path or DB)
        self._local = threading.local()
        self._sm: set[str] | None = None

    @property
    def db(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30)
            conn.execute(_SCHEMA)
            conn.commit()
            self._local.conn = conn
        return conn

    @property
    def api(self):
        obj = getattr(self._local, "api", None)
        if obj is None:
            import ds_api
            obj = ds_api.DocCenter()
            self._local.api = obj
        return obj

    def sitemap(self) -> set[str]:
        if self._sm is None:
            if SITEMAP_CN.exists() and SITEMAP_CN.stat().st_size > 0:
                xml = SITEMAP_CN.read_text(encoding="utf-8", errors="ignore")
                self._sm = set(re.findall(r"/consumer/cn/doc/[^/<]+/([^/?#<]+)", xml))
            else:
                self._sm = set()
        return self._sm

    # ---------- 单次探测 ----------
    def _fetch(self, lang: str, section: str, slug: str) -> tuple[str, str, str]:
        """返回 (verdict, detail, title)。detail 命中时是 catalogName 或新链接。"""
        for cat in SECTION_CATS.get(section, [section]):
            try:
                d = self.api._post("getDocumentById",
                                   {"objectId": slug, "catalogName": cat, "language": lang})
            except Exception:  # noqa: BLE001
                continue
            if d.get("code") != 0:
                continue
            v = d.get("value") or {}
            body = (v.get("content") or {})
            html = body.get("content") if isinstance(body, dict) else str(body or "")
            text = _plain(html)
            if text and len(text) < 800 and MOVE_RE.search(text):
                m = re.search(r'href="([^"]+)"', html or "")
                return "moved", (m.group(1) if m else cat), str(v.get("title") or "")
            return "ok", cat, str(v.get("title") or "")
        return "", "", ""

    def _probe(self, lang: str, section: str, slug: str) -> tuple[str, str]:
        verdict, detail, _title = self._fetch(lang, section, slug)
        if verdict:
            return verdict, detail
        if lang == "en":                      # 英文站没有 → 看中文站有没有
            v2, d2, _ = self._fetch("cn", section, slug)
            if v2:
                return "cn_only", d2
        # 接口不通/超时的兜底
        try:
            row = self.db.execute("SELECT 1 FROM catalog_index WHERE lang=? AND slug=? LIMIT 1",
                                  (lang, slug)).fetchone()
            if row:
                return "ok", "index"
        except sqlite3.OperationalError:
            pass
        if slug in self.sitemap():
            return ("ok", "sitemap") if lang == "cn" else ("cn_only", "sitemap")
        return "dead", ""

    def verdict(self, lang: str, section: str, slug: str, refresh: bool = False) -> str:
        db = self.db
        if not refresh:
            row = db.execute("SELECT verdict, checked_at FROM link_official"
                             " WHERE lang=? AND section=? AND slug=?",
                             (lang, section, slug)).fetchone()
            if row:
                try:
                    if time.time() - time.mktime(time.strptime(row[1], "%Y-%m-%d %H:%M:%S")) < TTL:
                        return row[0]
                except Exception:  # noqa: BLE001
                    pass
        verdict, detail = self._probe(lang, section, slug)
        db.execute("INSERT OR REPLACE INTO link_official(lang,section,slug,verdict,detail,checked_at)"
                   " VALUES(?,?,?,?,?,?)",
                   (lang, section, slug, verdict, detail, time.strftime("%Y-%m-%d %H:%M:%S")))
        db.commit()
        return verdict

    def detail(self, lang: str, section: str, slug: str) -> str:
        row = self.db.execute("SELECT detail FROM link_official"
                              " WHERE lang=? AND section=? AND slug=?",
                              (lang, section, slug)).fetchone()
        return row[0] if row else ""


if __name__ == "__main__":
    import sys
    o = Official()
    for arg in [a for a in sys.argv[1:] if a != "-r"]:
        lang, section, slug = arg.split("/", 2)
        v = o.verdict(lang, section, slug, refresh="-r" in sys.argv)
        print(f"  {arg} -> {v}  {o.detail(lang, section, slug)}")
