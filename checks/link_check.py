"""链接检查：站内（本地路由）/ 锚点 / 历史版本 / 跨语言。"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .base import Issue, register

# markdown 链接 + HTML a 标签
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_LINK_RE = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"']", re.I)
INTERNAL_RE = re.compile(r"^/(cn|en)/([A-Za-z0-9_\-]+)/([A-Za-z0-9_.\-]+)(?:#(.+))?$")
VINTAGE_RE = re.compile(r"-V\d{1,3}(?:/|$)|/doc/harmonyos-[a-z]+-V\d", re.I)
CN_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")

LINE_SKIP = ("```",)


def iter_links(text: str):
    """产出 (行号, 链接目标, 是否 html)。跳过代码块里的内容。"""
    in_code = False
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        for m in MD_LINK_RE.finditer(line):
            yield i, m.group(1), False
        for m in HTML_LINK_RE.finditer(line):
            yield i, m.group(1), True


def _docs_root(ctx) -> Path:
    return Path(ctx.md_path).parent.parent.parent  # site/docs


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


@register("link.dead", label="链接失效")
def check_dead(doc, ctx):
    out = []
    for line_no, target, _html in iter_links(ctx.text):
        if not target or target.startswith(("#", "mailto:", "javascript:")):
            continue
        m = INTERNAL_RE.match(target)
        if m:
            lang, section, slug, _frag = m.groups()
            if not _section_ready(ctx, lang, section):
                continue
            if not _target_exists(ctx, lang, section, slug):
                title, aid = ctx.anchor_for_line(line_no)
                out.append(Issue("link.dead", f"站内链接指向不存在的文档：{target}",
                                 locator={"line": line_no, "target": target, "anchorId": aid},
                                 anchor_text=title, evidence={"target": target}))
        elif target.startswith(("http://", "https://")):
            if not getattr(ctx, "external", False):
                continue
            st = ctx.check_url(target)
            if st in (404, 410) or st == 0:
                title, aid = ctx.anchor_for_line(line_no)
                out.append(Issue("link.dead", f"外链失效（HTTP {st or '不可达'}）：{target}",
                                 severity="error",
                                 locator={"line": line_no, "target": target, "anchorId": aid},
                                 anchor_text=title, evidence={"target": target, "status": st}))
    return out


@register("link.anchor", label="锚点失效")
def check_anchor(doc, ctx):
    out = []
    for line_no, target, _html in iter_links(ctx.text):
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
                             locator={"line": line_no, "target": target, "anchorId": aid},
                             anchor_text=title, evidence={"target": target, "fragment": frag}))
    return out


@register("link.vintage", label="指向历史版本")
def check_vintage(doc, ctx):
    out = []
    for line_no, target, _html in iter_links(ctx.text):
        if not target or not VINTAGE_RE.search(target):
            continue
        title, aid = ctx.anchor_for_line(line_no)
        out.append(Issue("link.vintage", f"链接指向历史版本文档：{target}",
                         locator={"line": line_no, "target": target, "anchorId": aid},
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
