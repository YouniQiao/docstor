"""链接检查：站内（本地路由）/ 锚点 / 历史版本 / 跨语言。"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import ds_db

from .base import Issue, register

# markdown 链接 + HTML a 标签（都带出链接文字，供问题列表原样展示）
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_LINK_RE = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
INTERNAL_RE = re.compile(r"^/(cn|en)/([A-Za-z0-9_\-]+)/([A-Za-z0-9_.\-]+)(?:#(.*))?$")
# 相对路径的 *.md 链接（如 `arkts-apis-image-PixelMap.md#release7`），docsify 会拼成同板块路由
REL_MD_RE = re.compile(r"^(?!https?:|/|#|mailto:|javascript:)([^)\s]+?\.md)(#[^\s]*)?$")
VINTAGE_RE = re.compile(r"-V\d{1,3}(?:/|$)|/doc/harmonyos-[a-z]+-V\d", re.I)
# 绝对地址指向官网文档中心的链接：SPA 对不存在的页面同样回 200，必须走内容接口
# 文档里的占位地址：IANA 保留域名、本机地址——点开就是示例页，不算死链
PLACEHOLDER_RE = re.compile(
    r"^https?://(?:[^/]+\.)?(?:example\.com|example\.org|example\.net|localhost|127\.0\.0\.1)(?:[:/]|$)",
    re.I)
OFFICIAL_RE = re.compile(
    r"^https?://developer\.huawei\.com/consumer/(cn|en)/doc/([A-Za-z0-9_\-]+)/([A-Za-z0-9_.\-]+?)(?:[#?].*)?$")
CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

LINE_SKIP = ("```",)


_RENDER_SEM = None


def _url_render_ok(url: str) -> bool:
    """外链 HTTP 404 时再真渲染一次：有些站点对任何路径都回 404，正文靠 JS 渲染。"""
    global _RENDER_SEM
    try:
        import threading

        import ds_render
        if _RENDER_SEM is None:
            _RENDER_SEM = threading.Semaphore(2)
        with _RENDER_SEM:
            return ds_render.check_url(url) == "ok"
    except Exception:      # noqa: BLE001
        return False


def _render_says_ok(lang: str, section: str, slug: str) -> bool:
    """接口判死后最后一道关：无头渲染一次，页面真能打开就不算死链。

    AGC/AppGallery 这类老文档 <title> 常停在「文档中心」，只能看 h1 + 正文长度；
    ds_render 里已按新判据实现，并缓存 30 天。渲染超时按「说不清」处理，仍判死。
    """
    global _RENDER_SEM
    try:
        import threading

        import ds_render
        if _RENDER_SEM is None:
            _RENDER_SEM = threading.Semaphore(2)
        with _RENDER_SEM:
            return ds_render.check(lang, section, slug) == "ok"
    except Exception:                     # noqa: BLE001
        return False


def iter_links(text: str):
    """产出 (行号, 链接目标, 链接文字, 是否 html)。跳过代码块里的内容。"""
    in_code = False
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for m in MD_LINK_RE.finditer(line):
            yield i, m.group(2), _plain(m.group(1)), False
        for m in HTML_LINK_RE.finditer(line):
            yield i, m.group(1), _plain(re.sub(r"<[^>]+>", "", m.group(2) or "")), True


def _plain(s: str) -> str:
    """链接文字去格式/压缩空白，截断过长文本。"""
    s = re.sub(r"[*_`~]", "", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:80]


def abs_href(target: str) -> str:
    """站内路由 → 官网原文地址（保留锚点）；外链原样返回。"""
    if not target:
        return ""
    if target.startswith(("http://", "https://")):
        return target
    m = INTERNAL_RE.match(target)
    if not m:
        return ""
    lang, section, slug, frag = m.groups()
    url = ds_db.official_url(lang, section, slug)
    return f"{url}#{frag}" if frag else url


def _docs_root(ctx) -> Path:
    return Path(ctx.md_path).parent.parent.parent  # site/docs


def _section_of(cat: str, lang: str) -> str:
    """官网目录名 → 我们镜像里的板块名（查不到就用目录名本身）。"""
    try:
        import ds_alias
        return ds_alias.cat_section(cat, cat) or cat
    except Exception:      # noqa: BLE001
        return cat


def _section_ready(ctx, lang: str, section: str) -> bool:
    """目标板块是否已经镜像（未镜像的板块，站内链接无从校验，跳过不误报）。"""
    ready = getattr(ctx, "sections", None)
    if ready is None:
        return True
    return (lang, section) in ready


def _target_exists(ctx, lang: str, section: str, slug: str) -> bool:
    p = _docs_root(ctx) / lang / section / f"{slug}.md"
    if p.exists():
        return True
    # 官网同一篇文档可能挂在多个板块下：任一已镜像板块命中即视为有效
    root = _docs_root(ctx)
    ready = getattr(ctx, "sections", set()) or set()
    for (l, s) in ready:
        if l != lang or s == section:
            continue
        if (root / l / s / f"{slug}.md").exists():
            return True
    return False


_ANCHOR_CACHE: dict[tuple[str, str], dict[str, set[str]]] = {}


def _anchor_index(ctx, lang: str, section: str) -> dict[str, set[str]] | None:
    """板块级锚点索引：{slug: {anchorId...}}（进程内缓存，避免反复解析大 JSON）。"""
    key = (lang, section)
    if key in _ANCHOR_CACHE:
        return _ANCHOR_CACHE[key]
    p = _docs_root(ctx) / lang / section / "_anchors.json"
    if not p.exists():
        _ANCHOR_CACHE[key] = {}
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _ANCHOR_CACHE[key] = {}
        return None
    idx = {slug: {a.get("anchorId") for a in (items or []) if a.get("anchorId")}
           for slug, items in data.items()}
    _ANCHOR_CACHE[key] = idx
    return idx


def _anchor_exists(ctx, lang: str, section: str, slug: str, anchor: str) -> bool:
    idx = _anchor_index(ctx, lang, section)
    if idx is None:
        return True  # 缺锚点表时不误报
    return anchor in idx.get(slug, set())


_OFFICIAL = None


def _official():
    """官网存在性判定器，按进程各持一份（ds_check 是多线程/多进程跑的）。"""
    global _OFFICIAL
    pid = os.getpid()
    if _OFFICIAL and _OFFICIAL[0] == pid:
        return _OFFICIAL[1]
    try:
        from ds_official import Official
        obj = Official()
    except Exception:  # noqa: BLE001
        obj = None
    _OFFICIAL = (pid, obj)
    return obj


@register("link.dead", label="链接失效")
def check_dead(doc, ctx):
    """站内链接是否指向官网不存在的文档。

    口径：**问官网**（内容接口 + 中文 sitemap），不看本地镜像有没有这篇。
    官网也查不到 = 真死链；官网有、本地没镜像 = 镜像缺口（link.mirror_missing）；
    英文页链到只有中文版的文档 = 英文缺译（link.no_translation）。
    """
    out = []
    seen = set()
    for line_no, target, text, _html in iter_links(ctx.text):
        if not target or target.startswith(("#", "mailto:", "javascript:")):
            continue
        m = INTERNAL_RE.match(target)
        if m:
            lang, section, slug, _frag = m.groups()
            if not _section_ready(ctx, lang, section) and _target_exists(ctx, lang, section, slug):
                continue      # 旧板块名（multi-device…），但镜像里同一篇能打开 → 不算问题
            # 板块没镜像、镜像里也没有 → 继续问官网（legacy 板块名合计 1.6 万条这类链接）
        else:
            rm = REL_MD_RE.match(target)
            if not rm:
                continue
            rel, frag = rm.group(1), (rm.group(2) or "").lstrip("#")
            if (ctx.md_path.parent / rel).exists():
                continue
            lang, section = doc["lang"], doc["section"]
            slug = rel.rsplit("/", 1)[-1][:-3]
            target = f"/{lang}/{section}/{slug}" + (f"#{frag}" if frag else "")
        key = (lang, section, slug)
        if key in seen:      # 同篇同一目标（含不同锚点）只报一次
            continue
        seen.add(key)
        if _target_exists(ctx, lang, section, slug):
            continue          # 镜像里有 = 官网肯定有，不必打接口
        off = _official()
        verdict = off.verdict(lang, section, slug) if off else None
        title, aid = ctx.anchor_for_line(line_no)
        loc = {"line": line_no, "target": target, "anchorId": aid,
               "text": text, "href": abs_href(target)}
        if verdict == "ok":
            if _section_ready(ctx, lang, section) and not _target_exists(ctx, lang, section, slug):
                out.append(Issue("link.mirror_missing",
                                 f"官网有这篇但镜像里没有：{target}",
                                 severity="info", locator=loc, anchor_text=title,
                                 evidence={"target": target, "verdict": verdict}))
            continue
        if verdict == "published" and _section_ready(ctx, lang, section):
            out.append(Issue("link.mirror_missing",
                             f"官网已发布但镜像里没有（不在目录树）：{target}",
                             severity="info", locator=loc, anchor_text=title,
                             evidence={"target": target, "verdict": verdict}))
            continue
        if verdict == "moved":
            new_url = off.detail(lang, section, slug) if off else ""
            out.append(Issue("link.moved",
                             f"官网页面已是迁移公告，建议改为：{new_url or '（原页面指向的新地址）'}",
                             severity="warn", locator=loc, anchor_text=title,
                             evidence={"target": target, "verdict": verdict, "suggestion": new_url}))
            continue
        if verdict == "renamed":
            continue          # 旧地址会自动跳到新地址，链接能打开 → 不算问题
        if _render_says_ok(lang, section, slug):
            continue          # 接口查不到但页面能打开（AGC 老文档常见）→ 不算问题
        out.append(Issue("link.dead",
                         f"官网查不到该文档（链接已失效）：{target}",
                         locator=loc, anchor_text=title,
                         evidence={"target": target, "verdict": verdict or "unknown"}))
    return out


IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")


@register("link.external", label="外链失效")
def check_external(doc, ctx):
    """正文里的第三方 http(s) 链接：404/410 报失效，403/超时只报不可达。"""
    import ds_http

    out = []
    seen = set()
    for line_no, target, text, _html in iter_links(ctx.text):
        if not target or not target.startswith(("http://", "https://")):
            continue
        if target.split("#")[0].lower().endswith(IMG_EXT):
            continue
        url = target.split("#")[0]
        if url in seen:                     # 同篇同链接只报一次
            continue
        seen.add(url)
        title, aid = ctx.anchor_for_line(line_no)
        loc = {"line": line_no, "target": target, "anchorId": aid,
               "text": text, "href": abs_href(target)}
        if PLACEHOLDER_RE.match(url):
            continue                        # 文档里的占位域名（example.com 等），不是真链接
        m = OFFICIAL_RE.match(url)
        if m:                               # 官网文档中心的绝对地址：按内容接口判，不看 HTTP 码
            lang, cat, slug = m.groups()
            if slug.endswith(".md"):
                slug = slug[:-3]
            if slug.endswith(".html"):
                slug = slug[:-5]
            section = _section_of(cat, lang)
            off = _official()
            v = off.verdict(lang, section, slug) if off else "ok"
            if v in ("dead", "cn_only") and not _render_says_ok(lang, section, slug):
                out.append(Issue("link.external", f"外链失效（官网已无此文档）：{url}",
                                 severity="error", locator=loc, anchor_text=title,
                                 evidence={"target": url, "status": 0, "verdict": v}))
            continue
        try:
            st, _final, err = ds_http.status(url)
        except Exception:  # noqa: BLE001
            continue
        if ds_http.verdict(st, err) != "dead":
            continue                        # 反爬/需登录/临时故障一律不报
        if st in (404, 410) and _url_render_ok(url):
            continue                        # 伪 404：站点对任何路径都回 404，页面其实能开
        out.append(Issue("link.external", f"外链失效（HTTP {st}）：{url}",
                         severity="error", locator=loc, anchor_text=title,
                         evidence={"target": url, "status": st, "err": err}))
    return out


@register("link.moved", label="链接已迁移")
def check_link_moved(doc, ctx):
    """占位：官网页面已是「内容已迁移」公告，由 check_dead 产出。"""
    return []


@register("link.mirror_missing", label="镜像缺这篇")
def check_mirror_missing(doc, ctx):
    """占位：结论由 check_dead 统一产出，这里只登记标签/严重级。"""
    return []


@register("link.anchor", label="锚点失效")
def check_anchor(doc, ctx):
    out = []
    for line_no, target, text, _html in iter_links(ctx.text):
        m = INTERNAL_RE.match(target or "")
        if not m:
            continue
        lang, section, slug, frag = m.groups()
        if not frag or not _section_ready(ctx, lang, section):
            continue
        if _target_exists(ctx, lang, section, slug) and not _anchor_exists(
                ctx, lang, section, slug, frag):
            title, aid = ctx.anchor_for_line(line_no)
            out.append(Issue("link.anchor",
                             f"锚点在目标文档中不存在：{target}",
                             locator={"line": line_no, "target": target, "anchorId": aid,
                                      "text": text, "href": abs_href(target)},
                             anchor_text=title, evidence={"target": target, "fragment": frag}))
    return out


@register("link.vintage", label="指向历史版本")
def check_vintage(doc, ctx):
    out = []
    for line_no, target, text, _html in iter_links(ctx.text):
        if not target or not VINTAGE_RE.search(target):
            continue
        title, aid = ctx.anchor_for_line(line_no)
        out.append(Issue("link.vintage", f"链接指向历史版本文档：{target}",
                         locator={"line": line_no, "target": target, "anchorId": aid,
                                  "text": text, "href": abs_href(target)},
                         anchor_text=title, evidence={"target": target}))
    return out


@register("link.cross_lang", label="英文文档链到中文", langs=("en",))
def check_cross_lang(doc, ctx):
    out = []
    for line_no, target, _html in iter_links(ctx.text):
        if not target:
            continue
        bad = False
        reason = ""
        if "/consumer/cn/doc/" in target or re.match(r"^/cn/", target):
            bad, reason = True, "链接指向中文站"
        elif CN_CHAR_RE.search(target) and target.startswith(("http", "/")):
            bad, reason = True, "链接 URL 含中文字符"
        if bad:
            title, aid = ctx.anchor_for_line(line_no)
            out.append(Issue("link.cross_lang", f"{reason}：{target}",
                             locator={"line": line_no, "target": target, "anchorId": aid},
                             anchor_text=title, evidence={"target": target}))
    return out
