#!/usr/bin/python3
"""每天早上 8:00 发送「开发者文档质量日报」订阅邮件。

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
import os
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
SITE = os.environ.get("DOCSTOR_SITE_URL") or "https://docstor.openharmony.cool"
OUT_DIR = BASE / "data" / "mail_preview"
WEEK = "一二三四五六日"
ALERT_TO = os.environ.get("DOCSTOR_ALERT_TO") or "sunxianlei@huawei.com"

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
    # 只用「对外」的规则：调试中的规则不进度量，避免订阅预览和日报提前泄露。
    # 语言限制也在 sql_filter 里：某规则只跑中文时，英文那批旧命中不进日报
    rules = ds_rules.visible_rules(c, checks.REGISTRY)

    def f(col: str):
        try:
            clause, args = ds_rules.sql_filter(c, checks.REGISTRY, rule_col=col,
                                               lang_col=(col.rsplit(".", 1)[0] + ".lang"))
            return clause.lstrip().removeprefix("AND ").strip(), args
        except Exception:  # noqa: BLE001
            if not rules:
                return "1=0", []
            return f"{col} IN ({','.join('?' * len(rules))})", list(rules)

    f.rules = list(rules)   # 供 collect_for 判断某个关注范围是否指向「调试中/已停用」的规则
    return f


def params_of(query: str) -> dict:
    from urllib.parse import unquote_plus      # 必须 unquote_plus：'+' 代表空格（Kit 名里就带空格）
    out = {}
    for kv in (query or "").split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k] = unquote_plus(v)
    return out


def group_stats(c, where, args, day: str) -> dict:
    total = c.execute(f"SELECT COUNT(*) n FROM issues i WHERE {where}", args).fetchone()["n"]
    na, nb = args + [day], args + [day]
    new = c.execute(
        f"SELECT COUNT(*) n FROM issues i WHERE {where} AND date(i.first_seen)=?", na).fetchone()["n"]
    rl = c.execute(
        f"""SELECT i.rule, COUNT(*) n FROM issues i WHERE {where}
            GROUP BY i.rule ORDER BY n DESC""", args).fetchall()
    rules = [{"rule": r["rule"], "label": checks.rule_label(r["rule"]), "n": r["n"]} for r in rl]
    return {"total": total, "new": new, "rules": rules}


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


def _cards_html(items: list[dict]) -> str:
    """问题小卡片：每行 3 个，数字在上、类型在下，点击跳到站点对应筛选。"""
    cells = []
    for it in items:
        cells.append(
            f'<td width="33%" style="padding:0 6px 10px 0">'
            f'<a href="{_esc(it["href"])}" style="display:block;text-decoration:none;'
            f'background:#f7f9fc;border:1px solid #eef0f3;border-radius:10px;padding:11px 12px">'
            f'<div style="font-size:19px;font-weight:700;color:#1f2430;line-height:1.2">'
            f'{it["n"]:,}</div>'
            f'<div style="color:#687083;font-size:12px;margin-top:3px">{_esc(it["label"])}</div>'
            f'</a></td>')
    rows = []
    for i in range(0, len(cells), 3):
        chunk = cells[i:i + 3]
        while len(chunk) < 3:
            chunk.append('<td width="33%"></td>')
        rows.append("<tr>" + "".join(chunk) + "</tr>")
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:separate;border-spacing:0">' + "".join(rows) + "</table>")


def build_html(user_name: str, groups: list[dict], changes: dict, day: str,
               token: str, total_open: int, total_new: int) -> str:
    """total_open / total_new 是各组条件的并集（去重），不是简单相加。"""
    d = datetime.strptime(day, "%Y-%m-%d")
    unsub = f"{SITE}/me/unsubscribe?token={token}"
    sync_link = f"{SITE}/sync?date={day}"
    if changes.get("runs"):
        change_text = (f"新增 <b>{changes['added']:,}</b> 篇 · 修改 <b>{changes['modified']:,}</b> 篇 ·"
                       f" 删除 <b>{changes['deleted']:,}</b> 篇"
                       + (f"（同步 {changes['secs'] // 60} 分钟）" if changes["secs"] else ""))
    else:   # 同步没跑/失败时别说「新增 0 篇」，会被误读成当天官网没变化
        change_text = ('<span style="color:#b25b00">当天没有同步记录（任务未跑或失败），'
                       '问题数据可能未更新</span>')

    head = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:22px 12px;background:#f6f7f9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f6f7f9;">
<tr><td align="center">
<table role="presentation" width="680" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border:1px solid #e4e7ec;border-radius:12px;
              font:14px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2430;">
  <tr><td style="padding:22px 24px 6px">
    <div style="font-size:17px;font-weight:700">开发者文档质量日报 · {d.month} 月 {d.day} 日（周{WEEK[d.weekday()]}）</div>
    <div style="color:#687083;font-size:12.5px;margin-top:4px">
      {_esc(user_name)}，你关注了 {len(groups)} 个范围 ·
      未解决 <b style="color:#1f2430">{total_open:,}</b> 条 · 今日新增
      <b style="color:{'#d92d20' if total_new else '#1f2430'}">{total_new:,}</b> 条
    </div>
  </td></tr>
  <tr><td style="padding:4px 24px 0">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="background:#f7f9fc;border-radius:10px;margin-top:12px;">
      <tr><td style="padding:12px 14px;font-size:13px;color:#4a5160">
        <b style="color:#1f2430">当天文档变化</b>：{change_text}
        <br><a href="{sync_link}" style="color:#2a6df4;text-decoration:none;font-size:12.5px">
          查看 {day} 的文档变更记录 →</a>
      </td></tr>
    </table>
  </td></tr>"""

    body = []
    for g in groups:
        label = _esc(g["label"])
        cond = _esc(g["summary"])
        q = g["query"] or "status=open"
        all_link = f"{SITE}/issues?{q}"
        rules = g.get("rules") or []
        cards = [{"n": r["n"], "label": r["label"],
                  "href": f"{SITE}/issues?{q}&rule={r['rule']}"} for r in rules]
        if len(cards) > 1:          # 只有一种类型时，总数卡片和类型卡片重复，就不要了
            cards.insert(0, {"n": g["total"], "label": "未解决问题总数", "href": all_link})
        new_line = (f'今日新增 <b style="color:#d92d20">{g["new"]:,}</b> 条'
                    if g["new"] else "今日无新增问题 👍")
        block = f"""
  <tr><td style="padding:18px 24px 0">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
      <tr><td style="border-top:1px solid #eef0f3;padding-top:14px">
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="font-size:14.5px;font-weight:700">{label}</td>
          <td align="right"><a href="{all_link}"
             style="color:#2a6df4;text-decoration:none;font-size:12.5px">在站点查看 →</a></td>
        </tr></table>
        {'' if cond == label else f'<div style="color:#687083;font-size:12.5px;margin-top:2px">{cond}</div>'}
        <div style="margin-top:10px">{_cards_html(cards)}</div>
        <div style="font-size:12.5px;color:#687083;margin-top:2px">{new_line}</div>
      </td></tr>
    </table>
  </td></tr>"""
        body.append(block)

    foot = f"""
  <tr><td style="padding:20px 24px 24px">
    <div style="border-top:1px solid #eef0f3;padding-top:14px;color:#9aa3b2;font-size:12px;line-height:1.9">
      这封邮件每天早上 8:00 发送 · 数据来自开发者文档中心镜像检查<br>
      <a href="{SITE}/me/subscribe" style="color:#9aa3b2">调整关注范围</a> ·
      <a href="{unsub}" style="color:#9aa3b2">不再接收（一键退订）</a> ·
      <a href="{SITE}/issues" style="color:#9aa3b2">打开已识别问题</a>
    </div>
  </td></tr>
</table>
</td></tr></table>
</body></html>"""
    return head + "".join(body) + foot


