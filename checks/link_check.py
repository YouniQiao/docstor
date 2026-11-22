"""链接检查：站内（本地路由）/ 锚点 / 历史版本 / 跨语言。"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import unquote
from pathlib import Path

import ds_catalog
import ds_db

from .base import Issue, register

# markdown 链接 + HTML a 标签（都带出链接文字，供问题列表原样展示）
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HTML_LINK_RE = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
INTERNAL_RE = re.compile(r"^/(cn|en)/([A-Za-z0-9_\-]+)/([A-Za-z0-9_.\-]+)(?:#(.*))?$")
# 相对路径的 *.md 链接（如 `arkts-apis-image-PixelMap.md#release7`），docsify 会拼成同板块路由
REL_MD_RE = re.compile(r"^(?!https?:|/|#|mailto:|javascript:)([^)\s]+?\.md)(#[^\s]*)?$")
# 历史版本文档的地址形如 /cn/harmonyos-guides-V5/xxx、/cn/harmonyos-releases-V2/…；
# 只看「某一段路径整体以 -V<数字> 结尾」，且大小写敏感——ArkTS 状态管理 V1/V2 的
# 文档名（arkts-mvvm-v2、arkts-state-management-v1）是小写，不是历史版本，别误报。
VINTAGE_RE = re.compile(r"(?:^|/)[A-Za-z0-9][A-Za-z0-9._-]*-V\d{1,3}(?=[/?#]|$)")
# 从地址里抠出具体版本号：…/doc/harmonyos-guides-V5/foo、…-V14、…-V5?x=1
VINTAGE_VER = re.compile(r"[-/]V(\d{1,3})(?=[/?#]|$)", re.I)
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
    if section in ds_db.SECTION_CAT:
        # 我们镜像的板块名 → 按文档实测目录还原（板块 ≠ 官网目录）
        url = ds_catalog.url_for(lang, section, slug) or ds_db.official_url(lang, section, slug)
    else:
        # 这一段本来就是官网目录名（harmonyos-guides-V5、design-guides-V1…），原样还它
        url = f"https://developer.huawei.com/consumer/{lang}/doc/{section}/{slug}"
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


_HEAD_CACHE: dict[tuple[str, str, str], set[str]] = {}


def _norm_anchor(s: str) -> str:
    """锚点归一：去掉大小写、空格、连字符、标点差异，中文标点也一并去掉。

    链接里写的是文档站渲染后的 slug（小写、空格转连字符、去标点），
    而 anchors_json 里是官网原始 anchorId（保留大小写和空格），
    直接比字符串会把「#通过appgallery-connect配置应用图标」这类全判成失效。
    """
    from urllib.parse import unquote
    s = s or ""
    for _ in range(2):            # 有的链接被编码两次（%25E5…）
        s = unquote(s)
    s = re.sub(r"<[^>]+>", "", s)  # 标题里的 <sup>11</sup> / <br>
    s = re.sub(r"\\(.)", r"\1", s)  # markdown 转义 \_
    s = s.lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)


def _heading_ids(ctx, lang: str, section: str, slug: str) -> set[str]:
    """目标文档里所有标题的归一化写法（含「标题原文」与「slug 化」两种）。"""
    key = (lang, section, slug)
    if key in _HEAD_CACHE:
        return _HEAD_CACHE[key]
    out: set[str] = set()
    p = _docs_root(ctx) / lang / section / f"{slug}.md"
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        _HEAD_CACHE[key] = out
        return out
    for line in text.splitlines():
        m = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not m:
            continue
        title = m.group(1)
        out.add(_norm_anchor(title))
        # 标题里的行内代码/加粗/链接会带符号，去掉后再来一份
        cleaned = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", title)
        out.add(_norm_anchor(re.sub(r"[`*_\[\]()]", "", cleaned)))
    _HEAD_CACHE[key] = out
    return out


def _anchor_exists(ctx, lang: str, section: str, slug: str, anchor: str) -> bool:
    idx = _anchor_index(ctx, lang, section)
    if idx is None:
        return True  # 缺锚点表时不误报
    want = _norm_anchor(anchor)
    if not want:
        return True
    # docsify 给重名标题加 -1/-2 后缀，带上也算能跳
    # 注意要从原始串剥后缀：归一化会把 - 去掉，'decode-1' 会变成 'decode1'
    base = _norm_anchor(re.sub(r"-\d+$", "", anchor.strip()))
    ids = {_norm_anchor(a) for a in idx.get(slug, set())}
    heads = _heading_ids(ctx, lang, section, slug)
    for w in (want, base):
        if w in ids or w in heads:
            return True
        # 标题带类名前缀（ScanCore.isDefaultScanSupported），链接只写方法名 → 后缀匹配
        # 方法名锚点很短（#start / #init / #stop），阈值放到 4 个字
        if any(h.endswith(w) for h in heads if len(w) >= 4):
            return True
    return False


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


@register("link.dead", label="链接失效",
          ai_hint="判断依据是官网页面上该链接是否真的 404；官网已下线的文档，"
                  "处理方式是改用当前有效路径，或把链接降级为纯文本并保留原文标题，不要凭空造 slug。")
def check_dead(doc, ctx):
    """站内链接是否指向官网不存在的文档。

    口径：**问官网**（内容接口 + 中文 sitemap），不看本地镜像有没有这篇。
    官网也查不到 = 真死链。官网查得到、只是镜像没存（镜像缺口）不再算问题；
    英文页链到只有中文版的文档归 link.cross_lang。
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
        if verdict in ("ok", "published"):
            # 官网有这篇、只是镜像没存（多半是官网目录树里没有的页面，抓取本来就不会收）
            # → 链接本身没问题，不再报「镜像缺这篇」（用户 2026-11 决定去掉这项检查）
            continue
        if verdict in ("moved", "renamed"):
            # moved：页面还在，只是正文换成了「内容已迁移」公告；renamed：旧地址自动跳新地址。
            # 两种点开都能用，不属于死链，也不再单列「链接已迁移」（用户 2026-11 决定去掉这项检查）
            continue
        if _render_says_ok(lang, section, slug):
            continue          # 接口查不到但页面能打开（AGC 老文档常见）→ 不算问题
        out.append(Issue("link.dead",
                         f"官网查不到该文档（链接已失效）：{target}",
                         locator=loc, anchor_text=title,
                         evidence={"target": target, "verdict": verdict or "unknown"}))
    return out


IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")


@register("link.external", label="外链失效",
          ai_hint="外链指向站外资源（GitHub / Gitee / 第三方站点），失效多半是仓库改名、分支变更或站点下线。"
                  "改法是换成当前有效地址；找不到就降级为纯文本并说明来源，不要指向镜像或无关页面。")
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


# 官网正文里由编辑器自动生成的 id（section123、li456、ZH-CN_TOPIC_0000001__xxx），
# markdown 里不存在这种 id，本地无法核实，不能当失效报
AUTO_ID_RE = re.compile(
    r"^(?:[a-z]{2}[-_][a-z]{2}_topic_\d+|section\d{3,}|(?:li|ul|p|div|span|table|img|h[1-6])\d{3,})",
    re.I)
# AGC / UX 这类老页面的小节 id 就是 section+编号，页面改版会重排 → 这类锚点会烂掉。
# 过去一律当「自动生成、无法核实」跳过；现在只要目标页锚点清单里本来就有这种编号，就能精确比对。
_SECTION_ID_RE = re.compile(r"^section\d{3,}$", re.I)


def _page_section_ids(ctx, lang: str, section: str, slug: str) -> set[str]:
    """目标页锚点清单里 section12345 这种编号型 id（页面不用这套编号就返回空集）。"""
    idx = _anchor_index(ctx, lang, section)
    if not idx:
        return set()
    return {a for a in (idx.get(slug) or ()) if _SECTION_ID_RE.match(str(a).strip())}


@register("link.anchor", label="锚点失效",
          ai_hint="锚点来源于目标文档的小标题（GitHub 风格：小写、空格转连字符、去标点）。"
                  "改法是让 #锚点 与目标文档真实存在的标题一致；目标文档没有对应标题时，"
                  "改指最近的小节或去掉锚点，不要自己造一个标题 id。")
