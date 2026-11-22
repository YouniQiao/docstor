"""图片检查：image.missing —— 正文引用的图片在官网上根本显示不出来。

判据都来自「官网页面实际怎么渲染」，不是「我们镜像抓没抓到」：

  code   图片语法被写进了围栏代码块 → 官网按代码原样显示 `![alt](figures/x.png)`
         （2026-11 实测：mindspore-guidelines-based-js / -native、中文 decorator 那篇）
  space  图片路径里有未编码的空格 → CommonMark 解析不了，整行漏成文本
         （实测 en/getting-started/ascf-development-guide）
  missing 引用的文件不存在（官网这张图本来就没了，或路径写错）
  html   HTML <img src> 指向的文件不存在
  empty  图片地址为空

围栏代码块必须按行判断：文档里存在「表格单元格内联 ``` 」这种写法，
用 `"```.*?```"` 整段正则匹配会把表格后面的正常图片误判成代码块。
"""
from __future__ import annotations

import bisect
import re

from .base import Issue, register

FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
IMG_MD = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
IMG_HTML = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
TITLE_RE = re.compile(r'^(.*?)\s+(["\'])(.*)\2\s*$')

KIND_TEXT = {
    "code": "图片写在代码块里，官网上只会显示 ![...](...) 文本：{t}",
    "space": "图片路径含未编码空格，markdown 解析不了，官网上显示为文本：{t}",
    "missing": "引用的图片不存在：{t}",
    "html": "HTML <img> 指向的图片不存在：{t}",
    "empty": "图片地址为空",
}


def _split_target(inner: str):
    """`![](path "标题")` → 路径。

    返回 None 表示「不是一张指向文件的图片」：跨行内容或含 HTML 标签
    （文档里 `![](` 换行接 <video> 标签，是视频转换的产物）。
    """
    if "\n" in inner or "<" in inner:
        return None
    s = inner.strip()
    if not s:
        return ""
    m = TITLE_RE.match(s)
    return (m.group(1) if m else s).strip()


def _is_img(target: str) -> bool:
    return target.split("?")[0].split("#")[0].strip().lower().endswith(IMG_EXT)


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """围栏代码块的字符区间（含围栏行），按行状态机识别，能容忍表格里的内联 ```。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    opened: str | None = None
    start = 0
    for line in text.splitlines(keepends=True):
        if opened is None:
            m = FENCE_OPEN.match(line)
            if m:
                opened, start = m.group(1), pos
        else:
            m = FENCE_CLOSE.match(line)
            if m and m.group(1)[0] == opened[0] and len(m.group(1)) >= len(opened):
                spans.append((start, pos + len(line)))
                opened = None
        pos += len(line)
    if opened is not None:            # 没有闭合：按到文末算
        spans.append((start, pos))
    return spans


def _line_of(newlines: list[int], offset: int) -> int:
    """字符偏移 → 行号（1 起）。"""
    return bisect.bisect_left(newlines, offset) + 1


@register("image.missing", label="图片缺失", severity="error",
          ai_hint="引用的图片取不到。先看官网页面本身能否显示：官网正常说明是镜像抓取的问题，文档不用改；"
                  "官网也不显示才是文档的问题，改法是修正相对路径（同目录 images/xxx.png）或换成官网可访问的图，"
                  "文件名不能凭空编造。",
          langs=("cn", "en"), default_visible=False)
def check_image_missing(doc, ctx):
    """返回图片引用不可用的位置（同篇同目标同类型只报一次）。"""
    out: list[Issue] = []
    text = ctx.text
    if not text or ("![" not in text and "<img" not in text.lower()):
        return out
    doc_dir = ctx.md_path.parent
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]
    spans = _code_ranges(text)
    starts = [s for s, _ in spans]
    seen: set[tuple[str, str]] = set()

    def span_of(off: int):
        i = bisect.bisect_right(starts, off) - 1
        return spans[i] if i >= 0 and spans[i][0] <= off < spans[i][1] else None

    def add(off: int, target: str, kind: str):
        key = (kind, target)
        if key in seen:
            return
        seen.add(key)
        line = _line_of(newlines, off)
        title, aid = ctx.anchor_for_line(line)
        out.append(Issue(
            rule="image.missing",
            message=KIND_TEXT[kind].format(t=target or "（空）"),
            severity="error",
            locator={"line": line, "target": target, "kind": kind},
            anchor_text=title,
            evidence={"anchorId": aid, "kind": kind, "target": target},
        ))

    for m in IMG_MD.finditer(text):
        target = _split_target(m.group(2))
        if target is None:
            continue
        _span = span_of(m.start())
        code = _span is not None
        if not target:
            if not code:
                add(m.start(), "", "empty")
            continue
        if not _is_img(target):
            continue
        if code:
            # 只认「代码块里除围栏外就只有这一行图片语法」的情况 —— 官网把 <pre> 里的
            # 图片语法原样显示成文本。代码块里夹着示例代码的，多半是我们转换器加围栏
            # 时留下的痕迹，官网其实是正常 <img>，不能报。
            inner = [ln.strip() for ln in text[_span[0]:_span[1]].splitlines()
                     if ln.strip() and not ln.strip().startswith(("```", "~~~"))]
            if len(inner) == 1 and inner[0] == m.group(0).strip():
                add(m.start(), target, "code")
            continue
        if target.startswith(("http://", "https://", "//", "data:")):
            continue                                # 外链图片归 link.external
        if " " in target or "\t" in target:
            add(m.start(), target, "space")         # markdown 解析不了
            continue
        if not (doc_dir / target.split("?")[0].split("#")[0].lstrip("./")).is_file():
            add(m.start(), target, "missing")

    for m in IMG_HTML.finditer(text):
        target = m.group(1).strip()
        if span_of(m.start()) is not None or not _is_img(target):
            continue
        if target.startswith(("http://", "https://", "//", "data:")):
            continue
        if not (doc_dir / target.split("?")[0].split("#")[0].lstrip("./")).is_file():
            add(m.start(), target, "html")

    return out
