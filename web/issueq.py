"""全量问题的筛选条件构造。

列表页、导出、邮件订阅共用同一套口径，避免「订阅数字和页面数字对不上」。
参数来源可以是 Flask 的 request.args，也可以是普通 dict（定时任务/脚本里用）。
"""
from __future__ import annotations

import re


def issue_where(a, enabled_filter) -> tuple[str, list]:
    """把筛选参数翻译成 SQL where + args。

    a: request.args 或 dict（键：lang/section/rule/sub/domain/kit/kind/status/q/dfrom/dto）
    enabled_filter: 已开启检查项的过滤函数（app._enabled_filter），保证关闭的检查项不参与
    """
    lang = a.get("lang", "")
    section = a.get("section", "")
    rule = a.get("rule", "")
    sub = a.get("sub", "")
    domain = a.get("domain", "")
    kit = a.get("kit", "")
    kind = a.get("kind", "")
    status = a.get("status", "open")
    q = (a.get("q", "") or "").strip()
    dfrom = (a.get("dfrom", "") or "").strip()
    dto = (a.get("dto", "") or "").strip()

    where, args = ["1=1"], []
    if lang:
        where.append("i.lang=?"); args.append(lang)
    if section:
        where.append("i.section=?"); args.append(section)
    if rule:
        where.append("i.rule=?"); args.append(rule)
    if sub:
        parts = sub.split("|")
        if len(parts) == 3:
            where.append("i.doc_key IN (SELECT c.lang||'/'||c.section||'/'||c.slug FROM doc_cat c"
                         " WHERE c.lang=? AND c.section=? AND c.sub=?)")
            args += parts
    # 指南与API参考：按侧边栏二级（领域）筛选
    if domain:
        parts = domain.split("|")
        if len(parts) == 3:
            where.append("i.doc_key IN (SELECT c.lang||'/'||c.section||'/'||c.slug"
                         " FROM doc_cat c WHERE c.lang=? AND c.section=? AND c.domain=?)")
            args += parts
    # Kit 用归一化名跨语言匹配（中文站 Ability Kit（程序框架服务）= 英文站 Ability Kit）
    if kit:
        parts = kit.split("|")
        if len(parts) == 2:
            where.append("i.doc_key IN (SELECT c.lang||'/'||c.section||'/'||c.slug"
                         " FROM doc_cat c WHERE c.section=? AND c.kit_key=?)")
            args += parts
        elif len(parts) == 3:      # 兼容旧链接（语言|板块|Kit 原名）
            where.append("i.doc_key IN (SELECT c.lang||'/'||c.section||'/'||c.slug"
                         " FROM doc_cat c WHERE c.lang=? AND c.section=? AND c.kit=?)")
            args += parts
    if kind in ("api", "guide"):
        where.append("i.doc_key IN (SELECT doc_key FROM docs WHERE is_api=?)")
        args.append(1 if kind == "api" else 0)
    if status and status != "all":
        where.append("i.status=?"); args.append(status)
    for val, op in ((dfrom, ">="), (dto, "<=")):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", val or ""):
            where.append(f"date(i.first_seen) {op} ?")
            args.append(val)
    if q:
        where.append("(i.message LIKE ? OR i.doc_key LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    fw, fa = enabled_filter("i.rule")
    where.append(fw)
    args += fa
    return " AND ".join(where), args
