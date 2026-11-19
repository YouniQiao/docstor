#!/usr/bin/python3
"""每天早上 8:00 发送「华为文档质量日报」订阅邮件。

数据来源与「全量问题」页完全一致（web/issueq.issue_where + 已开启检查项），
因此邮件里的数字和页面上的数字对得上。

用法：
  python3 scripts/send_digest.py                 # 发给所有已确认的订阅者
  python3 scripts/send_digest.py --dry-run       # 只生成本地 html，不发信
  python3 scripts/send_digest.py --to a@b.com    # 测试：只发这个地址（忽略订阅表）
  python3 scripts/send_digest.py --login xxx     # 只发某个账号
  python3 scripts/send_digest.py --date 2026-11-18
"""
from __future__ import annotations

import argparse
import html
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "web"))

import yaml  # noqa: E402

import checks  # noqa: E402
import ds_rules  # noqa: E402
import mailer  # noqa: E402
from issueq import issue_where  # noqa: E402

CFG = yaml.safe_load((BASE / "config.yaml").read_text(encoding="utf-8"))
DB_PATH = BASE / CFG["db"]["path"]
SITE = "https://docstor.openharmony.cool"
OUT_DIR = BASE / "data" / "mail_preview"
MAX_ITEMS = 30
WEEK = "一二三四五六日"

SECTION_NAMES = {
    "release-notes": "版本说明", "deveco-studio": "工具", "ux-design": "设计",
    "capabilities": "指南与API参考", "getting-started": "开发与测试",
    "submission": "上架分发与运营", "tools-faq": "问题处理与运维",
    "advanced-features": "最佳实践", "industry-solutions": "行业解决方案",
}


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _enabled_filter_factory(c: sqlite3.Connection):
    cfg = ds_rules.all_settings(c)
    rules = [r for r, meta in checks.REGISTRY.items()
             if (cfg.get(r) or {}).get("enabled", meta.get("default_on", True))]

    def f(col: str):
        if not rules:
            return "1=0", []
        return f"{col} IN ({','.join('?' * len(rules))})", list(rules)

    return f


def params_of(query: str) -> dict:
    from urllib.parse import unquote
    out = {}
    for kv in (query or "").split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = unquote(v)      # 存的是 urlencode 后的串，这里还原成原值给 SQL 用
    return out


def group_stats(c, where, args, day: str) -> dict:
    total = c.execute(f"SELECT COUNT(*) n FROM issues i WHERE {where}", args).fetchone()["n"]
    na, nb = args + [day], args + [day]
    new = c.execute(
        f"SELECT COUNT(*) n FROM issues i WHERE {where} AND date(i.first_seen)=?", na).fetchone()["n"]
    rows = c.execute(
        f"""SELECT i.rule, i.message, i.doc_key, i.locator, i.first_seen,
                   d.title AS doc_title, d.url AS doc_url
            FROM issues i LEFT JOIN docs d ON d.doc_key=i.doc_key
            WHERE {where}
            ORDER BY CASE WHEN date(i.first_seen)=? THEN 0 ELSE 1 END,
                     CASE i.severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
                     i.last_seen DESC
            LIMIT ?""", nb + [MAX_ITEMS]).fetchall()
    return {"total": total, "new": new, "rows": rows}


def doc_changes(c, day: str) -> dict:
    rows = c.execute(
        """SELECT i.kind, COUNT(*) n FROM sync_items i JOIN runs r ON r.id=i.run_id
           WHERE date(r.started_at)=? GROUP BY i.kind""", (day,)).fetchall()
    d = {r["kind"]: r["n"] for r in rows}
    added = d.get("added", 0)
    modified = d.get("modified", 0)
    deleted = d.get("deleted", 0)
    runs = c.execute("""SELECT COUNT(*) n, SUM(COALESCE(json_extract(stats,'$.elapsed_s'),0)) s
                        FROM runs WHERE kind='ingest' AND date(started_at)=?""", (day,)).fetchone()
    top = c.execute(
        """SELECT i.kind, i.title, i.slug, i.lang, i.section FROM sync_items i
           JOIN runs r ON r.id=i.run_id WHERE date(r.started_at)=? AND i.kind='added'
           ORDER BY i.id DESC LIMIT 5""", (day,)).fetchall()
    return {"added": added, "modified": modified, "deleted": deleted,
            "runs": runs["n"] or 0, "secs": int(runs["s"] or 0), "top_added": top}


def _esc(s) -> str:
    return html.escape(str(s or ""))


def _short_msg(msg: str, limit: int = 96) -> str:
    """长路径中间省略：保留开头说明和结尾锚点/文件名，比直接截尾好看。"""
    s = (msg or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 42] + "…" + s[-30:]


