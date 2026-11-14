"""图片检查：模糊（无 AI，边界强度方差）、英文文档图中含中文（OCR，可选）。"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Issue, register

IMG_REF_RE = re.compile(r"!\[[^\]]*\]\(images/([^)\s\"]+)")

MIN_SIDE = 120          # 小于这个边长的图当图标忽略
BLUR_THRESHOLD = 6.0    # 边缘标准差低于此值视为「可能模糊」（偏保守）
MIN_BYTES = 20 * 1024   # 太小的图不看


def _iter_images(ctx):
    for i, line in enumerate(ctx.text.splitlines(), 1):
        m = IMG_REF_RE.search(line)
        if m:
            yield i, m.group(1)


def _blur_score(path: Path) -> float | None:
    try:
        from PIL import Image, ImageFilter, ImageStat
    except Exception:  # noqa: BLE001
        return None
    try:
        if path.suffix.lower() in (".svg", ".mp4"):
            return None
        if path.stat().st_size < MIN_BYTES:
            return None
        with Image.open(path) as im:
            if min(im.size) < MIN_SIDE:
                return None
            g = im.convert("L")
            g.thumbnail((900, 900))
            edges = g.filter(ImageFilter.FIND_EDGES)
            return float(ImageStat.Stat(edges).stddev[0])
    except Exception:  # noqa: BLE001
        return None


@register("image.blur", label="图片可能模糊", default_on=False)
def check_blur(doc, ctx):
    out = []
    base = Path(ctx.md_path).parent
    for line_no, name in _iter_images(ctx):
        p = base / "images" / name
        if not p.exists():
            continue
        score = _blur_score(p)
        if score is None or score >= BLUR_THRESHOLD:
            continue
        title, aid = ctx.anchor_for_line(line_no)
        out.append(Issue("image.blur", f"图片清晰度偏低（边缘强度 {score:.1f}）：{name}",
                         severity="info",
                         locator={"line": line_no, "anchorId": aid, "image": f"images/{name}"},
                         anchor_text=title, evidence={"score": round(score, 2), "image": name}))
    return out


_OCR = None


def _ocr_text(path: Path) -> str:
    """复用 PaddleOCR（若环境可用），识别图片里的中文。"""
    global _OCR
    try:
        if _OCR is None:
            from paddleocr import PaddleOCR  # type: ignore
            _OCR = PaddleOCR(use_doc_orientation_classify=False,
                             use_doc_unwarping=False,
                             use_textline_orientation=False,
                             lang="ch")
        res = _OCR.predict(str(path))
        texts = []
        for r in res:
            for t in (r.get("rec_texts") or []):
                texts.append(str(t))
        return " ".join(texts)
    except Exception:  # noqa: BLE001
        return ""


CN_RE = re.compile(r"[\u4e00-\u9fff]")


@register("image.cn_text", label="图片含中文", langs=("en",))
def check_cn_text(doc, ctx):
    """英文文档里的图不应出现中文（图标/示意图中的中文也算）。"""
    out = []
    base = Path(ctx.md_path).parent
    for line_no, name in _iter_images(ctx):
        p = base / "images" / name
        if not p.exists() or p.suffix.lower() in (".svg", ".mp4"):
            continue
        if p.stat().st_size < MIN_BYTES:
            continue
        text = _ocr_text(p)
        if text and CN_RE.search(text):
            title, aid = ctx.anchor_for_line(line_no)
            snippet = " ".join(text.split())[:80]
            out.append(Issue("image.cn_text", f"英文文档图片中包含中文文字：{snippet}",
                             severity="error",
                             locator={"line": line_no, "anchorId": aid, "image": f"images/{name}"},
                             anchor_text=title, evidence={"image": name, "ocr": snippet}))
    return out
