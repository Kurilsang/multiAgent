# SPEC-0002：技能在线目录（爬取服务 + 一键安装市场）

状态：已定稿（Q1–Q6 逐条确认）· 日期：2026-09-23
关联：[SPEC-0001](0001-skill-market.md)（技能市场 v1，本 spec 兑现其二期候补第一条）、[ADR-0001](../adr/0001-state-machine-for-react-loop.md)、[ADR-0002](../adr/0002-dual-layer-termination.md)

## 目标

把技能市场从「知道 URL 才能装」升级为「浏览在线目录 → 点一下 → 确认 → 装好」：

1. **独立爬取服务 `services/catalog/`**：抓取主流技能平台的目录（名称/描述/安装坐标/热度/审计徽标），SQLite 缓存，支持一次爬取 + 后台定时刷新
2. **主服务代理 `/market/*`**：UI 同源免 CORS，主服务是 UI 唯一对话面
3. **WebUI 双子面板**：市场视图拆「已安装 / 在线浏览」，在线面板 = 搜索 + 平台筛选 + 分页列表
4. **一键安装**：点「安装」→ 单次确认卡（详情 + SKILL.md 预览 + 审计徽标）→ 复用既有安装校验闸门落盘
5. **隔离标注（硬性）**：爬取服务是**唯一与外网目录交互的组件**，README/AGENTS/spec 显式标注信任边界与升级到独立部署的路径

## 非目标

- SKILL.md 正文全文索引（Q5 明确砍掉；名称/描述级检索够用）
- 多步审核/审批流（Q3 明确轻量：单次确认即可，技能主体是提示词）
- 已验证源免确认白名单、正文级语义搜索（二期）
- 技能可执行脚本的安装（沿用 SPEC-0001：纯提示词包，附带脚本/资源文件丢弃并在报告中警告）
- 多用户 / 鉴权 / 限流治理（单用户网关定位不变）

## 事实地基（平台抓取面实测，2026-09-23）

| 平台 | 可编程面 | 安装坐标 | 信任信号 | v1 取舍 |
|---|---|---|---|---|
| **skills.sh**（Vercel） | 官方 API `https://skills.sh/api/v1/`：榜单（all-time/trending/hot）/ 搜索（模糊+语义）/ 详情（**含 SKILL.md 全文与文件树**、内容 hash）/ **审计**（Socket、Snyk、Runlayer、ZeroLeaks 等，pass/warn/fail + riskLevel） | 条目 `installUrl` = GitHub 仓库 + slug 子路径 | 审计端点 + `isDuplicate` 重复检测 | ✅ 接入（目录 + 预览 + 审计徽标 + 直装） |
| **LobeHub Market**（market.lobehub.com） | 市场 API（`@lobehub/market-cli` 同款协议）：注册制（限 5 次/30 分钟/IP）→ 搜索 JSON（identifier/描述/分类/installCount/stars/isValidated/tags/github.url） | ZIP 包下载（**含脚本资源**） | `isValidated` 标志 | ✅ 接入（目录 + 预览 + 直装，脚本丢弃） |
| **ClawHub**（openclaw 生态） | 站点可浏览，API 未文档化（Convex 后端）；Snyk 2026 报告其为恶意技能重灾区 | `clawhub` CLI | 有 /audits 页 | ❌ v1 不接，adapter 接口预留 |
| anthropics/skills 等 GitHub 仓库 | 无目录 API | git clone | — | 由 skills.sh 索引覆盖；手装走既有 Git URL 安装 |

**探针结论（2026-09-23，已回填）**：
- skills.sh API 匿名返回 **401 authentication_required（OIDC-only）**→ 走 HTML 降级；实测页面内嵌 Next.js RSC 载荷含完整榜单 JSON（source/skillId/name/installs/isOfficial），解析载荷而非 DOM；请求需浏览器 UA + 跟随重定向
- LobeHub：端点自 `@lobehub/market-sdk 0.40.1` 源码反查确认——`GET /api/v1/skills{?q,page,pageSize,sort,order}`、`GET /api/v1/skills/{id}/download`（ZIP）、`POST /api/v1/clients/register`（限 5 次/30 分钟/IP）、`POST /oauth/token`（client_credentials + HS256 JWT 断言，iss=sub=clientId）；搜索/下载均需 Bearer
- 真实注册握手、ZIP 下载与安装全流程归人工验收（#30）

## 架构与隔离边界