def _issue_rows(c, rows) -> str:
    out = []
    for r in rows:
        label = checks.rule_label(r["rule"])
        msg = _short_msg(r["message"] or "")
        title = r["doc_title"] or r["doc_key"]
        url = r["doc_url"] or ""
        link = (f'<a href="{_esc(url)}" style="color:#2a6df4;text-decoration:none">{_esc(title)}</a>'
                if url else _esc(title))
        is_new = (r["first_seen"] or "")[:10]
        tag = ('<span style="display:inline-block;padding:0 5px;border-radius:8px;background:#fff4e5;'
               'color:#b25b00;font-size:11px;margin-left:6px">新</span>')
        out.append(
            '<tr>'
            f'<td style="padding:7px 8px;border-bottom:1px solid #f0f1f3;color:#687083;font-size:12px;white-space:nowrap">{_esc(label)}</td>'
            f'<td style="padding:7px 8px;border-bottom:1px solid #f0f1f3;font-size:13px;color:#1f2430">'
            f'{_esc(msg)}</td>'
            f'<td style="padding:7px 8px;border-bottom:1px solid #f0f1f3;font-size:12.5px">{link}</td>'
            '</tr>')
    return "".join(out)


def build_html(user_name: str, groups: list[dict], changes: dict, day: str,
               token: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    total_open = sum(g["total"] for g in groups)
    total_new = sum(g["new"] for g in groups)
    unsub = f"{SITE}/me/unsubscribe?token={token}"
    sync_link = f"{SITE}/sync?date={day}"

    head = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:22px 12px;background:#f6f7f9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f7f9;">
<tr><td align="center">
<table role="presentation" width="680" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border:1px solid #e4e7ec;border-radius:12px;
              font:14px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2430;">
  <tr><td style="padding:22px 24px 6px">
    <div style="font-size:17px;font-weight:700">华为文档质量日报 · {d.month} 月 {d.day} 日（周{WEEK[d.weekday()]}）</div>
    <div style="color:#687083;font-size:12.5px;margin-top:4px">
      {_esc(user_name)}，你订阅了 {len(groups)} 组条件 ·
      未解决 <b style="color:#1f2430">{total_open:,}</b> 条 · 今日新增
      <b style="color:{'#d92d20' if total_new else '#1f2430'}">{total_new:,}</b> 条
    </div>
  </td></tr>
  <tr><td style="padding:4px 24px 0">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="background:#f7f9fc;border-radius:10px;margin-top:12px;">
      <tr><td style="padding:12px 14px;font-size:13px;color:#4a5160">
        <b style="color:#1f2430">当天文档变化</b>：
        新增 <b>{changes['added']:,}</b> 篇 ·
        修改 <b>{changes['modified']:,}</b> 篇 ·
        删除 <b>{changes['deleted']:,}</b> 篇
        {'（同步 ' + str(changes['secs'] // 60) + ' 分钟）' if changes['secs'] else ''}
        <br><a href="{sync_link}" style="color:#2a6df4;text-decoration:none;font-size:12.5px">
          查看 {day} 的文档变更记录 →</a>
      </td></tr>
    </table>
  </td></tr>"""

    body = []
    for g in groups:
        label = _esc(g["label"])
        cond = _esc(g["summary"])
        all_link = f"{SITE}/issues?{g['query']}"
        if g["rows"]:
            rows_html = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="margin-top:8px;border-collapse:collapse">'
                '<tr>'
                '<th align="left" width="14%" style="padding:0 8px 6px;background:#f7f9fc;color:#687083;font-size:12px;font-weight:600">类型</th>'
                '<th align="left" width="56%" style="padding:0 8px 6px;background:#f7f9fc;color:#687083;font-size:12px;font-weight:600">问题</th>'
                '<th align="left" width="30%" style="padding:0 8px 6px;background:#f7f9fc;color:#687083;font-size:12px;font-weight:600">文档</th>'
                '</tr>' + g["rows_html"] + '</table>')
            if g["total"] > len(g["rows"]):
                rows_html += (f'<div style="color:#9aa3b2;font-size:12px;margin-top:6px">'
                              f'仅列出前 {len(g["rows"])} 条，其余在站点查看。</div>')
        else:
            rows_html = ('<div style="color:#1a7f37;font-size:13px;margin-top:8px">'
                         '今日无新增问题 👍</div>')
        block = f"""
  <tr><td style="padding:18px 24px 0">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="border-top:1px solid #eef0f3;padding-top:14px">
        <div style="font-size:14.5px;font-weight:700">{label}</div>
        <div style="color:#687083;font-size:12.5px;margin-top:2px">{cond}</div>
        <div style="margin-top:8px;font-size:13px">
          本组未解决 <b>{g['total']:,}</b> 条 · 今日新增
          <b style="color:{'#d92d20' if g['new'] else '#1f2430'}">{g['new']:,}</b> 条
          <a href="{all_link}" style="color:#2a6df4;text-decoration:none;font-size:12.5px;margin-left:14px">查看全部 →</a>
        </div>
        {rows_html}
      </td></tr>
    </table>
  </td></tr>"""
        body.append(block)

    foot = f"""
  <tr><td style="padding:20px 24px 24px">
    <div style="border-top:1px solid #eef0f3;padding-top:14px;color:#9aa3b2;font-size:12px;line-height:1.9">
      这封邮件每天早上 8:00 发送 · 数据来自华为开发者文档中心镜像检查<br>
      <a href="{SITE}/me/subscribe" style="color:#9aa3b2">调整订阅条件</a> ·
      <a href="{unsub}" style="color:#9aa3b2">不再接收（一键退订）</a> ·
      <a href="{SITE}/issues" style="color:#9aa3b2">打开全量问题</a>
    </div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
    return head + "".join(body) + foot


def collect_for(c, where_fn, enabled_filter, day: str, login: str | None = None):
    q = "SELECT * FROM sub_users WHERE active=1 AND verified_at IS NOT NULL AND email IS NOT NULL"
    args: list = []
    if login:
        q += " AND login=?"
        args.append(login)
    users = c.execute(q, args).fetchall()
    out = []
    for u in users:
        subs = c.execute("SELECT * FROM subs WHERE login=? AND enabled=1 ORDER BY id",
                         (u["login"],)).fetchall()
        if not subs:
            continue
        groups = []
        for s in subs:
            p = params_of(s["query"])
            w, a = where_fn(p)
            st = group_stats(c, w, a, day)
            groups.append({
                "id": s["id"], "label": s["label"] or "未命名",
                "summary": s["summary"] or "", "query": s["query"], **st,
            })
            groups[-1]["rows_html"] = _issue_rows(c, st["rows"])
        out.append({"user": u, "groups": groups})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--to", default="", help="测试：只发这个地址")
    ap.add_argument("--login", default="", help="只发某个 GitCode 账号")
    ap.add_argument("--dry-run", action="store_true", help="只生成本地 html，不发信")
    args = ap.parse_args()

    c = conn()
    ef = _enabled_filter_factory(c)
    where_fn = lambda p: issue_where(p, ef)  # noqa: E731
    changes = doc_changes(c, args.date)

    if args.to:
        # 测试发送：取第一个已确认订阅者的条件组；没有就用「全部未解决」搭一个
        u = c.execute("SELECT * FROM sub_users WHERE active=1 AND verified_at IS NOT NULL LIMIT 1").fetchone()
        if u:
            targets = collect_for(c, where_fn, ef, args.date, u["login"])
        else:
            w, a = where_fn({"status": "open"})
            st = group_stats(c, w, a, args.date)
            targets = [{"user": {"login": "test", "name": "测试", "token": "x"},
                        "groups": [{"id": 0, "label": "全部未解决", "summary": "全部状态下的未解决问题",
                                    "query": "status=open", **st,
                                    "rows_html": _issue_rows(c, st["rows"])}]}]
    else:
        targets = collect_for(c, where_fn, ef, args.date, args.login or None)

    if not targets:
        print(f"  没有可发送的订阅者（date={args.date}）")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sent = 0
    for t in targets:
        u = t["user"]
        uname = u["name"] or u["login"]
        body = build_html(uname, t["groups"], changes, args.date, u["token"] or "x")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = OUT_DIR / f"{args.date}_{u['login']}_{stamp}.html"
        p.write_text(body, encoding="utf-8")
        to = args.to or u["email"]
        subject = (f"华为文档质量日报 {args.date}｜未解决 "
                   f"{sum(g['total'] for g in t['groups'])} 条，新增 "
                   f"{sum(g['new'] for g in t['groups'])} 条｜文档变化 +{changes['added']}")
        if args.dry_run:
            print(f"  [dry-run] {u['login']} → {to} | {p}")
            continue
        try:
            mailer.send_mail(to, subject, body)
            ok = 1
            err = ""
            sent += 1
        except Exception as e:  # noqa: BLE001
            ok, err = 0, str(e)[:300]
            print(f"  ✗ {u['login']} → {to}: {err}")
        c.execute("""INSERT INTO sub_sends(login,email,sent_at,groups,issues,new_today,ok,error)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (u["login"], to, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   len(t["groups"]), sum(g["total"] for g in t["groups"]),
                   sum(g["new"] for g in t["groups"]), ok, err))
        c.commit()
        if ok:
            print(f"  ✓ {u['login']} → {to}（{len(t['groups'])} 组）")
    print(f"  完成：{sent}/{len(targets)} 已发送，date={args.date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
