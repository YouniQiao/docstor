"""图片检查：模糊（无 AI，边界强度方差）、英文文档图中含中文（OCR，可选）。"""
from __future__ import annotations

import json
import os
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


_CACHE = None          # (pid, conn) —— 必须按进程各开一条，见 _cache_conn


def _cache_conn():
    """按进程缓存数据库连接。

    ds_check 是多进程跑的，sqlite 连接对象不能跨 fork 共享：父进程先开过连接、
    子进程继承后一查就报错，规则会静默返回空结果（曾导致整板块 0 命中）。
    """
    global _CACHE
    pid = os.getpid()
    if _CACHE and _CACHE[0] == pid:
        return _CACHE[1]
    conn = None
    try:
        import sqlite3
        db = Path(__file__).resolve().parent.parent / "data/ocr_cache.sqlite"
        if db.exists():
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)
    except Exception:  # noqa: BLE001
        conn = None
    _CACHE = (pid, conn)
    return conn


def _ocr_text(path: Path) -> str:
    """读离线 OCR 缓存（scripts/ocr_images.py 产出），检查阶段不再现场跑 OCR。

    PP-OCRv6 + oneDNN 对个别图片会 SIGSEGV，进程内调用会把整轮检查带崩；
    改成离线批量 + 可断点续跑的缓存后，检查只做查表。
    """
    try:
        conn = _cache_conn()
        if conn is None:
            return ""
        rel = path.resolve().relative_to(Path(__file__).resolve().parent.parent).as_posix()
        row = conn.execute("SELECT text FROM ocr WHERE path=?", (rel,)).fetchone()
        return (row[0] or "") if row else ""
    except Exception:  # noqa: BLE001
        return ""


CN_RE = re.compile(r"[\u4e00-\u9fff]")
CN_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

# OCR 在纯英文图上会零星蹦出几个汉字（日 / 三 / 国 / 路 这类来自图标、日期、键盘、地图图例），
# 那是识别噪声不是中文内容。实测：只要求「有汉字」会命中 3,629 张，九成是噪声；
# 「≥10 个汉字且有一段 ≥5 字连续中文」或「单段 ≥8 字连续中文」时，抽检 100% 是真中文
# （地图地名、HarmonyOS 界面、中文日志），共 157 张。
CN_MIN_CHARS = 10
CN_MIN_RUN = 5
CN_LONG_RUN = 8


def _cn_hits(text: str) -> tuple[int, int]:
    """返回（汉字数, 最长连续中文段长度）。"""
    runs = [len(m) for m in CN_RUN_RE.findall(text)]
    return len(CN_RE.findall(text)), (max(runs) if runs else 0)


def _is_cn_text(text: str) -> bool:
    chars, run = _cn_hits(text)
    return (chars >= CN_MIN_CHARS and run >= CN_MIN_RUN) or run >= CN_LONG_RUN


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
        if text and _is_cn_text(text):
            title, aid = ctx.anchor_for_line(line_no)
            snippet = " ".join(text.split())[:80]
            out.append(Issue("image.cn_text", f"英文文档图片中包含中文文字：{snippet}",
                             severity="error",
                             locator={"line": line_no, "anchorId": aid, "image": f"images/{name}"},
                             anchor_text=title, evidence={"image": name, "ocr": snippet}))
    return out