```
┌─ 内网信任域 ──────────────────────────┐     ┌─ 隔离区（外网边界）─────────┐
│ WebUI ── 同源 ──► 主服务 app/server   │ ──► │ services/catalog/（独立进程）│
│   /skills/install（校验闸门+落盘）    │◄── │  adapters → skills.sh API   │
│   /market/*（纯代理）                 │     │             → LobeHub API  │
└───────────────────────────────────────┘     │  SQLite 缓存 + 刷新器       │
                                              └─────────────────────────────┘
```

- **`services/catalog/` 是唯一与外网交互的组件**（隔离标注，Q1）：不持有 LLM Key、不持有技能包目录写权限、不信任外网数据（入库前清洗：字段长度上限、纯文本化）
- 主服务新增「目录包获取」安装路径后，**主服务默认零外网**（既有 Git URL 手装除外）；目录浏览、SKILL.md 预览、包下载全部经爬取服务
- **升级路径（Q1 预留）**：爬取服务可原样搬出为独立部署单元（置于网关之外），主服务仅需改 `CATALOG_BASE_URL`；两服务间只存在 `/internal/*` 窄接口合同，无共享状态文件
- 技术栈：FastAPI + httpx + SQLite（单文件 `catalog.db`），与主仓库同技术栈但**独立进程、独立 requirements**

## 目录数据与刷新（Q2）

- 统一条目 schema（各 adapter 归一）：`id`、`name`、`description`、`source`（平台）、`origin`（owner/repo 或市场标识）、热度（installs/stars）、`tags`、`detail_url`、`install_ref`（目录包引用）、审计摘要（如有）、`is_duplicate`
- SQLite 落盘 `catalog.db`：条目表 + 源状态表（`last_refresh`、条目数、最近错误）
- **刷新策略**：一次爬取（`POST /internal/refresh` 即时执行）+ 后台定时刷新（`CATALOG_REFRESH_HOURS`，默认 6h）+ UI「刷新目录」按钮；接口返回「目录更新于 X 前」
- **降级**：源不可达 / 爬取服务宕 → 返回缓存 + `stale` 标志；主服务对 UI 给中文降级提示，目录功能可关（`CATALOG_BASE_URL` 为空时隐藏入口）

## 目录包获取与安装流（Q3）

- 点击「安装」→ **单次确认卡**：名称、描述、来源徽标、origin、依赖工具（预览的 frontmatter）、热度、审计徽标（skills.sh：各审计方 pass/warn/fail + riskLevel；LobeHub：isValidated）、「查看 SKILL.md」展开全文预览 → 「确认安装」
- 确认后 `POST /skills/install {"source": "catalog", "catalog_id": ..., "source_platform": ...}`：
  1. 主服务向爬取服务 `GET /internal/pack/{id}` 要**文件集**（skills.sh = 详情端点 files；LobeHub = ZIP 解包后的 SKILL.md 集）
  2. 走既有校验闸门（frontmatter 三字段白名单、长度上限、工具依赖校验）
  3. **只落 SKILL.md 纯提示词**，包内脚本/资源文件丢弃并在安装报告中警告「附带文件已丢弃（纯提示词边界）」
  4. 落盘/重名策略/报告复用 `install_packs`（installed/skipped/invalid）
- 既有 Git URL / 本地导入手装路径不动（兼容入口）
- 确认卡是唯一人闸（轻量，不做多步审批）；审计徽标只做提示不做担保

## HTTP API 增量

主服务（代理 + 安装扩展）：

```
GET  /market/sources           各源状态（last_refresh / stale / 条目数 / 最近错误）
GET  /market/search            ?q=&source=&page=&page_size= → 统一条目分页（含审计摘要）
GET  /market/detail?id=        详情（SKILL.md 全文预览 + 审计明细）
POST /market/refresh           {"source"?: "..."} → 触发一次爬取，返回结果摘要
POST /skills/install           新增 {"source": "catalog", "catalog_id": ..., "source_platform": ...}
```

爬取服务（`/internal/*` 窄接口，不对公网暴露）：

```
GET  /internal/search | /internal/detail | /internal/pack/{id} | /internal/sources
POST /internal/refresh
GET  /health
```

## WebUI（Q4）

- 市场视图拆两个子面板：**「已安装」**（现列表原样）/ **「在线浏览」**
- 在线浏览面板：搜索框 + 平台筛选下拉（skills.sh / LobeHub）+ 分页列表（名称/描述/来源徽标/热度/审计徽标）+ 每条「安装」按钮 + 顶部「刷新目录」+「目录更新于 X 前」陈旧标注
- 确认卡（单次）：详情 + 「查看 SKILL.md」展开预览（预览超时降级「预览不可用」）+ 确认/取消
- 安装结果复用现有逐条报告（installed/skipped/invalid + 丢弃警告）
- 仍不引入框架/构建步骤/markdown 库（预览以 `<pre>` 呈现原文）

