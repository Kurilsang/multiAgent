# SPEC-0001：技能市场与技能自主调用

状态：已定稿（grilling 会话逐条确认）· 日期：2026-09-22
关联：[ADR-0001](../adr/0001-state-machine-for-react-loop.md)、[ADR-0002](../adr/0002-dual-layer-termination.md)、CONTEXT.md「工具」「技能」

## 目标

把技能从 agent.py 内的硬编码 dataclass 升级为文件化、可安装、可自产的能力包，并让两条通道（聊天 / Agent 任务）都能调用技能：

1. **文件化**：`skills/<名称>/SKILL.md`，纯提示词包（正文 + 工具依赖声明），不含可执行脚本
2. **市场**：从主流技能平台（anthropics/skills、skills.sh、ClawHub、LobeHub 等全部基于 SKILL.md + GitHub 分发的生态）与本地导入安装
3. **自主调用**：模型经清单 + 元工具按需激活技能，两条通道都支持
4. **自产**：Agent 经 `create_skill` 工具新增技能，校验通过立即生效
5. **WebUI**：聊天框 `/` 弹出可搜索技能列表（Tab 选中插入完整技能名）；技能市场独立视图管理已装技能

## 非目标

- 技能携带可执行脚本（市场安装等于安装代码，信任问题另一个量级）
- 技能内容自动审计（见「信任边界」）
- 打开本地文件目录（部署目标是服务器，浏览器无权开 explorer）
- CLI 侧任何改动
- 技能版本管理 / 更新检查 / 依赖传递解析

## 技能包格式

```
skills/
  时间报告/
    SKILL.md
  <名称>/
    SKILL.md
```

`SKILL.md` = YAML frontmatter + Markdown 正文：

```markdown
---
name: 时间报告
description: 涉及当前日期时间推算时的多步推理流程
tools:
  - get_current_time
  - calculator
---

（正文 = 注入上下文的 guide，即原 Skill.guide）
```

约束：

- `name`：`^[\w\u4e00-\u9fff-]{1,32}$`，与目录名一致；正文长度上限 8000 字符
- `description`：≤ 100 字符，进清单的一句话描述
- `tools`：必须全部已在工具注册表，否则**拒绝加载/安装/写入**，错误信息回灌给调用方自愈
- 纯提示词包：frontmatter 之外的字段一律拒绝（防恶意 frontmatter 注入）

现有 `demo_time_report_skill()` 迁移为首个文件技能。

## 技能注册表（新模块 `app/skills.py`）

- `SkillRegistry`：加载 `SKILLS_DIR` 下全部技能包 → `Skill` 对象，与工具注册表同构
- 文件态是唯一事实源；启停状态存 `skills/.installed.json`（name → {enabled, source}），不碰技能包本身
- 增删改（安装 / 卸载 / 启停 / 自产）后内存注册表即时重载，无需重启

## 调用模型（本 spec 的核心）

技能是提示词不是函数，「调用」= 注入。采用**动态清单 + 按需展开**：

1. **动态清单**：每次请求从注册表现场生成（安装/卸载/启停即时反映），以紧凑格式（名称 + 一句话描述）追加到 system prompt。清单条数上限 `SKILLS_CATALOG_MAX`（默认 30）：超出部分不进清单，改为提示模型「还有 N 个技能，可用 search_skills 检索」
2. **`use_skill(name)`**：普通注册表工具，执行结果 = 技能正文。正文经观察值通道回灌，**引擎循环零改动**——激活技能就是一次普通的工具调用
3. **`search_skills(query)`**：清单被截断时的检索入口，返回匹配的名称 + 描述
4. **`create_skill(name, description, guide, tools)`**：写 `skills/<name>/SKILL.md` 并重载注册表；校验失败（重名、非法名、tools 未注册、超长）拒绝写入，错误作为观察值回灌，模型自行修正重试——复用引擎现有自愈机制，不设待审流程

三个元工具随引擎 schemas 注入，对模型与普通工具无异。

## 双通道语义

| 入口 | 技能来源 | 工具 |
|---|---|---|
| `/agent/stream`（任务） | 动态清单 + use_skill 自主激活 | 全部注册表工具 |
| `/chat/stream`（聊天） | 动态清单注入，**模型按提示词自主判断**是否 use_skill | 元工具 + 已激活技能声明的依赖工具 |
| `/` 前缀消息（如 `/时间报告 3天后是几号`） | 显式点名 → 确定性路由到 Agent 通道并预激活该技能 | 全部注册表工具 |

聊天通道为支持技能激活新增**有界迷你循环**（`CHAT_MAX_TOOL_TURNS`，默认 4）：`use_skill` 及被激活技能的依赖工具可在本轮内执行；只有最终 assistant 文本写入主对话历史，技能正文与工具中间态是上下文通道产物，不落历史——与思考链的处理对齐。

