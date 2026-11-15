"""docstor 数据库（sqlite）。

表：
  nodes  —— 目录树快照（每个板块一棵树，用于生成侧边栏/统计）
  docs   —— 文档元数据（md 路径、内容哈希、锚点）
  issues —— 问题基线（首次=基线，增量并入，每日复核改状态）
  runs   —— 运行记录
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lang TEXT NOT NULL,
  section TEXT NOT NULL,
  node_id TEXT NOT NULL,
  parent TEXT,
  name TEXT,
  slug TEXT,
  is_leaf INTEGER DEFAULT 0,
  is_api INTEGER DEFAULT 0,
  hided INTEGER DEFAULT 0,
  depth INTEGER DEFAULT 0,
  sort INTEGER DEFAULT 0,
  alias TEXT,
  UNIQUE(lang, section, node_id)
);
CREATE INDEX IF NOT EXISTS idx_nodes_sec ON nodes(lang, section, depth, sort);

CREATE TABLE IF NOT EXISTS docs(
  doc_key TEXT PRIMARY KEY,
  lang TEXT, section TEXT, slug TEXT, title TEXT,
  doc_id TEXT, version TEXT,
  is_api INTEGER DEFAULT 0, alias TEXT,
  md_path TEXT, content_hash TEXT, anchors_json TEXT,
  images INTEGER DEFAULT 0, videos INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0,
  fetched_at TEXT, status TEXT DEFAULT 'ok',
  updated_at TEXT,          -- 官网「更新时间」(北京时间)
  url TEXT                  -- 官网原文地址
);
CREATE INDEX IF NOT EXISTS idx_docs_sec ON docs(lang, section);

CREATE TABLE IF NOT EXISTS issues(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fprint TEXT UNIQUE NOT NULL,
  doc_key TEXT NOT NULL, lang TEXT, section TEXT,
  rule TEXT NOT NULL, severity TEXT DEFAULT 'warn',
  locator TEXT, message TEXT, evidence TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  status TEXT DEFAULT 'open',
  resolved_at TEXT, run_id INTEGER, baseline INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_issues_doc ON issues(doc_key, status);
CREATE INDEX IF NOT EXISTS idx_issues_rule ON issues(rule, status);
CREATE INDEX IF NOT EXISTS idx_issues_lang ON issues(lang, status);

CREATE TABLE IF NOT EXISTS link_cache(
  url TEXT PRIMARY KEY,
  status INTEGER,
  checked_at TEXT
);

CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, target TEXT, started_at TEXT, finished_at TEXT, stats TEXT
);
"""


# 板块 → 官网文档 URL 的路径段（浏览器逐板块核对：打开 /doc/<cat>/<slug> 比对 H1 与库中标题）
SECTION_CAT = {
    "capabilities": "harmonyos-guides",
    "getting-started": "harmonyos-guides",
    "deveco-studio": "harmonyos-guides",
    "release-notes": "harmonyos-releases",
    "tools-faq": "harmonyos-faqs",
    "submission": "app",
    "ux-design": "design-guides",
    "advanced-features": "best-practices",
    "industry-solutions": "architecture-guides",
}


def official_url(lang: str, section: str, slug: str, is_api: int = 0) -> str:
    """拼接官网原文地址。API 参考统一在 harmonyos-references，其余按板块。"""
    cat = "harmonyos-references" if is_api else SECTION_CAT.get(section, "harmonyos-guides")
    return f"https://developer.huawei.com/consumer/{lang}/doc/{cat}/{slug}"


