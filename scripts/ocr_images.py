#!/usr/bin/python3
"""把「英文文档图片里是否含中文」的 OCR 结果离线缓存下来（可断点续跑）。

为什么单独干：
- PP-OCRv6 + oneDNN 在本机对个别图片会 SIGSEGV（ReduceMeanCheckIfOneDNNSupport），
  进程内直接崩，ds_check 会整轮作废；
- 9k+ 张图要跑很久，边跑边落库，中断后重跑只处理没做过的。

产物：`data/ocr_cache.sqlite`（path,size,mtime,text），检查侧只查表不再现场 OCR。

用法：
  python3 scripts/ocr_images.py --lang en --workers 4
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import queue as _queue
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CACHE = BASE / "data/ocr_cache.sqlite"
SKIP = BASE / "data/ocr_skip.txt"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
MIN_BYTES = 20 * 1024

DET_MODEL = "PP-OCRv5_mobile_det"
REC_MODEL = "PP-OCRv5_mobile_rec"


def init_db():
    db = sqlite3.connect(CACHE)
    db.execute("""CREATE TABLE IF NOT EXISTS ocr(
        path TEXT PRIMARY KEY, size INTEGER, mtime REAL, text TEXT, ts TEXT,
        ok INTEGER DEFAULT 1)""")
    db.commit()
    return db


def pending(db, lang: str) -> list[tuple[str, int, float]]:
    root = BASE / "site/docs" / lang
    done = {r[0]: (r[1], r[2]) for r in db.execute("SELECT path, size, mtime FROM ocr")}
    skip = set()
    if SKIP.exists():
        skip = {l.strip() for l in SKIP.read_text().splitlines() if l.strip()}
    out = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in IMG_EXT or "/images/" not in str(p):
            continue
        st = p.stat()
        if st.st_size < MIN_BYTES:
            continue
        key = str(p.relative_to(BASE))
        if key in skip:
            continue
        prev = done.get(key)
        if prev and prev[0] == st.st_size and abs(prev[1] - st.st_mtime) < 1:
            continue
        out.append((str(p), st.st_size, st.st_mtime))
    return out


def worker_main(chunk_q, out_q, err_q):
    from PIL import Image
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(use_doc_orientation_classify=False, use_doc_unwarping=False,
                    use_textline_orientation=False, lang="ch",
                    text_detection_model_name=DET_MODEL,
                    text_recognition_model_name=REC_MODEL,
                    enable_mkldnn=True)
    tmp = f"/tmp/ocr_norm_{os.getpid()}.jpg"
    while True:
        try:
            chunk = chunk_q.get_nowait()
        except _queue.Empty:
            return
        for path, size, mtime in chunk:
            try:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((1600, 1600))
                    im.save(tmp, "JPEG", quality=92)
                res = ocr.predict(tmp)
                texts = []
                for r in res:
                    texts.extend(str(t) for t in (r.get("rec_texts") or []))
                out_q.put((path, size, mtime, " ".join(texts).strip()))
            except Exception as e:  # noqa: BLE001
                err_q.put((path, f"{type(e).__name__}: {e}"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lang", default="en")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--chunk", type=int, default=20)
    args = ap.parse_args()

    db = init_db()
    todo = pending(db, args.lang)
    total = len(todo)
    print(f"  待 OCR：{total} 张（{args.lang}，模型 {DET_MODEL}/{REC_MODEL}）", flush=True)
    if not total:
        return 0

    chunk_q: mp.Queue = mp.Queue()
    out_q: mp.Queue = mp.Queue()
    err_q: mp.Queue = mp.Queue()
    for i in range(0, total, args.chunk):
        chunk_q.put(todo[i:i + args.chunk])

    procs = [mp.Process(target=worker_main, args=(chunk_q, out_q, err_q), daemon=True)
             for _ in range(args.workers)]
    for p in procs:
        p.start()

    done = 0
    t0 = time.time()
    while any(p.is_alive() for p in procs) or not out_q.empty():
        try:
            path, size, mtime, text = out_q.get(timeout=1)
            db.execute(
                "INSERT OR REPLACE INTO ocr(path,size,mtime,text,ts,ok) VALUES(?,?,?,?,?,1)",
                (str(Path(path).relative_to(BASE)), size, mtime, text,
                 time.strftime("%Y-%m-%d %H:%M:%S")))
            done += 1
            if done % 25 == 0:
                db.commit()
                el = time.time() - t0
                print(f"   进度 {done}/{total}  {done/el:.2f}张/s", flush=True)
        except _queue.Empty:
            pass
        try:
            bad, msg = err_q.get_nowait()
            print(f"   ⚠ {Path(bad).name}: {msg[:70]}", flush=True)
        except _queue.Empty:
            pass

    db.commit()
    crashed = [p.exitcode for p in procs if p.exitcode not in (0, None)]
    left = len(pending(db, args.lang))
    print(f"  ✅ 本次写入 {done} 张，崩溃 worker {len(crashed)} 个，剩余 {left} 张，"
          f"用时 {(time.time()-t0)/60:.1f} 分钟", flush=True)
    return 0 if left == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
