"""专项检查：名称 / 术语规范类。

special.agc_name —— AGC 名称排查
  找出正文里出现下面任一写法的页面：
    · AppGallery Connect（含 APPGallery Connect、App Gallery Connect 这类大小写/空格变体）
    · AGConnect
    · AGC（整词，不匹配包名里更长的词）
  口径：只看「正文」。代码块、行内代码、链接地址、HTML 标签、裸 URL 都会先剔掉，
  因为 `com.huawei.agconnect`、`agc-help-xxx` 这类是产品标识/文档 id，改了反而错。
  音频语境（自动增益控制 / Gain Control / AEC / ANC 等）里的 AGC 不算。
  每篇页面报一条，证据里带命中行号和原文片段，方便逐条核对。
"""
from __future__ import annotations

import re

from .base import Issue, register

# 边界：标识符/文件名/路径里的字符。`agconnect-services.json`、`com.huawei.agconnect`、
# `agc-help-xxx`、`AGC3.0` 这类是产品标识，不算正文中的名称写法。
IDENT = r"[A-Za-z0-9_.\-/@]"

# 顺序有讲究：先长后短，AGC 用词边界避免和 AGConnect / 更长的英文词撞车
TERMS = [
    ("AppGallery Connect", re.compile(rf"(?<!{IDENT})app\s*gallery\s*connect(?!{IDENT})", re.I)),
    ("AGConnect", re.compile(rf"(?<!{IDENT})ag\s*connect(?!{IDENT})", re.I)),
    ("AGC", re.compile(rf"(?<!{IDENT})agc(?!{IDENT})", re.I)),
]

INLINE_CODE = re.compile(r"`[^`\n]*`")

# 音频语境里的 AGC = 自动增益控制（Automatic Gain Control），不是 AppGallery Connect。
# 只认强信号词，避免误伤「AppGallery Connect 音频服务」这类正常正文。
AUDIO_HINT = re.compile(
    r"自动增益|增益控制|增益自动|Gain Control|Automatic Gain|"
    r"(?<![A-Za-z])AEC(?![A-Za-z])|(?<![A-Za-z])ANC(?![A-Za-z])|回声消除|噪声抑制|降噪", re.I)
HTML_COMMENT = re.compile(r"<!--.*?-->")
URL = re.compile(r"(?:https?|ftp)://[^\s)>\]]+")
EMAIL = re.compile(r"[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}")   # agconnect@huawei.com 这类联系邮箱
LINK_DEST = re.compile(r"\]\([^)\s]*\)")     # 去掉链接地址，保留链接文字
HTML_TAG = re.compile(r"<[^>]+>")

MAX_HITS = 40


@register("special.agc_name", label="AGC名称排查", severity="warn", default_visible=False,
          ai_hint=("AppGallery Connect / AGConnect / AGC 是同一事物的旧称，现已统一改称「HarmonyOS 开发者中心」。"
                   "正文中的这些名称应改为「HarmonyOS 开发者中心」；"
                   "但包名（com.huawei.agconnect）、文件名（agconnect-services.json）、"
                   "文档 id（agc-help-xxx）、域名与邮箱等标识符不能改。"
                   "音频语境里的 AGC（自动增益控制）不在此列。"))
def check_agc_name(doc, ctx):
    hits: list[dict] = []
    in_fence = False
    for line_no, line in enumerate(ctx.text.splitlines(), 1):
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence or not s:
            continue
        t = INLINE_CODE.sub(" ", line)
        t = HTML_COMMENT.sub(" ", t)
        t = URL.sub(" ", t)
        t = EMAIL.sub(" ", t)
        t = LINK_DEST.sub("] ", t)
        t = HTML_TAG.sub(" ", t)
        audio = AUDIO_HINT.search(t) if "agc" in t.lower() else None
        for name, rx in TERMS:
            if name == "AGC" and audio:
                continue          # 这一行讲的是音频增益，不是 AppGallery Connect
            for _m in rx.finditer(t):
                hits.append({"term": name, "line": line_no, "text": s[:160]})
    if not hits:
        return []

    counts: dict[str, int] = {}
    for h in hits:
        counts[h["term"]] = counts.get(h["term"], 0) + 1
    terms = sorted(counts)

    # fprint 由 (doc_key, rule, locator, message) 决定，这里都不含条数/行号，
    # 所以措辞变了才重新记一条，不会每天因为「命中 3 处变 4 处」反复新增/解决。
    return [Issue(
        rule="special.agc_name",
        message="页面出现 AGC 相关名称（应统一改为「HarmonyOS 开发者中心」）：" + "、".join(terms),
        severity="warn",
        locator={"terms": terms},
        anchor_text="",
        evidence={"counts": counts, "hits": hits[:MAX_HITS], "total_hits": len(hits)},
    )]
