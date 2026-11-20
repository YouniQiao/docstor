"""检查项插件框架。

新增一个检查项只需要：
  1. 在本目录建一个模块，写 @register("your.rule", label="中文名", severity="warn")
     def check(doc, ctx) -> list[Issue]
  2. 在 checks/__init__.py 的模块列表里加一行 import

约定：
  · doc   —— docs 表的一行（dict/Row），含 lang/section/slug/md_path/anchors_json/is_api
  · ctx   —— CheckContext，提供 md 文本、图片目录、DB、外部链接探测缓存
  · Issue —— rule / severity / message / locator(JSON 可序列化) / anchor_text / evidence
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY: dict[str, dict] = {}


@dataclass
class Issue:
    rule: str
    message: str
    severity: str = "warn"
    locator: dict = field(default_factory=dict)
    anchor_text: str = ""
    evidence: dict = field(default_factory=dict)


def register(rule: str, label: str, severity: str = "warn",
             langs: tuple[str, ...] = ("cn", "en"), default_on: bool = True,
             default_visible: bool | None = None):
    """注册一个检查项。

    default_visible=False 表示新规则默认「调试中」：照常检查、数据入库，
    但不出现在全量问题/导出/订阅日报，只在 /admin 里能看（用 /admin/rule/<rule>）。
    """
    def deco(fn):
        REGISTRY[rule] = {"label": label, "severity": severity, "langs": langs,
                          "fn": fn, "default_on": default_on,
                          "default_visible": (default_on if default_visible is None
                                              else default_visible)}
        return fn
    return deco


def rule_label(rule: str) -> str:
    return REGISTRY.get(rule, {}).get("label", rule)


def applicable(rule: str, lang: str) -> bool:
    langs = REGISTRY.get(rule, {}).get("langs", ("cn", "en"))
    return lang in langs


# ---------------- 文档上下文 ----------------
HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)


class DocContext:
    """一篇文档的检查上下文：md 文本、行号→标题、锚点表、图片目录。"""

    def __init__(self, doc: dict, md_path: Path, db=None, session=None):
        self.doc = doc
        self.md_path = md_path
        self.db = db
        self.session = session
        self.text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
        self.anchors = []
        try:
            self.anchors = json.loads(doc.get("anchors_json") or "[]")
        except Exception:  # noqa: BLE001
            self.anchors = []
        self._anchor_by_title = {a["title"]: a["anchorId"] for a in self.anchors}
        # 一次线性扫描取标题（原来用 text[:start].count("\n") 在大文档上是 O(n²)）
        self._headings = []          # [(line_no, level, title)]
        for idx, line in enumerate(self.text.splitlines(), 1):
            s = line.lstrip()
            if not s.startswith("#"):
                continue
            level = len(s) - len(s.lstrip("#"))
            if 1 <= level <= 6 and s[level:level + 1] in (" ", "\t"):
                self._headings.append((idx, level, s[level:].strip()))
        self._head_lines = [h[0] for h in self._headings]

    # 行号 → 所属最近标题（用于定位和跳转）
    def heading_at(self, line_no: int) -> tuple[int, str]:
        import bisect
        if not self._headings:
            return (1, "")
        i = bisect.bisect_right(self._head_lines, line_no) - 1
        if i < 0:
            return (1, "")
        return (self._headings[i][1], self._headings[i][2])

    def anchor_for_line(self, line_no: int) -> tuple[str, str]:
        _lvl, title = self.heading_at(line_no)
        return title, self._anchor_by_title.get(title, "")

    def lines(self):
        return self.text.splitlines()

    @property
    def doc_key(self) -> str:
        return f"{self.doc['lang']}/{self.doc['section']}/{self.doc['slug']}"
