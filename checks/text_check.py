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
import sqlite3
from pathlib import Path

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


# ── ③ 重复字：候选枚举（AI 复核过的才算问题） ──
HAN = re.compile(r"[\u4e00-\u9fff]")
TABLE_SEP = re.compile(r"^[\s|:\-+]+$")

# 正常叠词/惯用重复：直接跳过，省 AI 调用。判不准的一律交给 AI，不靠这张表兜底。
REDUP_OK = set("""
看看 说说 想想 试试 问问 听听 走走 坐坐 等等 讲讲 做做 用用 查查 改改 试试 记记 学学 读读
慢慢 渐渐 稍稍 略略 微微 深深 高高 远远 多多 少少 快快 早早 好好 紧紧 牢牢 稳稳 悄悄 偷偷
默默 缓缓 悠悠 纷纷 统统 通通 常常 往往 时时 处处 人人 家家 天天 年年 月月 日日 步步 层层
重重 点点 滴滴 丝丝 片片 阵阵 声声 句句 条条 个个 种种 件件 次次 遍遍 一一 一一对 各各
通通 处处 处处 时时 往往 慢慢 徐徐 冉冉 潺潺 滚滚 茫茫 苍苍 匆匆 纷纷 个个 阵阵 悠悠
""".split())


# 本身带叠字的固定说法/专业词：整词命中就不算重复字。
# 「端端同步」是分布式数据的正式说法（设备间的端端同步），不是「端」多打了一个。
DUP_TERMS = ("端端同步",)


def _term_spans(ctx: str):
    spans = []
    for t in DUP_TERMS:
        i = ctx.find(t)
        while i >= 0:
            spans.append((i, i + len(t)))
            i = ctx.find(t, i + 1)
    return spans


def in_dup_term(hit: dict) -> bool:
    """重复片段落在某个带叠字的固定说法里 → 不是重复录入。"""
    ctx = hit.get("context") or ""
    i = hit.get("idx", -1)
    if not ctx or i < 0:
        return False
    for a, b in _term_spans(ctx):
        if a <= i and i + 2 <= b:
            return True
    return False


def iter_corpus(text: str):
    """产出 (来源, 行号, 文本)。来源 = prose（正文） / comment（代码注释）。"""
    for line_no, t in prose_lines(text):
        yield "prose", line_no, t
    for line_no, body, _lang in code_comment_lines(text):
        yield "comment", line_no, body


def _redup_hits(line: str):
    """一行里的重复片段：[(列号, 片段)]。双字词重复优先（「进行进行」不再拆成单字）。"""
    out = []
    n = len(line)
    i = 0
    while i < n - 1:
        if (i + 3 < n and HAN.match(line[i]) and HAN.match(line[i + 1])
                and line[i:i + 2] == line[i + 2:i + 4]):
            out.append((i, line[i:i + 4]))
            i += 4
            continue
        if line[i] == line[i + 1] and HAN.match(line[i]):
            out.append((i, line[i:i + 2]))
            i += 2
            continue
        i += 1
    return out


# 「时间间隔」「参数数据」「动作动画」这类相邻两个词共用一个字，中文里极多
# （实测占单字重复候选的 84%）。用文档自身统计出的二字词表判词界，直接跳过，
# 既省 AI 调用又不误杀——词表由 scripts/build_text_words.py 生成。
_WORDS: set[str] | None = None


def word_set() -> set[str]:
    global _WORDS
    if _WORDS is None:
        p = Path(__file__).resolve().parent.parent / "data" / "text_words_cn.txt"
        try:
            _WORDS = set(p.read_text(encoding="utf-8").split())
        except Exception:  # noqa: BLE001
            _WORDS = set()
    return _WORDS


def is_boundary(hit: dict) -> bool:
    """重复片段左右各是一个常见二字词 → 词界重合，不是重复录入。"""
    pat = hit.get("pattern") or ""
    if len(pat) != 2:
        return False
    ctx = hit.get("context") or ""
    i = hit.get("idx", -1)
    if i < 1 or i + 3 > len(ctx):
        return False
    w = word_set()
    return bool(w) and ctx[i - 1:i + 1] in w and ctx[i + 1:i + 3] in w


