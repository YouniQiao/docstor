"""官网原文 URL 解析。

只用「板块 → 目录段」的固定映射是不够的：官网同一板块下其实挂着多个目录，
例如「开发与测试」里同时有 harmonyos-guides / games-guides / atomic-guides /
atomic-references / app / service / AppGallery-connect-*，用板块名硬拼会 404。

做法：
1. 下载官网文档 sitemap（`/consumer/<lang>/sitemap/doc/sitemap1.xml`），得到
   `slug → {catname}` 的权威锚点（本地缓存 7 天）。
2. sitemap 里 slug 唯一命中 → 直接用它。
3. 其余文档（新文档 sitemap 常滞后）在目录树上投票：从自己往上找最近的
   祖先节点，若该节点下已解析的文档 ≥90% 落在同一个 catname，就继承它——
   官网每个目录节点就是一个发布目录，兄弟节点同目录，留一法实测 100% 命中。
4. 还判不出来时，API 参考回落 `harmonyos-references`，其它回落板块默认目录。
"""

from __future__ import annotations

import collections
import re
import time
import urllib.request
from pathlib import Path

SITEMAP_TPL = "https://developer.huawei.com/consumer/{lang}/sitemap/doc/sitemap{n}.xml"

# 板块 → 默认目录段（浏览器逐板块核对过，作为兜底）
SECTION_CAT = {
    "capabilities": "harmonyos-guides",
    "getting-started": "harmonyos-guides",
    "deveco-studio": "harmonyos-guides",
    "release-notes": "harmonyos-releases",
    "tools-faq": "harmonyos-faqs",
    "submission": "app",
    "ux-design": "design-guides",
    "advanced-features": "best-practices",
    "industry-solutions": "architecture-guides",
}

API_CAT = "harmonyos-references"
DOC_URL = "https://developer.huawei.com/consumer/{lang}/doc/{cat}/{slug}"


def url_for(lang: str, cat: str, slug: str) -> str:
    return DOC_URL.format(lang=lang, cat=cat, slug=slug)


def fetch_sitemap(lang: str, cache_dir, ttl: int = 7 * 86400) -> str:
    """取官网文档 sitemap（多分片拼起来），本地缓存 ttl 秒。"""
    p = Path(cache_dir) / f"sitemap_doc_{lang}.xml"
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        return p.read_text(encoding="utf-8", errors="ignore")
    text = ""
    for n in range(1, 6):
        req = urllib.request.Request(SITEMAP_TPL.format(lang=lang, n=n),
                                     headers={"User-Agent": "Mozilla/5.0"})
        try:
            body = urllib.request.urlopen(req, timeout=40).read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            break
        if "<loc>" not in body:
            break
        text += body
    if text:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return text


def _slug_cats(xml: str) -> dict[str, set]:
    out: dict[str, set] = collections.defaultdict(set)
    for u in re.findall(r"<loc>(.*?)</loc>", xml or ""):
        m = re.match(r"https://developer\.huawei\.com/consumer/\w+/doc/([^/]+)/([^/]+)$", u)
        if m:
            out[m.group(2)].add(m.group(1))
    return out


def resolve(conn, lang: str, cache_dir, purity: float = 0.9) -> dict[str, str]:
    """返回 {doc_key: catname}，覆盖该语言下所有 status='ok' 的文档。"""
    slug_cats = _slug_cats(fetch_sitemap(lang, cache_dir))

    docs = conn.execute(
        "SELECT doc_key, section, slug, is_api FROM docs WHERE lang=? AND status='ok'",
        (lang,)).fetchall()
    nodes = conn.execute(
        "SELECT node_id, parent, slug FROM nodes WHERE lang=?", (lang,)).fetchall()

    slug2node = {n["slug"]: n["node_id"] for n in nodes if n["slug"]}
    parent = {n["node_id"]: n["parent"] for n in nodes}

    # 1) sitemap 锚点
    anchored: dict[str, str] = {}
    for d in docs:
        cats = slug_cats.get(d["slug"])
        if cats and len(cats) == 1:
            anchored[d["doc_key"]] = next(iter(cats))

    # 2) 祖先节点投票表（用锚点文档累加）
    desc: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for d in docs:
        cat = anchored.get(d["doc_key"])
        if not cat:
            continue
        cur = slug2node.get(d["slug"])
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            desc[cur][cat] += 1
            cur = parent.get(cur)

    def predict(slug: str):
        cur = slug2node.get(slug)
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            cnt = desc.get(cur)
            if cnt:
                tot = sum(cnt.values())
                cat, n = cnt.most_common(1)[0]
                if tot and n / tot >= purity:
                    return cat
            cur = parent.get(cur)
        return None

    out: dict[str, str] = {}
    for d in docs:
        cat = anchored.get(d["doc_key"]) or predict(d["slug"])
        if not cat:
            cat = API_CAT if d["is_api"] else SECTION_CAT.get(d["section"], "harmonyos-guides")
        out[d["doc_key"]] = cat
    return out
