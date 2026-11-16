#!/usr/bin/python3
"""重验 dead_render_check.json 里超时/报错的目标（单线程，减少争用）。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ds_render  # noqa: E402

OUT = ROOT / "data" / "dead_render_check.json"


def main() -> int:
    rows = json.load(open(OUT))
    bad = [r for r in rows if str(r["verdict"]).startswith(("timeout", "error"))]
    print(f"  重验 {len(bad)} 个超时目标", flush=True)
    for r in bad:
        lang, cat, slug = r["target"]
        if slug.endswith(".md"):
            slug = slug[:-3]
        v = ds_render.check(lang, cat, slug, refresh=True)
        r["verdict"] = v
        print(f"  {'/'.join(r['target'])} → {v}", flush=True)
    json.dump(rows, open(OUT, "w"), ensure_ascii=False, indent=1)
    ok = [r for r in rows if r["verdict"] == "ok"]
    print(f"  可打开 {len(ok)} 个目标 / {len(rows)}，涉及 {sum(len(r['issues']) for r in ok)} 条问题", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