def find_dup_candidates(text: str, skip_ok: bool = True, skip_boundary: bool = True):
    """找重复字候选（正文 + 代码注释）。返回 [{source,line,col,idx,pattern,context}]。"""
    hits = []
    for source, line_no, line in iter_corpus(text):
        s = line.strip()
        if not s or TABLE_SEP.match(s):
            continue
        for col, pat in _redup_hits(line):
            if skip_ok and pat in REDUP_OK:
                continue
            hit = {"source": source, "line": line_no, "col": col,
                   "idx": col - (len(line) - len(line.lstrip())),
                   "pattern": pat, "context": s[:160]}
            if skip_boundary and is_boundary(hit):
                continue
            if skip_ok and in_dup_term(hit):
                continue
            hits.append(hit)
    return hits


def dup_key(pattern: str, context: str, source: str) -> str:
    """AI 结论的键：同一行同一片段稳定；行文改了会重新判。"""
    import hashlib
    raw = f"{pattern}|{source}|{context}".encode()
    return "dup:" + hashlib.sha1(raw).hexdigest()[:20]


RULE_DUP = "text.dup_char"
NOTE_KIND_DUP = "dup_char_review"


_REAL_CACHE: dict[tuple[str, str], set[str]] = {}


def _real_notes(ctx, kind: str = NOTE_KIND_DUP) -> set[str]:
    """AI 判为 real 的候选键集合。每进程每种 kind 只读一次（结论由 scripts/text_ai_review.py 落库）。"""
    db_path = ""
    try:
        db_path = str(Path(ctx.db.conn.execute("PRAGMA database_list").fetchone()[2]).resolve())
    except Exception:  # noqa: BLE001
        db_path = str(getattr(ctx.db, "path", "") or "")
    ck = (db_path, kind)
    if ck in _REAL_CACHE:
        return _REAL_CACHE[ck]
    s: set[str] = set()
    try:
        conn = sqlite3.connect(db_path or ctx.db.path, timeout=20)
        try:
            for (k,) in conn.execute("SELECT item_key FROM ai_notes WHERE kind=? AND verdict='real'",
                                     (kind,)):
                s.add(k)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        s = set()
    _REAL_CACHE[ck] = s
    return s


@register("text.dup_char", label="重复字", severity="warn", default_visible=False,
          ai_hint="重复字/词检查。指连续重复的同一个字或同一双字词（「的的」「进行进行」），"
                  "不算正常叠词（看看、慢慢、渐渐）、不算英文/数字重复、不算表格。"
                  "改法是删掉多余的一个字/词。")
def check_dup_char(doc, ctx):
    """重复字/词：正文 + 代码注释里的候选，只有 AI 判为 real 才上报。

    流程（与图片检查同一套闸门）：
      1) find_dup_candidates() 出候选（跳过正常叠词；表格分隔行不看）；
      2) scripts/text_ai_review.py 把候选逐条交 AI 判断，结论写 ai_notes(kind=dup_char_review)；
      3) 这里只读库，verdict == 'real' 才报；同一篇文档同一个片段合并成一条。
    为什么不按行报：一篇文档里「的的」出现 3 次，同事只需要一条问题 + 3 个行号。
    """
    text = ctx.text
    if not text:
        return []
    cands = find_dup_candidates(text)
    if not cands:
        return []
    real = _real_notes(ctx)
    hits = [h for h in cands if dup_key(h["pattern"], h["context"], h["source"]) in real]
    if not hits:
        return []
    groups: dict[str, list[dict]] = {}
    for h in hits:
        groups.setdefault(h["pattern"], []).append(h)
    out: list[Issue] = []
    for pattern, hs in groups.items():
        hs.sort(key=lambda x: x["line"])
        out.append(Issue(
            rule=RULE_DUP,
            message=f"重复字：「{pattern}」",       # 不写条数：条数变了不该重新开问题
            severity="warn",
            locator={"line": hs[0]["line"], "pattern": pattern,
                     "lines": [h["line"] for h in hs][:20]},
            evidence={"hits": hs[:20], "total": len(hs)},
        ))
    return out