def collect_for(c, where_fn, enabled_filter, day: str, login: str | None = None):
    q = ("SELECT * FROM sub_users WHERE active=1 AND verified_at IS NOT NULL"
         " AND email IS NOT NULL AND TRIM(email) <> ''")
    args: list = []
    if login:
        q += " AND login=?"
        args.append(login)
    users = c.execute(q, args).fetchall()
    vis = set(getattr(enabled_filter, "rules", []))
    out = []
    for u in users:
        subs = c.execute("SELECT * FROM subs WHERE login=? AND enabled=1"
                         " ORDER BY COALESCE(sort_order, id), id",
                         (u["login"],)).fetchall()
        # 「调试中 / 已停用」的检查项：指向它的关注范围本轮整块不出现，
        # 否则会显示成「0 条 · 今日无新增」，看起来像已经修好了
        subs = [s for s in subs
                if not (params_of(s["query"]).get("rule")
                        and vis and params_of(s["query"])["rule"] not in vis)]
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
        conds = [(where_fn(params_of(s["query"]))[0], where_fn(params_of(s["query"]))[1])
                 for s in subs]
        union = " OR ".join(f"({w})" for w, _ in conds)
        union_args = [x for _, a in conds for x in a]
        total_union = c.execute(f"SELECT COUNT(*) n FROM issues i WHERE {union}",
                                union_args).fetchone()["n"]
        new_union = c.execute(
            f"SELECT COUNT(*) n FROM issues i WHERE ({union}) AND date(i.first_seen)=?",
            union_args + [day]).fetchone()["n"]
        out.append({"user": u, "groups": groups,
                    "total_union": total_union, "new_union": new_union})
    return out