def check_anchor(doc, ctx):
    out = []
    seen = set()
    for line_no, target, text, _html in iter_links(ctx.text):
        m = INTERNAL_RE.match(target or "")
        if not m:
            continue
        lang, section, slug, frag = m.groups()
        if not frag or not _section_ready(ctx, lang, section):
            continue
        if _target_exists(ctx, lang, section, slug) and not _anchor_exists(
                ctx, lang, section, slug, frag):
            frag_u = unquote(frag or "").strip()
            if AUTO_ID_RE.match(frag_u):
                # 编号型锚点（section12345）：目标页确实用这套编号才能比对，否则仍是无法核实
                if not (_SECTION_ID_RE.match(frag_u) and _page_section_ids(ctx, lang, section, slug)):
                    continue
            if (target, frag) in seen:        # 同一篇里同一个锚点只报一条
                continue
            seen.add((target, frag))
            title, aid = ctx.anchor_for_line(line_no)
            out.append(Issue("link.anchor",
                             f"锚点在目标文档中不存在：{target}",
                             locator={"line": line_no, "target": target, "anchorId": aid,
                                      "text": text, "href": abs_href(target)},
                             anchor_text=title, evidence={"target": target, "fragment": frag}))
    return out


@register("link.vintage", label="指向历史版本",
          ai_hint="链接指向了历史版本目录（如带 3.x/4.x 等旧版本号），应改到当前版本的同名路径；"
                  "确实需要引用旧版行为时，保留并加一句版本说明。")
def check_vintage(doc, ctx):
    out = []
    for line_no, target, text, _html in iter_links(ctx.text):
        if not target or not VINTAGE_RE.search(target):
            continue
        title, aid = ctx.anchor_for_line(line_no)
        m = VINTAGE_VER.search(target)
        ver = f"V{m.group(1)}" if m else ""
        head = f"链接指向历史版本 {ver}" if ver else "链接指向历史版本"
        out.append(Issue("link.vintage", f"{head}：{target}",
                         locator={"line": line_no, "target": target, "anchorId": aid,
                                  "text": text, "href": abs_href(target), "version": ver},
                         anchor_text=title, evidence={"target": target, "version": ver}))
    return out


# 代码仓库地址：路径/锚点里的中文是仓库内容，不属于「链到中文文档站」
REPO_HOST_RE = re.compile(r"^https?://(?:[^/]+\.)?(?:gitcode|github|gitee)\.(?:com|io)/", re.I)


@register("link.cross_lang", label="英文文档链到中文", langs=("en",),
          ai_hint="英文文档里的链接应指向 /en/ 下的同篇文档，slug 通常与中文一致；"
                  "英文站确实没有对应文档时，才保留中文链接并在文字上说明。")
def check_cross_lang(doc, ctx):
    """英文文档里链到中文站的内容。

    口径：
      · 只看「目标文档本身在中文站」（/cn/… 或 /consumer/cn/doc/…）；
      · 代码仓库（gitcode/github/gitee）里带中文锚点不算，那是仓库 README 的锚点；
      · 目标是 /en/ 只有锚点含中文 → 不是跨语言，归 link.anchor。
    """
    out = []
    for line_no, target, _text, _html in iter_links(ctx.text):
        if not target or REPO_HOST_RE.match(target):
            continue
        reason = ""
        m = INTERNAL_RE.match(target)
        if m:
            lang, section, slug, _frag = m.groups()
            if lang != "cn":
                continue
            if _target_exists(ctx, "en", section, slug):
                reason = "链接指向中文站（英文站有同一篇，应改指 /en/…）"
            else:
                reason = "链接指向中文站（英文站没有这篇）"
        elif "/consumer/cn/doc/" in target:
            mm = re.search(r"/consumer/cn/doc/([^/?#]+)/([^/?#]+)", target)
            cat, slug = (mm.group(1), mm.group(2)) if mm else ("", "")
            section = _section_of(cat, "cn") if cat else ""
            off = _official()
            v = off.verdict("en", section, slug) if (off and slug) else ""
            reason = ("链接指向中文站（英文站有同一篇，应改指 /en/…）" if v == "ok"
                      else "链接指向中文站（英文站没有这篇）")
        elif re.match(r"^https?://developer\.huawei\.com/consumer/cn/", target):
            reason = "链接指向中文站页面（非文档，如论坛/下载/活动页）"
        elif target.startswith(("http", "/")) and CN_CHAR_RE.search(target.split("#")[0]):
            reason = "链接 URL 含中文字符"
        if not reason:
            continue
        title, aid = ctx.anchor_for_line(line_no)
        out.append(Issue("link.cross_lang", f"{reason}：{target}",
                         locator={"line": line_no, "target": target, "anchorId": aid},
                         anchor_text=title, evidence={"target": target}))
    return out
