#!/usr/bin/python3
"""英文图片中文 AI 复核：读 OCR 结果 → 选候选 → 调 AI 判定 → 落 ai_notes。

用法:
    python3 scripts/image_ai_review.py [--ocr /tmp/full_ocr.jsonl] [--han 3] [--ratio 0.02]
                                       [--limit N] [--workers 6] [--dry-run]

- 按图片内容哈希去重（同一张图只判一次，跨文档复用）
- 已判过的（ai_notes 里 module='image'）自动跳过
- 结果同时追加到 /tmp/image_ai_review.jsonl，便于回看
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import ai  # noqa: E402
from ai.cache import note_get  # noqa: E402

RUBRIC = "image_zh_in_en"
INDEX_FILE = Path("/tmp/img_doc_index.json")
OUT_FILE = Path("/tmp/image_ai_review.jsonl")
IMG_RE = re.compile(r"images/([0-9a-f]{32}\.[a-z]{3,4})")

_lock = threading.Lock()


def build_index(force: bool = False) -> dict:
    """图片文件名 → (doc_key, url, title)，扫一遍英文 md（结果缓存到 /tmp）。"""
    if INDEX_FILE.exists() and not force:
        try:
            return json.loads(INDEX_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    import sqlite3
    db = sqlite3.connect(BASE / "data/docstor.db")
    db.row_factory = sqlite3.Row
    idx: dict[str, dict] = {}
    for r in db.execute("select doc_key, md_path, url, title from docs "
                        "where lang='en' and status='ok' and md_path is not null"):
        p = BASE / r["md_path"]
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in set(IMG_RE.findall(text)):
            idx.setdefault(name, {"doc_key": r["doc_key"], "url": r["url"],
                                  "title": r["title"], "refs": 0})
            idx[name]["refs"] += 1
    INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return idx


def collect(ocr_path: Path, han_min: int, ratio_min: float, limit: int) -> list[dict]:
    cands, seen = [], set()
    for ln in ocr_path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("err") or r["han"] < han_min:
            continue
        ratio = r["han"] / max(1, r["chars"])
        if ratio < ratio_min:
            continue
        h = r.get("img_hash") or r["path"]
        if h in seen:
            continue
        seen.add(h)
        r["ratio"] = ratio
        cands.append(r)
    cands.sort(key=lambda x: -x["han"])
    return cands[:limit] if limit else cands


def one(item: dict, index: dict, dry: bool) -> dict:
    name = Path(item["path"]).name
    meta = index.get(name) or {}
    fields = {
        "doc_url": meta.get("url", ""),
        "doc_title": (meta.get("title") or "")[:80],
        "ocr_text": (item.get("text") or "")[:600],
        "han": item["han"],
        "ratio": f"{item['ratio']*100:.1f}%",
    }
    if dry:
        return {"img_hash": item.get("img_hash"), "dry": True, **fields}
    res = ai.review_image(RUBRIC, fields, item["path"], module="image",
                          item_key=item.get("img_hash") or name)
    row = {"img_hash": item.get("img_hash"), "path": item["path"], "han": item["han"],
           "ratio": round(item["ratio"], 4), "doc_key": meta.get("doc_key", ""),
           "verdict": res["verdict"], "confidence": res.get("confidence"),
           "reason": res.get("reason"), "suggestion": res.get("suggestion"),
           "cached": res["_meta"]["cached"], "tokens": res["_meta"].get("tokens", 0)}
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ocr", default="/tmp/full_ocr.jsonl")
    ap.add_argument("--han", type=int, default=3)
    ap.add_argument("--ratio", type=float, default=0.02)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)      # 并发高了会撞 429
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not ai.ai_ready() and not args.dry_run:
        print("AI 未配置（缺 .ai.env）", file=sys.stderr)
        sys.exit(2)
    index = build_index()
    print(f"文档索引 {len(index):,} 张图", flush=True)
    cands = collect(Path(args.ocr), args.han, args.ratio, args.limit)
    done = set(note_get("image", RUBRIC).keys())
    todo = [c for c in cands if (c.get("img_hash") or "") not in done]
    print(f"候选 {len(cands)} 张（已判过 {len(cands) - len(todo)}，本次待判 {len(todo)}）", flush=True)

    counts: dict[str, int] = {}
    ok = fail = 0
    with OUT_FILE.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(args.workers) as ex:
        futs = {ex.submit(one, c, index, args.dry_run): c for c in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"  ! {type(e).__name__}: {str(e)[:110]}", flush=True)
                continue
            ok += 1
            v = row.get("verdict", "dry")
            counts[v] = counts.get(v, 0) + 1
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            if i % 20 == 0 or i == len(todo):
                print(f"  {i}/{len(todo)} 完成  {counts}", flush=True)
    print(f"\n完成 {ok} 条，失败 {fail}；结论分布：{counts}")


if __name__ == "__main__":
    main()
