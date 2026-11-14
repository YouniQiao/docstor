"""规范性检查：重复字/词、标点重复、常见错别字（规则类，零成本）。"""
from __future__ import annotations

import re

from .base import Issue, register

# 中文重复字只报「明显的书写重复」，避开正常叠词
DUP2_RE = re.compile(r"([\u4e00-\u9fff])\1")
# 不含英文句点：`...`（省略号）、`1..10`（区间）都合法
PUNCT_DUP_RE = re.compile(r"([，。、；：？！,;:?!])\1+")

# 正常叠词白名单（常见词）
OK_DOUBLES = set("""
谢谢 常常 渐渐 刚刚 往往 偏偏 恰恰 仅仅 仅仅 慢慢 轻轻 悄悄 稍稍 纷纷 处处 人人 家家 年年 天天 时时
个个 种种 件件 条条 想想 看看 试试 问问 说说 走走 坐坐 好好 多多 少少 大大 小小 高高 长长 大大 各各
一一 二二 三三 星星 宝宝 妈妈 爸爸 哥哥 姐姐 弟弟 妹妹 爷爷 奶奶 叔叔 阿姨 娃娃 团团 层层 句句 字字
微微 稍稍 略略 屡屡 频频 频频 匆匆 悠悠 茫茫 苍苍 滴滴 点点 星星 白白 红红 绿绿 蓝蓝 黑黑 高高
""".split())

# 常见写重复的函数词/助词（基本只可能是手误）
FUNC_DUP = {"的的", "了了", "是是", "就就", "都都", "也也",
            "还还", "又又", "而而", "或或", "并并", "之之", "其其", "这这", "那那"}
# 「在在」「和和」「与与」「及及」会被「现在在」「存在在」等正常词组命中，不报

# 常见错别字 / 错误写法 → 建议
# 只保留无争议的错写（「泄露/帐号/登陆」等在规范上有争议，不报）
TYPOS = {
    "分辩": "分辨",
    "松驰": "松弛",
    "既使": "即使",
    "做为": "作为",
    "按装": "安装",
    "不径而走": "不胫而走",
    "一如继往": "一如既往",
    "严竣": "严峻",
    "报筹": "报酬",
    "凑和": "凑合",
}


def _clean_lines(ctx):
    in_code = False
    for i, line in enumerate(ctx.text.splitlines(), 1):
        s = line.strip()
        if s.startswith("```"):
            in_code = not in_code
            continue
        if in_code or s.startswith(("    ", "|", ">")):
            continue
        # 去掉行内代码与链接目标，减少误报
        line = re.sub(r"`[^`]*`", "", line)
        line = re.sub(r"\(([^)]*)\)", "()", line)
        yield i, line


@register("text.dup", label="重复字", default_on=False)
def check_dup(doc, ctx):
    out = []
    for line_no, line in _clean_lines(ctx):
        for m in PUNCT_DUP_RE.finditer(line):
            title, aid = ctx.anchor_for_line(line_no)
            out.append(Issue("text.dup", f"标点重复：{m.group(0)}",
                             locator={"line": line_no, "anchorId": aid},
                             anchor_text=title, evidence={"snippet": line[max(0, m.start()-24):m.start()+24]}))
        for m in DUP2_RE.finditer(line):
            pair = m.group(0)
            if pair in OK_DOUBLES or pair[0] in "一不很最更太多少":
                continue
            ctx_snip = line[max(0, m.start()-30):m.start()+30]
            sev = "warn" if pair in FUNC_DUP else "info"
            if pair not in FUNC_DUP:
                continue  # 只报函数词的重复，避免叠词误报
            title, aid = ctx.anchor_for_line(line_no)
            out.append(Issue("text.dup", f"疑似重复字「{pair}」",
                             severity=sev, locator={"line": line_no, "anchorId": aid},
                             anchor_text=title, evidence={"snippet": ctx_snip}))
    return out


@register("text.typo", label="疑似错别字", default_on=False)
def check_typo(doc, ctx):
    out = []
    for line_no, line in _clean_lines(ctx):
        for wrong, right in TYPOS.items():
            idx = line.find(wrong)
            while idx >= 0:
                title, aid = ctx.anchor_for_line(line_no)
                out.append(Issue("text.typo", f"「{wrong}」建议写作「{right}」",
                                 locator={"line": line_no, "anchorId": aid},
                                 anchor_text=title,
                                 evidence={"snippet": line[max(0, idx-24):idx+24]}))
                idx = line.find(wrong, idx + 1)
    return out