def to_cst(s: str) -> str:
    """官网 displayUpdateTime 是 UTC 字符串，转成北京时间 'YYYY-MM-DD HH:mm'。"""
    from datetime import datetime, timedelta, timezone
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime((s or "").strip()[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        except Exception:  # noqa: BLE001
            continue
    return (s or "").strip()[:16]


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def fprint(*parts: str) -> str:
    return hashlib.md5("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()


class DB:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        # 老库补列（CREATE TABLE IF NOT EXISTS 不会改已存在的表）
        for tbl, col, typ in [("nodes", "alias", "TEXT"),
                              ("docs", "updated_at", "TEXT"),
                              ("docs", "url", "TEXT")]:
            cols = [r[1] for r in self.conn.execute(f"PRAGMA table_info({tbl})")]
            if col not in cols:
                self.conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
        self.conn.commit()

    # ---------- nodes ----------
    def replace_section_nodes(self, lang: str, section: str, nodes: list[dict]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM nodes WHERE lang=? AND section=?", (lang, section))
            self.conn.executemany(
                """INSERT INTO nodes(lang,section,node_id,parent,name,slug,is_leaf,is_api,hided,depth,sort,alias)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(lang, section, n["node_id"], n["parent"], n["name"], n["slug"],
                  int(n["is_leaf"]), int(n["is_api"]), int(n["hided"]),
                  n["depth"], n["sort"], n.get("alias")) for n in nodes])

    # ---------- docs ----------
    def upsert_doc(self, doc: dict) -> None:
        cols = ("doc_key,lang,section,slug,title,doc_id,version,is_api,alias,"
                "md_path,content_hash,anchors_json,images,videos,bytes,fetched_at,status")
        self.conn.execute(
            f"INSERT INTO docs({cols}) VALUES({','.join('?' * 17)}) "
            "ON CONFLICT(doc_key) DO UPDATE SET "
            "title=excluded.title, doc_id=excluded.doc_id, version=excluded.version, "
            "is_api=excluded.is_api, alias=excluded.alias, md_path=excluded.md_path, "
            "content_hash=excluded.content_hash, anchors_json=excluded.anchors_json, "
            "images=excluded.images, videos=excluded.videos, bytes=excluded.bytes, "
            "fetched_at=excluded.fetched_at, status=excluded.status",
            tuple(doc.get(c) for c in cols.replace(" ", "").split(",")))
        self.conn.commit()

    def set_updated(self, doc_key: str, raw) -> None:
        if not raw:
            return
        self.conn.execute("UPDATE docs SET updated_at=? WHERE doc_key=?",
                          (to_cst(str(raw)), doc_key))

    def set_url(self, doc_key: str, url: str) -> None:
        if url:
            self.conn.execute("UPDATE docs SET url=? WHERE doc_key=?", (url, doc_key))

    def export_meta(self, lang: str, section: str, out_dir) -> int:
        """导出该板块 {slug: {t: 更新时间, u: 官网原文地址}} 供前端显示。"""
        import json as _json
        from pathlib import Path as _Path
        rows = self.conn.execute(
            "SELECT slug, updated_at, url FROM docs WHERE lang=? AND section=? AND status='ok'",
            (lang, section)).fetchall()
        if not rows:
            return 0
        d = {}
        for r in rows:
            item = {}
            if r["updated_at"]:
                item["t"] = r["updated_at"]
            if r["url"]:
                item["u"] = r["url"]
            d[r["slug"]] = item
        p = _Path(out_dir) / "_meta.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
        return len(d)

    def get_doc(self, doc_key: str) -> sqlite3.Row | None:
        cur = self.conn.execute("SELECT * FROM docs WHERE doc_key=?", (doc_key,))
        return cur.fetchone()

    def docs_in_section(self, lang: str, section: str) -> list[sqlite3.Row]:
        cur = self.conn.execute(
            "SELECT * FROM docs WHERE lang=? AND section=? AND status='ok' ORDER BY slug",
            (lang, section))
        return cur.fetchall()

    # ---------- issues ----------
    def upsert_issue(self, issue: dict, baseline: bool = True,
                     run_id: int | None = None) -> bool:
        """返回 True 表示新建（首次发现）。"""
        fp = fprint(issue["doc_key"], issue["rule"], issue.get("locator", ""),
                    issue.get("message", ""))
        cur = self.conn.execute("SELECT id, status FROM issues WHERE fprint=?", (fp,))
        row = cur.fetchone()
        ts = now()
        if row is None:
            self.conn.execute(
                """INSERT INTO issues(fprint,doc_key,lang,section,rule,severity,locator,
                       message,evidence,first_seen,last_seen,status,run_id,baseline)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'open',?,?)""",
                (fp, issue["doc_key"], issue.get("lang"), issue.get("section"),
                 issue["rule"], issue.get("severity", "warn"),
                 issue.get("locator"), issue.get("message"),
                 json.dumps(issue.get("evidence") or {}, ensure_ascii=False),
                 ts, ts, run_id, int(baseline)))
            self.conn.commit()
            return True
        self.conn.execute(
            "UPDATE issues SET last_seen=?, status='open', resolved_at=NULL WHERE id=?",
            (ts, row["id"]))
        self.conn.commit()
        return False

    def open_issue_keys(self, doc_key: str) -> set[str]:
        cur = self.conn.execute(
            "SELECT fprint FROM issues WHERE doc_key=? AND status='open'", (doc_key,))
        return {r["fprint"] for r in cur.fetchall()}

    def resolve_missing(self, doc_key: str, keep: set[str], run_id: int | None = None,
                        rules: set[str] | None = None) -> int:
        """文档复检后，把本次未再出现的问题标记已解决。

        rules 非空时只处理这些规则的旧问题 —— 用 `--rules` 跑单类检查时，
        不能把其它规则的历史问题误判为「已解决」（曾把 en 的 723 条链接问题全标 resolved）。
        """
        rows = self.conn.execute(
            "SELECT id, fprint, rule FROM issues WHERE doc_key=? AND status='open'", (doc_key,)
        ).fetchall()
        n = 0
        for r in rows:
            if rules and r["rule"] not in rules:
                continue
            if r["fprint"] not in keep:
                self.conn.execute(
                    "UPDATE issues SET status='resolved', resolved_at=?, run_id=? WHERE id=?",
                    (now(), run_id, r["id"]))
                n += 1
        self.conn.commit()
        return n

    # ---------- link cache ----------
    def get_link_status(self, url: str, ttl_days: int = 7) -> int | None:
        cur = self.conn.execute(
            "SELECT status, checked_at FROM link_cache WHERE url=?", (url,))
        row = cur.fetchone()
        if not row:
            return None
        try:
            from datetime import datetime, timedelta
            ts = datetime.strptime(row["checked_at"], "%Y-%m-%d %H:%M:%S")
            if datetime.now() - ts > timedelta(days=ttl_days):
                return None
        except Exception:  # noqa: BLE001
            return None
        return row["status"]

    def set_link_status(self, url: str, status: int) -> None:
        self.conn.execute(
            "INSERT INTO link_cache(url,status,checked_at) VALUES(?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET status=excluded.status, checked_at=excluded.checked_at",
            (url, status, now()))
        self.conn.commit()

    # ---------- runs ----------
    def start_run(self, kind: str, target: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(kind,target,started_at) VALUES(?,?,?)", (kind, target, now()))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, stats: dict) -> None:
        self.conn.execute("UPDATE runs SET finished_at=?, stats=? WHERE id=?",
                          (now(), json.dumps(stats, ensure_ascii=False), run_id))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
