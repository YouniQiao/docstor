"""文本检查：正文文字本身写得对不对。

与其它分组的边界（按「载体」分，不按「性质」分）：
  link.*    链接是否有效、指向是否正确
  image.*   图片是否存在、图里是不是中文（image.zh_in_en = 截图里的中文）
  text.*    正文文字本身（本文件）：错别字、重复字、英文正文里的中文
  special.* 有特定背景的治理任务（如 AGC 名称改名）

**两条语料，别混着用**（用户的判断标准：只要是人写给人看的字，都要查）：

  ① prose_lines()       正文：剔掉代码块、行内代码、HTML 注释、URL、图片语法、HTML 标签
  ② code_comment_lines() 代码块里的**注释**：只取注释部分，代码本身不要

为什么要有 ②：英文文档示例代码里的中文注释同样是问题；注释里写错字、重复字也是问题。
但不能把整段代码当语料——标识符、API 名、字符串里的示例数据不是给人读的，
拿它们做错别字/中文检查全是误报。所以代码块只取注释。

本文件目前是**骨架**：三条规则已注册、已进 /admin「文本检查」分组、默认「调试中」
（default_visible=False，照常跑但只管理员可见、不进导出/日报）。检测逻辑留空，
等口径逐条讨论后再填。空实现必须 return []，不要抛异常，否则当天整轮检查会失败。

三条规则的 key 一旦定下就不要改：分组按 key 前缀归类，改 key 等于新开规则，
历史记录会全部变成「已解决 + 新增」，之前的忽略也会丢。

计划口径（待讨论，先写在这里防跑偏）：
  text.typo     错别字。候选来源：常见错字词典 + 形近字/同音字替换 + 的地得类混淆；
                语料 = 正文 + 代码注释；误报极高，必须像图片检查那样「先出候选，AI 复核只报 real」。
  text.dup_char 重复字/词（「的的」「了了」「进行进行」）。纯正则，误报低；
                语料 = 正文 + 代码注释；要放过正常叠词（看看、慢慢）、英文、数字、表格。
  text.zh_in_en 英文文档里出现中文（截图里的中文是 image.zh_in_en，两者不要混）。
                语料 = 正文 + 代码注释，**不含字符串字面量**（示例数据里的中文多半是有意的）。
                判据可复用 OCR 那套「汉字数 ≥ N 且占可见字符 ≥ x%」。
"""
from __future__ import annotations

import re

from .base import Issue, register

# ── ① 正文：剔掉一切「不是给人读的文字」 ──
FENCE = re.compile(r"^\s*(```+|~~~+)(.*)$")
INLINE_CODE = re.compile(r"`[^`\n]*`")
HTML_COMMENT = re.compile(r"<!--.*?-->")
URL = re.compile(r"(?:https?|ftp)://[^\s)>\]|]+")
IMAGE_SYNTAX = re.compile(r"!\[[^\]]*\]\([^)]*\)")
HTML_TAG = re.compile(r"<[^>]+>")


def prose_lines(text: str):
    """产出 (行号, 剔除代码与链接后的行文本)。"""
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        t = INLINE_CODE.sub(" ", line)
        t = HTML_COMMENT.sub(" ", t)
        t = IMAGE_SYNTAX.sub(" ", t)
        t = URL.sub(" ", t)
        t = HTML_TAG.sub(" ", t)
        yield line_no, t


# ── ② 代码块里的注释 ──
# 按语言选注释符；围栏没写语言时用「auto」：先试 // 与 /* */，再试行首 # / -- ，
# 单行里出现 `color: #fff`、URL 里的 `//` 不会被当成注释（见 _comment_from_line）。
_LANGS = {
    "slash": {"js", "jsx", "mjs", "ts", "tsx", "arkts", "ets", "java", "kt", "kts", "c", "h",
              "cc", "cpp", "cxx", "hpp", "cs", "go", "rs", "rust", "swift", "dart", "php",
              "scala", "groovy", "gradle", "proto", "less", "scss", "css", "json", "jsonc",
              "vue", "solidity", "graphql"},
    "hash": {"py", "python", "sh", "bash", "zsh", "shell", "console", "yaml", "yml", "toml",
             "ini", "conf", "dockerfile", "makefile", "make", "r", "ruby", "rb", "pl", "perl",
             "ps1", "powershell", "awk", "sed", "nix", "cmake"},
    "dash": {"sql", "lua", "hs", "haskell", "ada", "vhdl"},
    "html": {"html", "htm", "xml", "svg", "xaml", "wxml", "markdown", "md"},
}


def _family(fence_info: str) -> str:
    lang = (fence_info or "").strip().strip("{}").split()
    lang = lang[0].lower().rstrip(",") if lang else ""
    for fam, names in _LANGS.items():
        if lang in names:
            return fam
    return "auto"


def _outside_quotes(s: str, idx: int) -> bool:
    """idx 之前的引号数量为偶数 → 认为不在字符串里。"""
    head = s[:idx]
    return head.count('"') % 2 == 0 and head.count("'") % 2 == 0


