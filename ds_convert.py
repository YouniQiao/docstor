"""HTML → Markdown 转换 + 图片/动图本地化。

沿用 docscheck 踩过的坑：
  · HTML 实体 &amp; 必须先 unescape，否则 CDN 签名参数错 → 403
  · markitdown 必须显式声明 charset="utf-8"，否则偶发整篇乱码
  · 图片文件名用「去掉签名参数后的 URL」做 md5，签名变化也能去重

docstor 新增：
  · 动图（GIF）转 MP4（默认 crf 23），md 里改用 <video autoplay loop muted>
    实测 11.6MB GIF → 1.28MB MP4（省 89%）
  · 返回 anchorList，供 docsify 注入官方锚点 id
"""
from __future__ import annotations

import hashlib
import html as html_mod
import io
import os
import re
import subprocess
import tempfile
from pathlib import Path

import requests
from markitdown import MarkItDown, StreamInfo

IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

EXT_MAP = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
           "image/svg+xml": "svg", "image/webp": "webp", "image/bmp": "bmp"}

SITE_BASE = "https://developer.huawei.com"
_md = MarkItDown()


def _resolve_url(src: str) -> str:
    src = html_mod.unescape(src.strip())
    if src.startswith(("http://", "https://")):
        return src
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return SITE_BASE + src
    return SITE_BASE + "/" + src