## 配置增量（同步 `.env.example` 与 README）

主服务：

```
CATALOG_BASE_URL=http://127.0.0.1:8100   # 爬取服务地址；留空 = 在线目录功能关闭并隐藏入口
```

爬取服务 `services/catalog/`：

```
CATALOG_PORT=8100
CATALOG_DB=services/catalog/catalog.db
CATALOG_REFRESH_HOURS=6
CATALOG_SOURCES=skills-sh,lobehub        # 启用的源（逗号分隔）
```

## 信任边界

- 爬取服务零写权限、零密钥持有（LobeHub 注册凭证除外，单独存 `catalog.db`，仅用于其 API）
- 目录元数据与包内容**一律视为不可信输入**：入库清洗（长度上限、纯文本化、拒绝控制字符），UI 渲染只走 `textContent`
- 安装闸门不放松（SPEC-0001 同一套校验）；审计徽标是提示不是背书——ClawHub 的教训（Snyk：抽样 36% 提示注入）说明任何目录都可能藏毒
- `/internal/*` 只监听 `127.0.0.1`（配置可覆盖，独立部署时置于网关外 + 网络隔离）
- **实现裁定（21 号工单，较 SPEC-0001 的闸门适配）**：目录包安装对第三方 frontmatter 采用**清洗不拒绝**——未知字段一律丢弃（字段值根本不进上下文，注入面不变）；name 规范化为合法技能名（可作 `/` 前缀点名）、description 截到清单预算（100 字）、正文超长截断并标注；**仅缺 name / 空正文拒绝**。若按自家严格格式拒装，anthropics/skills 等真实生态包（普遍长描述、带空格名）会被整体挡在门外（用户实测案例：find-skills 描述 200 字被拒）。生态常见的 `allowed-tools` 等字段丢弃、工具依赖留空（外部工具名与本注册表不通，用户可在包内补 `tools` 声明）。自有技能（create_skill / 手装）仍走严格解析

## 验收标准

1. `services/catalog` 独立启动；一次爬取后 SQLite 落盘，`/internal/search` 分页返回统一条目
2. 后台定时刷新生效；源不可达降级读缓存并标 `stale`；刷新失败有中文错误记录
3. 主服务 `/market/*` 代理可用；爬取服务未启动时 UI 隐藏入口（或中文降级），主服务不崩
4. 在线浏览面板：搜索 / 平台筛选 / 分页 / 陈旧标注可用
5. 点「安装」→ 单次确认卡（含 SKILL.md 预览与审计徽标）→ 确认后安装成功出现在「已安装」；附带脚本丢弃并警告
6. skills.sh API 鉴权姿势探针结论（匿名可用 / OIDC-only 走 HTML 降级）写入实现票并回填本 spec
7. **隔离标注落地**：README/AGENTS 明确 `services/catalog/` = 外网边界组件与独立部署升级路径
8. 测试全绿：爬取适配器用 fixture JSON/HTML/ZIP（不发真实请求）；代理层用 fake 爬取服务；现有 111 用例不回归

## 已定决策记录（本轮问答）

| 决策点 | 结论 |
|---|---|
| 服务形态 | 同仓 `services/catalog/` 独立 FastAPI + SQLite；预留升级独立部署；**文档显式做外网隔离标注**（Q1 a + 隔离要求） |
| 刷新 | 一次爬取 + 后台定时刷新 + 手动刷新 + 陈旧标注 + 降级读缓存（Q2） |
| 安装确认 | 单次确认 + 详情（来源/坐标/依赖/预览/审计徽标），轻量不过度严格（Q3） |
| UI | 市场视图双子面板「已安装 / 在线浏览」（Q4 a） |
| 检索深度 | 名称 + 描述级（Q5 a）；正文全文索引不做 |
| 交付 | 老规矩：本 spec → 工单（017 起编号）→ 按票实现拆提交 + GitHub 回填（Q6） |

## 二期候补

- ClawHub 适配器（探到 API 后）+ LobeHub ZIP 版本钉选
- 已验证源免确认白名单；skills.sh 审计徽标进列表行内展示与自动避雷（fail 自动禁装）
- 正文全文/语义检索；`/` 弹层混入在线目录（选中即装的交互另设计）
- 爬取服务独立部署（网关外）、多源限流治理、`isDuplicate` 去重聚合视图