def _comment_from_line(s: str, fam: str) -> str:
    """从一行代码里取出注释正文；没有注释返回空串。"""
    fams = ("slash", "hash", "dash") if fam == "auto" else (fam,)
    if "html" in fams or fam == "html":
        if "<!--" in s:
            body = s.split("<!--", 1)[1]
            return body.split("-->", 1)[0].strip()
    for one in fams:
        if one == "slash":
            # 行首注释
            if s.startswith("//"):
                return s[2:].strip()
            # 行尾注释：排除 http:// 与 "..." 里的 //
            for m in re.finditer(r"//", s):
                i = m.start()
                if i and s[i - 1] == ":":
                    continue
                if not _outside_quotes(s, i):
                    continue
                return s[i + 2:].strip()
        elif one == "hash":
            if s.startswith("#"):
                head = s.lstrip("#").strip()
                return "" if head.startswith(("!", "include")) else head
            m = re.search(r"(?:^|\s)#\s*(\S.*)$", s)
            if m and _outside_quotes(s, m.start()):
                return m.group(1).strip()
        elif one == "dash":
            if s.startswith("--"):
                return s[2:].strip()
    return ""


def code_comment_lines(text: str):
    """产出 (行号, 注释正文, 围栏语言)：只取代码块里的注释，代码本身不出现。"""
    in_fence = False
    fam = "auto"
    lang = ""
    in_block = False
    for line_no, line in enumerate(text.splitlines(), 1):
        m = FENCE.match(line)
        if m:
            if not in_fence:
                in_fence = True
                lang = (m.group(2) or "").strip()
                fam = _family(lang)
                in_block = False
            else:
                in_fence = False
                lang, fam, in_block = "", "auto", False
            continue
        if not in_fence:
            continue
        s = line.strip()
        if not s:
            continue
        if in_block:                       # 跨行块注释：一直取到 */
            if "*/" in s:
                body = s.split("*/", 1)[0].strip()
                in_block = False
                if body:
                    yield line_no, body, lang
            else:
                yield line_no, s, lang
            continue
        if s.startswith("/*"):             # 块注释开头
            body = s[2:]
            if "*/" in body:
                body = body.split("*/", 1)[0]
            else:
                in_block = True
            body = body.strip().lstrip("*").strip()
            if body:
                yield line_no, body, lang
            continue
        body = _comment_from_line(s, fam)
        if body:
            yield line_no, body, lang


@register("text.typo", label="错别字", severity="warn", default_visible=False,
          ai_hint="错别字检查。只报有把握的用字错误（形近字、同音字、「的/地/得」误用、"
                  "常见词写错），不改专业术语、产品名、代码标识符；"
                  "拿不准的不要报，宁可漏报也不要让同事一条条去驳回。")
def check_typo(doc, ctx):
    """错别字（骨架，未实现）。

    语料（两处都要查）：
      · prose_lines()          正文
      · code_comment_lines()   代码块注释（示例代码里的中文注释写错字同样要改）

    计划：
      1) 词典命中：内置错字表（如「登计」→「登记」）+ 形近字/同音字混淆对；
         「的/地/得」「其它/其他」属风格问题，默认不算错，只在明确语境下报；
      2) 候选 → AI 复核（新增 rubric，如 typo_review），只报 AI 判为 real 的，
         结论落 ai_notes 避免重复调用；
      3) 标题、按钮文案这类短行优先；长段落按句切分后再判断。
    """
    return []


@register("text.dup_char", label="重复字", severity="warn", default_visible=False,
          ai_hint="重复字/词检查。指连续重复的同一个字或同一双字词（「的的」「进行进行」），"
                  "不算正常叠词（看看、慢慢、渐渐）、不算英文/数字重复、不算表格。"
                  "改法是删掉多余的一个字/词。")
def check_dup_char(doc, ctx):
    """重复字/词（骨架，未实现）。

    语料：正文 + 代码注释（注释里「的的」「进行进行」同样算）。

    计划：
      1) 汉字单字重复：逐字比较（Python re 的 \\1 不能匹配汉字类，别用正则反向引用）；
      2) 双字词重复：「(..)\\1」限定两边都是汉字，且排除叠词白名单；
      3) 排除：英文单词、数字、标点成对（「。。」「、、」）、表格分隔行。
    """
    return []


@register("text.zh_in_en", label="英文正文含中文", severity="warn", langs=("en",),
          default_visible=False,
          ai_hint="英文文档里混入中文。要改的是把中文译成英文（不是删掉）；"
                  "中文示例数据、语言/地区代码、品牌并列中英对照属于合法用法，不算问题。"
                  "代码块里的中文**注释**算问题，字符串里的中文示例不算。")
def check_zh_in_en(doc, ctx):
    """英文文档里的中文（骨架，未实现）。

    语料：正文 + 代码注释；**不含字符串字面量**（示例数据里的中文多半是有意的）。

    计划：
      1) 统计每行汉字数与可见字符数，候选条件：汉字数 ≥ 3 且占比 ≥ 2%（与图片侧同口径）；
      2) 代码块只走 code_comment_lines()，字符串里的中文不参与判断；
      3) 排除白名单：中英对照括号内容、zh/cn 语言代码、表格里的中文列；
      4) 与 image.zh_in_en 互不覆盖：那条查截图，这条查正文与注释。
    """
    return []