def alert_all_failed(day: str, attempted: int, fails: list[tuple[str, str]]) -> None:
    """整轮一封都没发出去时给运维发一封告警，每天最多一封。"""
    flag = BASE / "data" / f".alert_{day}"
    if flag.exists():
        return
    rows = "".join(
        f'<tr><td style="padding:4px 8px;border-bottom:1px solid #f0f1f3">{_esc(to)}</td>'
        f'<td style="padding:4px 8px;border-bottom:1px solid #f0f1f3;color:#d92d20">{_esc(err)}</td></tr>'
        for to, err in fails[:20])
    body = f"""<div style="font:14px/1.7 -apple-system,'PingFang SC','Microsoft YaHei',sans-serif;color:#1f2430">
  <p><b>开发者文档质量日报 {day} 全部发送失败</b>（{attempted} 个收件人，0 成功）</p>
  <table role="presentation" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px">{rows}</table>
  <p style="color:#687083;font-size:12.5px">9:30 会自动重试一次；若仍失败请检查 .smtp.env 与网络。</p>
</div>"""
    try:
        mailer.send_mail(ALERT_TO,
                         f"【告警】开发者文档质量日报 {day} 发送失败（{attempted} 个收件人）",
                         body, from_name="docstor 告警")
        flag.write_text(datetime.now().isoformat(), encoding="utf-8")
        print(f"  已发告警邮件给 {ALERT_TO}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! 告警邮件也发不出去：{e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--to", default="", help="测试：只发这个地址")
    ap.add_argument("--login", default="", help="只发某个 GitCode 账号")
    ap.add_argument("--dry-run", action="store_true", help="只生成本地 html，不发信")
    ap.add_argument("--force", action="store_true",
                    help="当天已成功发送过也再发一次（默认同一账号同一天只发一封，防误触/重跑）")
    args = ap.parse_args()

    c = conn()
    ef = _enabled_filter_factory(c)
    where_fn = lambda p: issue_where(p, ef)  # noqa: E731
    changes = doc_changes(c, args.date)

    if args.to:
        # 测试发送：--login 指定账号就用它，否则取第一个已确认订阅者；都没有就用「全部问题」搭一个
        q = ("SELECT * FROM sub_users WHERE active=1 AND verified_at IS NOT NULL"
             " AND TRIM(COALESCE(email,'')) <> ''")
        u = (c.execute(q + " AND login=? LIMIT 1", (args.login,)).fetchone() if args.login
             else c.execute(q + " LIMIT 1").fetchone())
        if u:
            targets = collect_for(c, where_fn, ef, args.date, u["login"])
        else:
            w, a = where_fn({"status": "open"})
            st = group_stats(c, w, a, args.date)
            targets = [{"user": {"login": "test", "name": "测试", "token": "x"},
                        "groups": [{"id": 0, "label": "全部问题", "summary": "",
                                    "query": "status=open", **st}]}]
    else:
        targets = collect_for(c, where_fn, ef, args.date, args.login or None)

    if not targets:
        print(f"  没有可发送的订阅者（date={args.date}）")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    sent = 0
    attempted = 0
    fails: list[tuple[str, str]] = []
    for t in targets:
        u = t["user"]
        uname = u["name"] or u["login"]
        total_union = t.get("total_union") or sum(g["total"] for g in t["groups"])
        new_union = t.get("new_union") or sum(g["new"] for g in t["groups"])
        body = build_html(uname, t["groups"], changes, args.date, u["token"] or "x",
                          total_union, new_union)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        p = OUT_DIR / f"{args.date}_{u['login']}_{stamp}.html"
        p.write_text(body, encoding="utf-8")
        # 幂等：同一账号同一天只发一封。定时任务被重复触发、手动重跑都不会重复轰炸收件人
        if (not args.to and not args.force and not args.dry_run
                and args.date == date.today().strftime("%Y-%m-%d")):
            done = c.execute(
                "SELECT 1 FROM sub_sends WHERE login=? AND ok=1 AND substr(sent_at,1,10)=? LIMIT 1",
                (u["login"], args.date)).fetchone()
            if done:
                print(f"  - {u['login']} 今天已发过，跳过（需要重发加 --force）")
                continue
        to = args.to or u["email"]
        subject = (f"开发者文档质量日报 {args.date}｜未解决 "
                   f"{total_union} 条，新增 {new_union} 条｜文档变化 +{changes['added']}")
        if args.dry_run:
            print(f"  [dry-run] {u['login']} → {to} | {p}")
            continue
        attempted += 1
        unsub_url = f"{SITE}/me/unsubscribe?token={u['token']}"
        try:
            mailer.send_mail(to, subject, body, unsubscribe_url=unsub_url)
            ok = 1
            err = ""
            sent += 1
        except Exception as e:  # noqa: BLE001
            ok, err = 0, str(e)[:300]
            fails.append((to, err))
            print(f"  ✗ {u['login']} → {to}: {err}")
        c.execute("""INSERT INTO sub_sends(login,email,sent_at,groups,issues,new_today,ok,error)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (u["login"], to, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   len(t["groups"]), total_union, new_union, ok, err))
        c.commit()
        if ok:
            print(f"  ✓ {u['login']} → {to}（{len(t['groups'])} 组）")
    if attempted and sent == 0 and not args.dry_run:   # 全军覆没才告警，个别失败只入库
        alert_all_failed(args.date, attempted, fails)

    cutoff = datetime.now().timestamp() - 30 * 86400
    for f in OUT_DIR.glob("*.html"):        # 预览件只留 30 天
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
    print(f"  完成：{sent}/{len(targets)} 已发送，date={args.date}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
