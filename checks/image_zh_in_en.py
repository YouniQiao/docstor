"""image.zh_in_en —— 英文文档截图内含中文界面文案（OCR 筛候选 + AI 复核）。

判据链条（本地 OCR + AI 都已落库，检查时只读库，不联网）：
  1. 文档里的图片引用 → 绝对路径 → image_path(path→img_hash)
  2. image_scan 里该图 OCR 结果满足 han≥3 且 汉字占比≥2%（候选）
  3. ai_notes(module='image', kind='image_zh_in_en') 的 verdict == 'real' 才上报
     —— AI 判 false_positive（地图地名/品牌名/用户数据/OCR 误识/字形样例）一律不报
小图标不在扫描池里（长边<400 或 <20KB），本规则自然查不到。
"""
from __future__ import annotations

import bisect
import sqlite3
from pathlib import Path

from .base import Issue, register
from .image_check import IMG_MD, _is_img, _split_target

RULE = "image.zh_in_en"
CAND_HAN = 3
CAND_RATIO = 0.02
_MAP: dict | None = None
_MAP_KEY = ""

SQL = """
SELECT p.path, s.img_hash, s.han, s.chars, s.ratio,
       n.verdict, n.confidence, n.reason, n.suggestion
FROM image_path p
JOIN image_scan s ON s.img_hash = p.img_hash
LEFT JOIN ai_notes n ON n.module = 'image' AND n.item_key = s.img_hash
                    AND n.kind = 'image_zh_in_en'
WHERE s.han >= ? AND s.ratio >= ?
"""


def _load_map(ctx) -> dict:
    """path → 候选行（含 AI 结论）。每进程只读一次。"""
    global _MAP, _MAP_KEY
    db_path = ""
    try:
        db_path = str(Path(ctx.db.conn.execute("PRAGMA database_list").fetchone()[2]).resolve())
    except Exception:  # noqa: BLE001
        db_path = ""
    if _MAP is not None and _MAP_KEY == db_path:
        return _MAP
    m: dict = {}
    try:
        conn = sqlite3.connect(db_path or ctx.db.path, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(SQL, (CAND_HAN, CAND_RATIO)):
                m[r["path"]] = dict(r)
                try:                                   # 兼容 ./ ../ 写法
                    m.setdefault(str(Path(r["path"]).resolve()), dict(r))
                except Exception:  # noqa: BLE001
                    pass
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        m = {}
    _MAP, _MAP_KEY = m, db_path
    return m


def _line_of(newlines: list[int], off: int) -> int:
    return bisect.bisect_left(newlines, off) + 1


@register(RULE, label="英文图片含中文", severity="warn", langs=("en",),
          ai_hint="英文文档里的截图含中文界面或文案，改法是换成英文版截图并更新文件名与 alt 文案；"
                  "若中文属于地图地名、农历日历、品牌并列或用户数据，则不算问题。"
                  "图片不在你手上，只说清该不该换、该换成什么场景，不要编造新文件名。",
          default_on=True, default_visible=False)
def check_image_zh_in_en(doc, ctx):
    out: list[Issue] = []
    if (doc.get("lang") or "") != "en":
        return out
    text = ctx.text
    if not text or "![" not in text:
        return out
    table = _load_map(ctx)
    if not table:
        return out
    doc_dir = ctx.md_path.parent
    newlines = [i for i, ch in enumerate(text) if ch == "\n"]
    seen: set[str] = set()

    for m in IMG_MD.finditer(text):
        target = _split_target(m.group(2))
        if not target or not _is_img(target) or target.startswith(("http://", "https://", "//", "data:")):
            continue
        p = doc_dir / target.split("?")[0].split("#")[0].lstrip("./")
        row = table.get(str(p))
        if row is None:
            try:
                row = table.get(str(p.resolve()))
            except Exception:  # noqa: BLE001
                row = None
        if row is None or (row.get("verdict") or "") != "real":
            continue
        h = row["img_hash"]
        if h in seen:
            continue
        seen.add(h)
        line = _line_of(newlines, m.start())
        title, aid = ctx.anchor_for_line(line)
        conf = row.get("confidence")
        pct = f"{round((conf or 0) * 100)}%" if conf is not None else "—"
        out.append(Issue(
            rule=RULE,
            message=(f"截图含中文界面文案（AI {pct}）：{row.get('reason') or '截图内界面文案未翻译'}"),
            severity="warn",
            locator={"line": line, "target": target, "image": target, "img": h},
            anchor_text=title,
            evidence={"anchorId": aid, "image": target, "img_hash": h,
                      "han": row.get("han"), "ratio": round((row.get("ratio") or 0) * 100, 1),
                      "confidence": conf, "suggestion": row.get("suggestion") or ""},
        ))
    return out