def _ext(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in EXT_MAP:
        return EXT_MAP[ct]
    path = url.split("?")[0].lower()
    for e in ("png", "jpg", "jpeg", "gif", "svg", "webp", "bmp"):
        if path.endswith("." + e):
            return "jpg" if e == "jpeg" else e
    return "png"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# 华为 CDN 的图片/视频/下载链接带时效签名，每次抓取都会变：
#   图片/视频：?HW-CC-KV=V1&HW-CC-Date=20261116T143942Z&HW-CC-Expire=…&HW-CC-Sign=…
#   下载链接：:20261117223942:2800:649F096C…
# 直接对 md 做 md5 会让「没改过的文档」每次都算变更（实测单板块 24 篇假修改），
# 判据一律用剔除签名后的 stable_hash。
_DYN_QUERY_RE = re.compile(r"[?&]HW-CC-[A-Za-z]+=[^&\s)\"']*")
_DL_TOKEN_RE = re.compile(r":\d{14}:\d+:[A-F0-9]{40,}")


def stable_hash(text: str) -> str:
    """剔除时效签名后的内容哈希（判「文档有没有改」用这个）。"""
    t = _DYN_QUERY_RE.sub("", text)
    t = _DL_TOKEN_RE.sub("", t)
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def gif_to_mp4(src: Path, dst: Path, crf: int = 23, preset: str = "veryfast") -> bool:
    """GIF → H.264 MP4。成功返回 True。"""
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
           "-movflags", "faststart", "-pix_fmt", "yuv420p",
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
           "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
           "-an", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False


VIDEO_TAG = ('<video class="ds-video" src="{src}" autoplay loop muted '
             'playsinline controls preload="metadata"></video>')


def convert(html: str, images_dir: Path, session: requests.Session,
            gif_to_video: bool = True, crf: int = 23, preset: str = "veryfast",
            gif_min_bytes: int = 40960) -> dict:
    """转换一篇文档。

    返回 {markdown, images, videos, failed, bytes}
    """
    raw_srcs = IMG_RE.findall(html)
    seen, srcs = set(), []
    for s in raw_srcs:
        s = html_mod.unescape(s.strip())
        if s and s not in seen:
            seen.add(s)
            srcs.append(s)

    result = _md.convert_stream(
        io.BytesIO(html.encode("utf-8")),
        stream_info=StreamInfo(mimetype="text/html", extension=".html", charset="utf-8"),
    )
    text = result.text_content
    # markitdown 会把部分 <h2>/<h3> 原样带出成「[h2]标题」，在标题行上去掉这个标记
    text = re.sub(r"^(#{1,6}\s+)\[h([1-6])\]\s*", r"\1", text, flags=re.M)

    images_dir.mkdir(parents=True, exist_ok=True)
    local_map: dict[str, str] = {}
    videos = 0
    images = 0
    failed = 0
    total_bytes = 0

    for src in srcs:
        full = _resolve_url(src)
        base_url = full.split("?")[0]
        stem = _md5(base_url)

        # 已存在（任意扩展名）直接复用
        existing = None
        for ext in ("png", "jpg", "gif", "svg", "webp", "bmp", "mp4"):
            p = images_dir / f"{stem}.{ext}"
            if p.exists():
                existing = (p, ext)
                break
        if existing:
            p, ext = existing
            if ext == "mp4":
                local_map[src] = VIDEO_TAG.format(src=f"images/{p.name}")
                videos += 1
            else:
                local_map[src] = f"images/{p.name}"
                images += 1
            continue

        try:
            r = session.get(full, timeout=40)
            if r.status_code != 200 or not r.content:
                failed += 1
                continue
            ext = _ext(r.headers.get("Content-Type", ""), full)
            p = images_dir / f"{stem}.{ext}"
            p.write_bytes(r.content)
            total_bytes += len(r.content)

            if ext == "gif" and gif_to_video and len(r.content) >= gif_min_bytes:
                mp4 = images_dir / f"{stem}.mp4"
                if gif_to_mp4(p, mp4, crf, preset):
                    try:
                        p.unlink()
                    except OSError:
                        pass
                    local_map[src] = VIDEO_TAG.format(src=f"images/{mp4.name}")
                    videos += 1
                    total_bytes = total_bytes - len(r.content) + mp4.stat().st_size
                    continue
            local_map[src] = f"images/{p.name}"
            images += 1
        except Exception:  # noqa: BLE001
            failed += 1
            continue

    if local_map:
        for orig, local in local_map.items():
            text = text.replace(orig, local)
            resolved = _resolve_url(orig)
            if resolved != orig:
                text = text.replace(resolved, local)

    # <video> 是块级 HTML，前后需要空行，否则被当成行内 HTML 粘在段落里
    text = re.sub(r'(?<!\n)(<video class="ds-video")', r"\n\n\1", text)
    text = text.replace("</video>", "</video>\n")

    text = rewrite_links(text)

    return {"markdown": text, "images": images, "videos": videos,
            "failed": failed, "bytes": total_bytes}


# 官网 catname → docstor 板块 alias
CAT_ALIAS = {
    "doccenter-getting-started": "getting-started",
    "doccenter-capabilities": "capabilities",
    "doccenter-advanced-features": "advanced-features",
    "doccenter-tools-faq": "tools-faq",
    "doccenter-dev-faq": "tools-faq",
    "doccenter-release-notes": "release-notes",
    "doccenter-deveco-studio": "deveco-studio",
    "doccenter-ux-design": "ux-design",
    "doccenter-submission": "submission",
    "doccenter-industry-solutions": "industry-solutions",
    # API 参考在新结构里并到 capabilities 板块
    "doccenter-references": "capabilities",
    "doccenter-doccenter": "capabilities",
    # 旧站 catalog 名（历史文档里仍可能出现）
    "harmonyos-guides": "capabilities",
    "harmonyos-references": "capabilities",
    "harmonyos-faqs": "tools-faq",
    "harmonyos-releases": "release-notes",
    "best-practices": "advanced-features",
}

_DOC_LINK_RE = re.compile(
    r"(?:https?://developer\.huawei\.com)?/consumer/(cn|en)/doc/"
    r"([A-Za-z0-9_\-]+)/(?:api/)?([A-Za-z0-9_.\-]+)(#[^\s\)\]]*)?")


def rewrite_links(md: str) -> str:
    """把官网站内链接改写为 docstor 路由（/cn/<section>/<slug>#anchor）。"""
    def rep(m):
        lang, cat, slug, frag = m.group(1), m.group(2), m.group(3), m.group(4) or ""
        alias = CAT_ALIAS.get(cat, cat.replace("doccenter-", ""))
        return f"/{lang}/{alias}/{slug}{frag}"
    return _DOC_LINK_RE.sub(rep, md)


__HEAD_RE = re.compile(r"^(#{2,6})\s+(.+?)\s*$", re.M)


def normalize_headings(md: str, anchors: list[dict]) -> str:
    """按官方 anchorList 的 level 归一标题层级。

    官网正文常用 <h4> 表示一级小节，markitdown 会输出 ####，在 docsify 里
    显示过小。这里用 anchor level（1 表示文档内一级）反推应有的层级：
    level 1 → ##, level 2 → ###, 依此类推。
    """
    lvl_by_title: dict[str, int] = {}
    for a in anchors or []:
        t = (a.get("title") or "").strip()
        try:
            lvl = int(a.get("level") or 0)
        except (TypeError, ValueError):
            lvl = 0
        if t and lvl:
            lvl_by_title.setdefault(t, lvl)

    def rep(m):
        hashes, title = m.group(1), m.group(2)
        t = re.sub(r"\s+#+$", "", title).strip()
        lvl = lvl_by_title.get(t)
        if not lvl:
            return m.group(0)
        depth = min(lvl + 1, 6)
        return f"{'#' * depth} {title}"

    return __HEAD_RE.sub(rep, md)


def anchors_for_docsify(anchors: list[dict]) -> list[dict]:
    """规整 anchorList → [{level, title, anchorId}]（只留文档内锚点）。"""
    out = []
    for a in anchors or []:
        aid = a.get("anchorId")
        title = (a.get("title") or "").strip()
        if not aid or not title:
            continue
        try:
            lvl = int(a.get("level") or 0)
        except (TypeError, ValueError):
            lvl = 0
        out.append({"level": lvl, "title": title, "anchorId": aid})
    return out