`/` 前缀在 UI 上只是「插入完整技能名」的输入加速（见 WebUI），语义发生在服务端：识别已注册技能名前缀即走任务通道。

## 安装与来源

统一到**通用 Git 适配器**：主流平台（anthropics/skills、skills.sh 收录仓库、LobeHub 发布、ClawHub 镜像）的技能包都是「GitHub 仓库里的 SKILL.md 目录」，`git clone` 后扫描 `**/SKILL.md` 即完成安装。

- `POST /skills/install`：`{"source": "git", "url": "https://github.com/anthropics/skills", "subpath": "..."}` 或 `{"source": "local", "path": "..."}`
- **预设源**：内置平台清单（名称 + 仓库 URL）随 UI 下拉展示，一键安装，免用户找 URL
- 本地导入：服务器侧目录路径（部署到服务器后即服务器文件系统）
- 平台原生搜索 API（skills.sh / ClawHub 在线检索）→ **二期**，v1 用预设源 + Git URL 覆盖

## HTTP API 增量

```
GET    /skills                 列表（元数据 + enabled + source）
GET    /skills/{name}          查看正文（渲染 SKILL.md）
POST   /skills/install         安装（git url / local path / 预设名）
POST   /skills/{name}/enable   启用
POST   /skills/{name}/disable  禁用
DELETE /skills/{name}          卸载（删目录 + 登记项）
```

安装/卸载/启停与 `/chat` 共用线程锁串行化（注册表热重载与进行中的流式请求互斥）。

## WebUI（延续无框架单文件，页内 tab 切换）

- **聊天视图**：输入框检测 `/` 触发技能弹层（按名称/描述模糊搜索），Tab 或 Enter 选中后把完整技能名插入输入文本；消息发送后按上表路由，任务类消息沿用现有时间线展示并标注来源技能
- **技能市场视图**（顶栏按钮跳转）：已装技能列表（名称 / 描述 / 来源 / 启停开关 / 删除）、点开查看正文、安装面板（预设平台下拉 + 粘贴 Git URL + 本地路径）

## 配置增量（同步 `.env.example` 与 README）

```
SKILLS_DIR=skills                # 技能包目录，相对项目根
SKILLS_CATALOG_MAX=30            # 清单注入条数上限，超出走 search_skills
CHAT_MAX_TOOL_TURNS=4            # 聊天通道迷你循环上限
```

## 信任边界

生态已有实测恶意技能：Snyk 2026 年报告 ClawHub 抽样 36% 含提示注入、1467 个恶意载荷。技能 = 注入 system prompt 的指令，安装第三方技能即引入提示注入面。v1 措施：严格 frontmatter schema 校验、字段白名单、正文长度上限、工具依赖必须在注册表（技能本身永远拿不到任意代码执行）、禁用/删除兜底。自动内容审计明确为非目标，风险由「内部网关 + 单用户」的信任模型兜底。

## 验收标准

1. `skills/` 目录加载生效，demo 技能迁移为文件后全链路回归
2. Agent 任务中模型自主 `use_skill` 激活技能并完成依赖工具调用
3. Agent 任务中模型 `create_skill` 自产技能，落盘立即生效；声明未注册工具时被拒并自愈重试
4. 聊天消息不含 `/` 前缀时，模型可自主判断并激活技能（迷你循环），仅最终文本入主对话
5. `/时间报告 ...` 路由到任务通道并预激活
6. 聊天框 `/` 弹层可搜索，Tab 选中插入技能名
7. 市场视图可从预设平台 / Git URL / 本地路径安装，启停删即时生效，无需重启
8. 现有测试全绿；新模块（注册表 / 加载器 / 安装器）用 tests/fakes.py 缝隙覆盖，不发真实请求

## 已定决策记录（grilling 会话）

| 决策点 | 结论 |
|---|---|
| 技能形态 | SKILL.md 目录格式，纯提示词包 |
| 货源 | 主流平台经通用 Git 适配器 + 预设源；本地导入；平台原生 API 二期 |
| 注入机制 | 动态清单 + use_skill 按需展开（用户确认清单是核心点，动态生成解决清单膨胀） |
| 通道 | 聊天通道注入清单、模型自主判断（Q4 选 b）；`/` 前缀显式点名走任务通道 |
| 自产 | create_skill 立即生效，校验失败回灌自愈，无待审 |
| 打开目录 | 砍掉（部署到服务器） |
| `/` 交互 | 弹层搜索 + Tab 插入技能名，非召唤市场；市场走按钮跳转 |
| CLI | 不动 |

## 二期候补

- skills.sh / ClawHub / LobeHub 原生搜索与一键安装（在线目录浏览）
- zip 上传安装、技能版本与更新检查
- 多 Agent 协作下的技能共享与按 Agent 装配
