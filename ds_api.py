"""docstor —— 华为开发者文档中心（新）接口客户端。

实测（2026-11）可用接口：

  getCenterCatalog     {centerPrefix, language}
      → 板块列表 value.centerNodeList[]，每项含 nodeName / firstLevel2Alias

  getCenterCatalogTree {centerPrefix, language, level2NodeAlias}
      → 整个板块的目录树 value.centerNodeList[]，叶子节点 isLeaf=true 且
        relateDocument=<slug>；节点可嵌套在 catalogTreeList / children 里

  getCenterDocument    {centerPrefix, language, level2NodeAlias, isApi, fileName}
      → 正文 value.content.content（HTML 字符串）+ value.anchorList

取正文的 alias/isApi 规则（实测）：
  · 普通文档    → (板块 alias, isApi=0)，例如 tools-faq / advanced-features
  · API 参考文档 → ("references", isApi=1)
  失败时依次回退 capabilities / getting-started。

注意：图片 CDN 不校验 Referer，但正文接口需要 Referer 头。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import threading

import requests

import ds_fetch

API_BASE = "https://svc-drcn.developer.huawei.com/community/servlet/consumer/cn/documentPortal/"

# 板块 alias → 中文名（用于站点导航标题）
SECTION_NAMES_CN = {
    "release-notes": "版本说明",
    "deveco-studio": "工具",
    "ux-design": "设计",
    "capabilities": "指南与API参考",
    "getting-started": "开发与测试",
    "submission": "上架分发与运营",
    "tools-faq": "问题处理与运维",
    "advanced-features": "最佳实践",
    "industry-solutions": "行业解决方案",
}
SECTION_NAMES_EN = {
    "release-notes": "Release Notes",
    "deveco-studio": "Tools",
    "ux-design": "Design",
    "capabilities": "Guides and API References",
    "getting-started": "Development and Testing",
    "submission": "Listing, Distribution & Ops",
    "tools-faq": "Issue Handling & Operations",
    "advanced-features": "Best Practices",
}

# 板块 → 英文站点使用的 alias（别名同义，取目录树时两者等价）
SECTION_ALIAS_EN = {
    "release-notes": "release-notes",
    "deveco-studio": "deveco-studio",
    "ux-design": "ux-design",
    "capabilities": "capabilities",
    "getting-started": "getting-started",
    "submission": "submission",
    "tools-faq": "dev-faq",       # 英文侧叫 dev-faq
    "advanced-features": "advanced-features",
}


def section_name(alias: str, lang: str) -> str:
    table = SECTION_NAMES_EN if lang == "en" else SECTION_NAMES_CN
    return table.get(alias, alias)


@dataclass
class TreeNode:
    name: str
    slug: str | None
    is_leaf: bool
    is_api: bool
    hided: bool
    depth: int
    sort: int
    parent: str | None
    node_id: str
    alias: str | None = None       # 该节点最近祖先声明的 level2NodeAlias
    children: list["TreeNode"] = field(default_factory=list)


class DocCenter:
    def __init__(self, base: str = API_BASE, center_prefix: str = "doccenter",
                 referer: str = "https://developer.huawei.com/consumer/cn/doc/",
                 timeout: int = 40, retries: int = 3, ua: str = "Mozilla/5.0"):
        self.base = base
        self.center_prefix = center_prefix
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": ua, "Referer": referer,
                                     "Content-Type": "application/json"})
        self._local = threading.local()

    def _sess(self) -> requests.Session:
        """requests.Session 不是线程安全的：抓取时 6 个线程共用一个会互踩连接池。

        每个线程一份 session（复用 keep-alive），既避开竞态也不会串 cookie。
        """
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update(dict(self.session.headers))
            self._local.s = s
        return s

    # ---------- 底层 ----------
    def _post(self, method: str, payload: dict) -> dict:
        last = None
        for attempt in range(self.retries):
            try:
                # 总时长兜底：服务器一点点吐字节时，requests 的单次读超时永远不触发
                return ds_fetch.post_json(self._sess(), self.base + method, payload,
                                          total=self.timeout * 2,
                                          read=min(self.timeout, 30))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"{method} 失败: {last}")

    # ---------- 目录 ----------
    def catalogs(self, lang: str = "cn") -> list[dict]:
        d = self._post("getCenterCatalog",
                       {"centerPrefix": self.center_prefix, "language": lang})
        if d.get("code") != 0:
            raise RuntimeError(f"getCenterCatalog code={d.get('code')}")
        return (d.get("value") or {}).get("centerNodeList") or []

    def raw_tree(self, alias: str, lang: str = "cn") -> dict:
        d = self._post("getCenterCatalogTree",
                       {"centerPrefix": self.center_prefix, "language": lang,
                        "level2NodeAlias": alias})
        if d.get("code") != 0:
            raise RuntimeError(f"getCenterCatalogTree({alias}) code={d.get('code')}")
        return d.get("value") or {}

    def tree(self, alias: str, lang: str = "cn") -> TreeNode:
        """把接口返回的树拍平成 TreeNode 森林的虚拟根。"""
        value = self.raw_tree(alias, lang)
        root = TreeNode(name=alias, slug=None, is_leaf=False, is_api=False,
                        hided=False, depth=0, sort=0, parent=None, node_id="root", alias=alias)

        def walk(items: list[dict], parent: TreeNode, depth: int, api_branch: bool,
                 parent_alias: str | None = None):
            for idx, n in enumerate(items or []):
                name = (n.get("nodeName") or "").strip()
                is_api = api_branch or bool(n.get("apiAtomic")) or bool(n.get("apiType"))
                # 官网每个二级分类都可声明自己的 nodeAlias（如「方案设计」= architecture），
                # 取正文时必须用它，而不是顶层板块 alias，否则 92531031 document not found
                node_alias = (n.get("nodeAlias") or "").strip() or parent_alias or alias
                node = TreeNode(
                    name=name,
                    slug=n.get("relateDocument") or None,
                    is_leaf=bool(n.get("isLeaf")),
                    is_api=is_api,
                    hided=bool(n.get("hided")),
                    depth=depth,
                    sort=idx,
                    parent=parent.node_id,
                    node_id=str(n.get("nodeId") or f"{parent.node_id}-{idx}"),
                    alias=node_alias,
                )
                parent.children.append(node)
                # catalogTreeList 是权威结构；children 是它的降级副本（无 nodeId、名称可能滞后），
                # 两者拼接会造成同名节点重复（如「更多版本」「变更预告」各出现两次）
                kids = (n.get("catalogTreeList") or n.get("children") or [])
                # 「API参考」这一支下的文档都按 API 文档取（英文站叫 "API references"，
                # alias 同为 references；只认中文名会让英文 API 文档全部 92531031）
                is_api_child = (is_api or name in ("API参考", "API references")
                                or node_alias == "references")
                walk(kids, node, depth + 1, is_api_child, node_alias)

        walk(value.get("centerNodeList") or [], root, 1, False, alias)
        return root

    # ---------- 正文 ----------
    def document(self, slug: str, alias: str, lang: str = "cn",
                 is_api: bool = False) -> dict:
        """取正文；按实测规则自动回退 alias / isApi 组合。"""
        if is_api:
            combos = [("references", 1)]
        else:
            combos = [(alias, 0), ("capabilities", 0), ("getting-started", 0)]
        last_code = None
        for a, flag in combos:
            d = self._post("getCenterDocument",
                           {"centerPrefix": self.center_prefix, "language": lang,
                            "level2NodeAlias": a, "isApi": flag, "fileName": slug})
            code = d.get("code")
            if code == 0:
                v = d.get("value") or {}
                content = v.get("content")
                body = content.get("content") if isinstance(content, dict) else (content or "")
                return {"ok": True, "title": v.get("title"), "html": body or "",
                        "anchors": v.get("anchorList") or [],
                        "doc_id": v.get("docId"), "version": v.get("version"),
                        "alias": a, "is_api": flag,
                        # 官网「更新时间」用的就是 displayUpdateTime（UTC，需 +8 显示为北京时间）
                        "updated": v.get("displayUpdateTime") or v.get("updatedDate") or ""}
            last_code = code
        return {"ok": False, "code": last_code}


def flatten(root: TreeNode) -> list[TreeNode]:
    out: list[TreeNode] = []

    def walk(n: TreeNode):
        for c in n.children:
            out.append(c)
            walk(c)

    walk(root)
    return out


def sidebar_lines(nodes: list[TreeNode], lang: str, section: str,
                  link_prefix: str) -> list[str]:
    """生成 docsify _sidebar.md 内容（层级与官网一致）。"""
    lines: list[str] = []
    stack: list[int] = []

    for n in nodes:
        if n.hided:
            continue
        while stack and stack[-1] >= n.depth:
            stack.pop()
        indent = "  " * (len(stack))
        if n.is_leaf and n.slug:
            lines.append(f"{indent}- [{n.name}]({link_prefix}/{n.slug}.md)")
        else:
            lines.append(f"{indent}- {n.name}")
        stack.append(n.depth)
    return lines