RULE_ZH = "text.zh_in_en"
NOTE_KIND_ZH = "zh_in_en_review"
# 链接地址不算文字：`[文档](https://…/zh/…)` 只保留链接文字
LINK_ADDR = re.compile(r"\]\([^)]*\)")

MIN_HAN = 2          # 一行至少 2 个汉字就算候选（「Error code changed兼容」这种 2 字残留也要抓）
# 英文正文动辄一两百字符，中文残留往往只有「兼容」两个字（占比 <1%），
# 用占比门槛会漏掉；好在英文语料极干净（全站去掉代码与链接地址后只剩十几行含汉字），
# 直接不限占比、全部交 AI 判，反而更准也更便宜。
MIN_RATIO = 0.0


def find_zh_candidates(text: str, min_han: int = MIN_HAN, min_ratio: float = MIN_RATIO):
    """英文文档里的中文候选：正文 + 代码注释，字符串字面量不参与。

    返回 [{source,line,text,han,ratio}]。统计的是「这一行里汉字有多少、占比多少」，
    阈值与截图那条（image.zh_in_en）保持一致，判据统一。
    """
    out = []
    for source, line_no, line in iter_corpus(text):
        s = line.strip()
        if not s:
            continue
        t = LINK_ADDR.sub("]", s)          # 去掉链接地址，只留链接文字
        han = len(HAN.findall(t))
        if han < min_han:
            continue
        visible = sum(1 for ch in t if not ch.isspace())
        ratio = han / visible if visible else 0.0
        if ratio < min_ratio:
            continue
        out.append({"source": source, "line": line_no, "text": s[:200],
                    "han": han, "ratio": round(ratio, 3)})
    return out


def zh_key(text: str, source: str) -> str:
    """AI 结论的键：同一行同一来源稳定；改写后重新判。"""
    import hashlib
    raw = f"{source}|{text}".encode()
    return "zh:" + hashlib.sha1(raw).hexdigest()[:20]


@register("text.zh_in_en", label="英文正文含中文", severity="warn", langs=("en",),
          default_visible=False,
          ai_hint="英文文档里混入中文。要改的是把中文译成英文（不是删掉）；"
                  "中文示例数据、语言/地区代码、品牌并列中英对照属于合法用法，不算问题。"
                  "代码块里的中文**注释**算问题，字符串里的中文示例不算。")
def check_zh_in_en(doc, ctx):
    """英文文档里的中文：正文 + 代码注释，AI 判为 real 才报，一篇文档一条。

    为什么不按行报：一篇英文文档里如果混了整段中文，同事要的是「这篇有问题、这些行」，
    而不是十几条几乎一样的行级问题。

    与 image.zh_in_en 的分工：那条查**截图里的中文**，这条查**正文与代码注释里的中文**，
    同一篇文档两条都可能命中，这是两个不同的载体，不算重复。
    """
    if (doc.get("lang") or "") != "en":
        return []
    text = ctx.text
    if not text:
        return []
    cands = find_zh_candidates(text)
    if not cands:
        return []
    real = _real_notes(ctx, NOTE_KIND_ZH)
    hits = [h for h in cands if zh_key(h["text"], h["source"]) in real]
    if not hits:
        return []
    hits.sort(key=lambda x: x["line"])
    return [Issue(
        rule=RULE_ZH,
        message="英文文档里出现中文（正文或代码注释）",
        severity="warn",
        locator={"line": hits[0]["line"], "lines": [h["line"] for h in hits][:20]},
        evidence={"hits": hits[:20], "total": len(hits)},
    )]
