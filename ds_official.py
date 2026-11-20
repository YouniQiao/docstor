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

结论缓存在 `link_official` 表：`ok` 留 30 天，「报问题的结论」（dead/cn_only/moved/renamed）
只留 3 天——官网修好后最多 3 天就能自动从问题列表里消失；离网时回退到 catalog_index / sitemap。
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
TTL = 30 * 24 * 3600          # 官网「有这篇」：没问题的不用勤查，全量 3.2 万条靠它省请求
TTL_SUSPECT = 3 * 24 * 3600   # 「dead / cn_only / moved」：正在报问题，官网随时可能修好，3 天重判一次

# 板块 → 官网可能的 catalogName（新版 doccenter-* 与老版目录名）
SECTION_CATS = {
    "capabilities": ["doccenter-capabilities", "harmonyos-guides", "harmonyos-references",
                     "doccenter-references", "doccenter-doccenter"],
    "tools-faq": ["doccenter-tools-faq", "doccenter-dev-faq", "harmonyos-faqs"],
    "release-notes": ["doccenter-release-notes", "harmonyos-releases"],
    "advanced-features": ["doccenter-advanced-features", "best-practices"],
    # 元服务（atomic-*）和部分新手引导文档实际挂在 atomic-guides / atomic-references /
    # harmonyos-guides 下，只写 doccenter-getting-started 会导致按板块探测全部失败，
    # 进而把「这篇的官网目录」缓存成空串，页面上显示成「已下架」。
    "getting-started": ["doccenter-getting-started", "atomic-guides",
                        "atomic-references", "harmonyos-guides"],
    "deveco-studio": ["doccenter-deveco-studio", "deveco-studio"],
    "ux-design": ["doccenter-ux-design"],
    "submission": ["doccenter-submission"],
    "industry-solutions": ["doccenter-industry-solutions"],
}

MOVE_RE = re.compile(r"(已迁移|已下线|已下架|内容迁移|计划于.{0,8}下架)")

# 查旧地址映射时把所有目录都试一遍：链接里的板块段经常和文档真实目录不一致
ALL_CATS = list(dict.fromkeys(
    [c for cats in SECTION_CATS.values() for c in cats]
    + ["harmonyos-guides", "harmonyos-references", "best-practices", "harmonyos-faqs",
       "harmonyos-releases", "app", "design-guides", "architecture-guides"]))


def _slug_variants(slug: str) -> list[str]:
    """官网改 slug 的几种常见形态（下划线/连字符互换、加 bpta- 前缀、去掉 -V14 版本后缀）。

    实测：ts-universal-events-focus_axis → …-focus-axis、ide-signing-V14 → ide-signing、
    multi-mobile-payment → bpta-multi-mobile-payment。旧地址在官网仍能打开。
    """
    s = re.sub(r"\.(md|html?|shtml)$", "", slug.strip())       # 相对 .md 链接会带后缀
    slug = s or slug
    out = [slug]
    if slug != slug.lower():
        out.append(slug.lower())        # 官网 slug 全小写，源码里常写成驼峰
    if "_" in slug:
        out.append(slug.replace("_", "-"))
    if "-" in slug:
        out.append(slug.replace("-", "_"))
    out.append("bpta-" + slug)
    out.append(re.sub(r"-V\d+$", "", slug))
    out.append(re.sub(r"-0\d{12,}$", "", slug))   # 老式「slug-0000001820881413」数字后缀
    return list(dict.fromkeys(x for x in out if x))

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
        cats = SECTION_CATS.get(section, [section])
        cats = list(cats) + [c for c in ALL_CATS if c not in cats]   # 老板块名（multi-device…）也要能查到
        for cat in cats:
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

    def _mapping(self, lang: str, section: str, slug: str) -> str:
        """官方的「旧地址 → 新地址」映射（getUrlMapping）。

        文档改过 slug 后，旧地址在官网仍能打开（SPA 会自动跳转），
        但 getDocumentById 用旧 slug 查不到——这是此前把这类链接误判成死链的根因。
        """
        for cat in ALL_CATS:
            url = f"https://developer.huawei.com/consumer/{lang}/doc/{cat}/{slug}"
            try:
                d = self.api._post("getUrlMapping", {"oldUrl": url})
            except Exception:  # noqa: BLE001
                continue
            new = (d.get("newUrl") or "").strip()
            if new and new.rstrip("/").rsplit("/", 1)[-1] != slug:
                return new
        return ""

    def _resolve(self, lang: str, section: str, slug: str) -> tuple[str, str]:
        """本语言内找这篇：原 slug → slug 变体 → 旧地址映射。返回 (verdict, detail)。"""
        verdict, detail, _title = self._fetch(lang, section, slug)
        if verdict:
            return verdict, detail
        for alt in _slug_variants(slug):
            if alt == slug:
                continue
            v2, cat2, _ = self._fetch(lang, section, alt)
            if v2 in ("ok", "moved"):
                return "renamed", f"https://developer.huawei.com/consumer/{lang}/doc/{cat2}/{alt}"
        new = self._mapping(lang, section, slug)      # 旧地址能自动跳转到新地址 → 不是死链
        if new:
            m = re.search(r"/consumer/(cn|en)/doc/", new)
            if lang == "en" and m and m.group(1) == "cn":
                return "cn_only", new                 # 英文旧地址被跳去中文页 = 英文缺译
            return "renamed", new
        return "", ""

    def _probe(self, lang: str, section: str, slug: str) -> tuple[str, str]:
        verdict, detail = self._resolve(lang, section, slug)
        if verdict:
            return verdict, detail
        # 英文站查不到就是 404（中文站有没有同篇不影响这个 en 地址打不开），不再降级成 cn_only
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
        slug = re.sub(r"\.(md|html?|shtml)$", "", (slug or "").strip())   # 相对 .md 链接带的文件后缀
        db = self.db
        if not refresh:
            row = db.execute("SELECT verdict, checked_at FROM link_official"
                             " WHERE lang=? AND section=? AND slug=?",
                             (lang, section, slug)).fetchone()
            if row:
                # 保质期跟着结论走：报问题的结论（dead/cn_only/moved）只留 3 天，
                # 官网修好后最多 3 天就能自动消失；「有这篇」留 30 天，省掉 3.2 万次请求。
                ttl = TTL if row[0] == "ok" else TTL_SUSPECT
                try:
                    if time.time() - time.mktime(time.strptime(row[1], "%Y-%m-%d %H:%M:%S")) < ttl:
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
