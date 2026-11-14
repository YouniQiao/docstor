# docstor 设计决策记录

> 名称寓意：docs + doctor = 文档医生。镜像华为开发者文档中心，并做质量检查。

## 1. 数据源（已实测，2026-11-12）

新开发者文档中心：https://developer.huawei.com/consumer/cn/doc/doccenter

接口（POST，需 `Referer: https://developer.huawei.com/consumer/`）：

```
BASE = https://svc-drcn.developer.huawei.com/community/servlet/consumer/cn/documentPortal/

getCenterCatalog      {centerPrefix:"doccenter", language}                  → 板块列表 + firstLevel2Alias
getCenterCatalogTree  {centerPrefix:"doccenter", language, level2NodeAlias}  → 整棵目录树（含全部文档叶子 relateDocument）
getCenterDocument     {centerPrefix, language, level2NodeAlias, isApi, fileName} → 正文
getDocumentById       {objectId, language}                                   → 正文（按 docId）
```

正文形态：`value.content.content` 为 **HTML 字符串**；另有 `anchorList[]`（anchorId/level/title），
`title/docId/lang/domain/version/status` 等字段。图片为 HTML 内远程 URL（华为 CDN）。

### 文档量（实测，一回请求即得整个板块的树）

| 板块 | cn alias | cn 篇数 | en 篇数 |
|---|---|---|---|
| 版本说明 | release-notes | 1,203 | 696 |
| 工具 | deveco-studio | 1,130 | 597 |
| 设计 | ux-design | 148 | 133 |
| 指南与API参考 | capabilities | 7,419 | 7,857 |
| 开发与测试 | getting-started | 1,924 | 589 |
| 上架分发与运营 | submission | 1,111 | 244 |
| 问题处理与运维 | tools-faq (en: dev-faq) | 4,323 | 1,514 |
| 最佳实践 | advanced-features | 367 | 121 |
| 行业解决方案 | industry-solutions | 104 | —（en 无此板块）|
| **合计** | | **17,729** | **11,751** |

## 2. 已定决策

| 日期 | 决策 | 说明 |
|---|---|---|
| 2026-11-12 | **英文源 = 新中心 en（方案 A）** | 不混用旧英文站（`/consumer/en/doc/harmonyos-guides/…`）。旧站仅作历史参考，不做双源比对。结构一次到位，与将来官网上线一致。风险：英文未上线期若新中心缺稿，docstor 不会自动发现（接受）。 |

## 3. 待定

1. 图片存哪：A 不落地（CDN 外链，站点 ~1GB）/ B 扩盘（约 84GB）/ C 转 WebP 压缩（~25–30GB）
2. docscheck 的去留：并存（新端口+子域）还是最终替代（可释放 77GB）
3. 检查项优先级：建议第一批 = 链接类（404/历史版本/锚点/跨语言）+ 图片中文 OCR + 错别字/重复字规则类；AI 类（语病、图文关联）第二批
4. POC 是否启动：建议先抓 capabilities（7,419 篇）→ docsify 渲染 → 链接检查 → 右侧问题面板

## 4. 架构草案

```
docstor/
  docs/cn/<level2NodeAlias>/<level3>/<slug>.md     # 9 板块
  docs/en/<level2NodeAlias>/<level3>/<slug>.md     # 8 板块，路径对称
  docs/_sidebar/{cn,en}/<section>.md               # 每板块一个侧边栏（17.7k 文档不能单文件）
  index.html + docsify 配置（loadSidebar / alias / search 分片）
  checks/                                          # 插件化检查
  db: docs / issues / runs / ai_notes / user_prefs
```

检查项插件接口：

```python
class Check:
    key: str                 # 如 "link.dead"
    label: str
    def applicable(self, doc) -> bool: ...
    def run(self, doc) -> list[Issue]: ...
```

问题模型：

```sql
issues(id, doc_key, kind, locator, evidence, severity,
       first_seen, last_seen, status, resolved_at)
-- 首次全量 = 基线；增量只 INSERT / UPDATE(last_seen)；每日复核改 status
```

展示：
- 独立页：全量问题 + 多维筛选（语言/类型/识别日期/板块），**必须分页**
- docsify：左侧章节标题挂问题数徽标；右侧自定义面板列出本页问题，点击定位到锚点，面板内可按类型过滤
- GitCode 登录后保存个人筛选偏好（复用 docscheck 的 OAuth + user_prefs）

## 5. 已知硬约束

- 磁盘：197G 总，已用 130G（69%），剩 60G；docscheck 自身占 77G。全量图片 84GB 放不下，需先定图片方案
- AI 成本：29,480 篇全量 LLM 检查成本高，须"规则先筛 + AI 只跑可疑段落 + 增量优先"
- docsify 规模：单板块最大 7,857 篇，侧边栏与搜索必须分片
