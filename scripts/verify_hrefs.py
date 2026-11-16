#!/usr/bin/python3
"""核对问题页「问题」列里那个官网超链接本身能不能打开。

问题列展示的是我们从镜像路由还原出来的官网地址：路径段如果本来就是官网目录名
（harmonyos-guides-V5、design-guides-V1…）直接还它；如果是我们镜像的板块名
（capabilities…），就按实测目录还原。板块名映射错、老板块名（multi-device…）
当目录用时都会拼出 404。

用法：
  python3 scripts/verify_hrefs.py            # 只体检，写 data/href_check.json
  python3 scripts/verify_hrefs.py --apply    # 把能救的 href 写回 issues.locator
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_api          # noqa: E402
import ds_catalog      # noqa: E402
import ds_official     # noqa: E402

DB = ROOT / "data" / "docstor.db"
OUT = ROOT / "data" / "href_check.json"
_local = None


def _api():
    """每个线程一个 DocCenter（自带 session）。"""
    global _local
    import threading
    if _local is None:
        _local = threading.local()
    api = getattr(_local, "api", None)
    if api is None:
        api = ds_api.DocCenter()
        _local.api = api
    return api


DOC_RE = re.compile(r"^https://developer\.huawei\.com/consumer/(cn|en)/doc/([^/#?]+)/([^#?]+)")


def candidates(lang: str, cat: str, section: str) -> list[str]:
    cats: list[str] = []
    if section:
        cats += list(ds_official.SECTION_CATS.get(section, []))
    cats += [c for pre, c in ds_catalog.PREFIX_CATS if cat.startswith(pre)]
    cats += [cat]
    cats += list(ds_official.ALL_CATS)
    cats += ds_catalog._all_catalogs()
    return list(dict.fromkeys(cats))


def main() -> int:
    apply = "--apply" in sys.argv
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, doc_key, locator FROM issues"
                        " WHERE status='open' AND locator IS NOT NULL").fetchall()
    hrefs: dict[str, list[int]] = {}
    meta: dict[str, tuple] = {}
    for r in rows:
        try:
            loc = json.loads(r["locator"])
        except Exception:  # noqa: BLE001
            continue
        h = loc.get("href") or ""
        m = DOC_RE.match(h)
        if not m:
            continue
        hrefs.setdefault(h, []).append(r["id"])
        meta[h] = (m.group(1), m.group(2), m.group(3), r["doc_key"].split("/")[1])

    print(f"  待核对官网超链接 {len(hrefs)} 个（涉及 {len(rows)} 条问题）", flush=True)

    def job(item):
        href, (lang, cat, slug, section) = item
        api = _api()
        try:
            d = api._post("getDocumentById",
                          {"objectId": slug, "catalogName": cat, "language": lang})
        except Exception:  # noqa: BLE001
            return href, "error", ""
        if d.get("code") == 0:
            return href, "ok", href
        for cand in candidates(lang, cat, section):
            try:
                d = api._post("getDocumentById",
                              {"objectId": slug, "catalogName": cand, "language": lang})
            except Exception:  # noqa: BLE001
                continue
            if d.get("code") == 0:
                new = f"https://developer.huawei.com/consumer/{lang}/doc/{cand}/{slug}"
                return href, "moved", new
        return href, "dead", ""

    res = {"ok": [], "moved": [], "dead": [], "error": []}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(job, it) for it in meta.items()]
        for i, f in enumerate(as_completed(futs), 1):
            href, st, new = f.result()
            res[st].append({"href": href, "new": new, "ids": hrefs.get(href, [])})
            if i % 200 == 0:
                print(f"   {i}/{len(meta)} ok={len(res['ok'])} moved={len(res['moved'])}"
                      f" dead={len(res['dead'])}", flush=True)

    print(f"  结论：能打开 {len(res['ok'])}，目录变了 {len(res['moved'])}，"
          f"真的打不开 {len(res['dead'])}，异常 {len(res['error'])}", flush=True)
    OUT.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")

    if apply and res["moved"]:
        n = 0
        for item in res["moved"]:
            for iid in item["ids"]:
                row = conn.execute("SELECT locator FROM issues WHERE id=?", (iid,)).fetchone()
                try:
                    loc = json.loads(row["locator"])
                except Exception:  # noqa: BLE001
                    continue
                loc["href"] = item["new"]
                conn.execute("UPDATE issues SET locator=? WHERE id=?",
                             (json.dumps(loc, ensure_ascii=False), iid))
                n += 1
        conn.commit()
        print(f"  已写回 {n} 条问题的官网地址", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
