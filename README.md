# multiagent

针对内部网关场景特化的 Agent 项目。在多厂商大模型对话（DeepSeek / GLM / MiniMax）之上，引入 ReAct 式 Agent 自主执行循环：思考→行动→观察→判断，双入口全链路流式；第三步落地技能市场：SKILL.md 文件化技能包、动态清单与双通道技能调用；第四步接入技能在线目录：独立爬取服务抓目录，浏览点装一键完成；第五步 MCP 市场与 MCP 工具接入已落地（连接定义 + 启动装配 + 观察值续读 + 管理面 + 官方 MCP Registry 目录 + WebUI 统一市场与 MCP 单次确认卡 + 动态 OAuth 授权流）；第六步多 Agent 协作已落地（编排器派发 + 并行子任务 + critic 收尾 + 轨迹树与审计日志）；第七步多会话持久化与断连续流进行中（会话落库 + 任务生命周期与 HTTP 连接解耦已落地，见 [docs/specs/0005](docs/specs/0005-session-persistence.md)）。

## 特性

- **统一接入**：三家厂商均通过 OpenAI 兼容 API 接入，一个 SDK 覆盖全部
- **上下文支持**：内存维护单会话历史，自动按上限截断（保留 system + 最近 N 条）
- **Agent 任务**：`/agent` 发起自主多步任务——状态机编排（见 docs/adr/0001、0003）、双层终止（见 docs/adr/0002）、死循环止损、partial 进展摘要；多 Agent 编排：`dispatch_task` 派发子任务并行执行（动态装配、深度限一层）、critic 收尾审查（不合格回炉一次）、随时叫停、全轨迹审计日志
- **工具 + 技能**：工具走 function calling 通道；技能为文件化提示词包（`skills/<名称>/SKILL.md`），动态清单 + `use_skill` 按需激活，Agent 可用 `create_skill` 自产技能
- **技能市场**：预设平台 / Git URL / 本地目录安装（覆盖 anthropics/skills、skills.sh 等 SKILL.md 生态），WebUI 管理启停/删除/查看，热生效无需重启
- **技能在线目录**：独立爬取服务（`services/catalog/`，隔离区）抓取 skills.sh / LobeHub / 官方 MCP Registry 目录——SQLite 缓存 + 定时刷新 + 陈旧标注；市场页「在线浏览」搜索翻页（技能 / MCP 按资产类型过滤）→ 单次确认卡（manifest 预览 + 审计徽标）→ 一键安装；外网元数据交互集中在隔离区（可独立部署升级），MCP 会话流量由主服务直连远程端点（见 [docs/specs/0003-mcp-market.md](docs/specs/0003-mcp-market.md)）
- **MCP 市场与 MCP 工具**：MCP 服务安装为声明式连接定义（`mcp/servers.json`，密钥只以 `${VAR}` 占位写入 `.env`，手写同权、重载生效）；确认卡展示命令原文/endpoint + 域名 + 密钥表单（isSecret 走密码框），单次确认即安装并收集密钥；启用服务的工具以 `mcp__<服务短名>__<工具名>` 灌入工具注册表，聊天/任务两通道即装即用；观察值视图截断 + `read_tool_result` 旁存续读；OAuth 保护的远程服务走动态授权流（`auth: oauth`：授权码 + PKCE 终端贴码 / 无头设备码，token 独立存储自动刷新）
- **双通道调用**：任务通道全量工具 + 自主激活；聊天通道注入清单、模型自主判断是否借助技能；`/技能名 …` 显式点名确定性升级为任务
- **运行时切换**：CLI 中 `/model glm` 随时切换厂商；API 请求中传 `provider` 字段
- **思考链展示**：`reasoning_content` 字段（GLM / DeepSeek 思考模型）与内联 `<think>` 标签（MiniMax M 系列）统一转写，WebUI / CLI / 任务时间线均实时展示，不污染对话历史
- **对话导出**：WebUI 一键导出 Markdown（含任务轨迹与错误，可复制剪贴板）、CLI `/export`、HTTP `GET /export`（markdown / json）
- **全链路流式**：聊天逐 token SSE；Agent 任务思考逐字实时转发、动作/观察按步推送
- **双入口**：终端 CLI + HTTP API（FastAPI）

## 快速开始

```bash
# 1. 安装依赖（建议使用虚拟环境）
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 2. 配置 API Key
copy .env.example .env        # 编辑 .env，填入你实际使用的厂商 Key

# 3a. WebUI（浏览器访问 http://127.0.0.1:8000）
#     在线目录默认自动托管：主服务启动时探活失败会自行拉起爬取服务（独立子进程），
#     一条命令即可；外部部署或自行管理爬取服务时设 CATALOG_AUTOSTART=0
python -m app.server

# 3b. 技能在线目录爬取服务（独立进程，可选单独启动；独立依赖）
pip install -r services/catalog/requirements.txt
python -m services.catalog          # 默认 127.0.0.1:8100

# 3c. HTTP API 服务（与 WebUI 同一服务）
python -m app.server          # 默认 127.0.0.1:8000

# 3d. 终端对话
python -m app.cli
python -m app.cli --provider deepseek
```

Windows 下也可以直接双击 **`run.bat`**：首次运行自动创建虚拟环境、安装依赖并生成 `.env`；随后菜单可选 WebUI（自动开浏览器）、终端对话、运行测试、更新依赖、编辑 `.env`。

启动 WebUI 前会自动做端口预检（端口取自 `.env` 的 `API_PORT`）：若端口已被本服务旧实例占用（通过 `/health` 的 `app` 身份标识确认），自动结束旧进程后重启；若被其他程序占用则拒绝误杀并提示换端口。

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/model` | 查看可用厂商及配置状态 |
| `/model <name>` | 切换厂商（deepseek / glm / minimax） |
| `/agent <任务>` | 发起自主多步任务（ReAct 循环，完成后答案回写主对话） |
| `/export [路径]` | 导出对话记录为 Markdown（缺省写到当前目录，可直接粘贴给 AI 排障） |
| `/reset` | 清空对话上下文（CLI 仍是进程内单会话） |
| `/help` | 帮助 |
| `/exit` | 退出 |

启动参数：`--provider`（起始厂商）、`--model`（指定模型名）、`--system`（system 提示词）。

命令不区分大小写；`.env` 中的 `LLM_MODEL` 只对默认厂商生效，切换厂商后自动使用该厂商的默认模型。

## HTTP API

```bash
# 对话（provider / model 均可选，model 覆盖本次请求的模型名）
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "你好", "provider": "glm"}'

# 聊天流式（逐 token SSE；curl -N 关闭缓冲）
curl -N -X POST http://127.0.0.1:8000/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "你好"}'
# 事件: {"type":"reasoning_delta","text":"..."}(思考链,如有) / {"type":"delta","text":"..."} ... {"type":"done","provider":"...","model":"..."}

# Agent 任务流式（思考逐字转发，动作/观察按步推送，终态带原因）
curl -N -X POST http://127.0.0.1:8000/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"task": "现在时间加 3 天是星期几"}'
# 事件: task_started / thought_delta / action / observation / final | failed

# 技能管理（技能包 = skills/<名称>/SKILL.md，改动热生效无需重启）
curl http://127.0.0.1:8000/skills                    # 已装技能列表（含启停状态与加载错误）
curl http://127.0.0.1:8000/skills/presets            # 预设平台源清单
curl http://127.0.0.1:8000/skills/时间报告           # 技能详情（含 SKILL.md 原文）
curl -X POST http://127.0.0.1:8000/skills/时间报告/disable
curl -X POST http://127.0.0.1:8000/skills/时间报告/enable
curl -X DELETE http://127.0.0.1:8000/skills/时间报告

# 安装技能：目录包（经爬取服务，点点点的后端）/ Git 适配器 / 本地导入
curl -X POST http://127.0.0.1:8000/skills/install \
  -H "Content-Type: application/json" \
  -d '{"source": "catalog", "catalog_id": "owner/repo/skill", "source_platform": "skills-sh"}'
curl -X POST http://127.0.0.1:8000/skills/install \
  -H "Content-Type: application/json" \
  -d '{"source": "git", "url": "https://github.com/anthropics/skills", "subpath": ""}'
curl -X POST http://127.0.0.1:8000/skills/install \
  -H "Content-Type: application/json" \
  -d '{"source": "local", "path": "D:/skills-source"}'

# 在线目录（爬取服务代理；CATALOG_BASE_URL 未配置时返回 503）
curl "http://127.0.0.1:8000/market/search?q=pdf&kind=skill&page=1&page_size=10"   # kind=skill|mcp 按资产类型过滤
curl http://127.0.0.1:8000/market/sources    # 源状态（含 warning 截断告警）+ refreshing（后台爬取进行中标志）
curl "http://127.0.0.1:8000/market/detail?id=owner/repo/skill&source=skills-sh"
# 异步触发一次目录爬取（立即返回 {"status": "started"|"running"}，重复触发去重；
# 默认翻到游标耗尽（2000 页硬上限防失控），分钟级，结果逐源降级落源状态；
# "max_pages"?: N 可显式限预算（供运维/测试用，同样受硬上限保护）
curl -X POST http://127.0.0.1:8000/market/refresh -H "Content-Type: application/json" -d '{}'

# MCP 管理（连接定义 = mcp/servers.json，改动可经重载生效；工具即装即用）
curl http://127.0.0.1:8000/mcp                          # 服务列表（状态 + 工具 + 授权状态 + 加载错误；值脱敏）
curl -X POST http://127.0.0.1:8000/mcp/enable  -H "Content-Type: application/json" -d '{"name": "io.github.acme/filesystem"}'
curl -X POST http://127.0.0.1:8000/mcp/disable -H "Content-Type: application/json" -d '{"name": "io.github.acme/filesystem"}'
curl -X POST http://127.0.0.1:8000/mcp/remove  -H "Content-Type: application/json" -d '{"name": "io.github.acme/filesystem"}'
curl -X POST http://127.0.0.1:8000/mcp/reload                          # 手改连接定义文件后重载重连
curl -X POST http://127.0.0.1:8000/mcp/reauth  -H "Content-Type: application/json" -d '{"name": "acme/remote"}'   # OAuth 服务重新授权（清 token 并重连）
# 安装 MCP：目录条目（env_values 为密钥值，只入 .env）/ 本地 server.json、mcp.json 导入
curl -X POST http://127.0.0.1:8000/mcp/install \
  -H "Content-Type: application/json" \
  -d '{"source": "catalog", "catalog_id": "io.github.acme/filesystem", "source_platform": "mcp-registry", "env_values": {"ACME_TOKEN": "..."}}'
curl -X POST http://127.0.0.1:8000/mcp/install \
  -H "Content-Type: application/json" \
  -d '{"source": "local", "path": "D:/mcp-import/servers.json"}'

# 导出主对话历史（?format=json 可选，默认 markdown；?session_id= 指定会话）
curl http://127.0.0.1:8000/export
curl "http://127.0.0.1:8000/export?format=json"
curl "http://127.0.0.1:8000/export?session_id=<会话id>"

# 会话（持久化在 SESSION_DB，重启后仍在）
curl http://127.0.0.1:8000/sessions
curl -X POST http://127.0.0.1:8000/sessions -H "Content-Type: application/json" -d '{"name": "新会话"}'
curl http://127.0.0.1:8000/sessions/<会话id>/messages
curl -X POST http://127.0.0.1:8000/sessions/<会话id>/rename -H "Content-Type: application/json" -d '{"name": "改名"}'
curl -X DELETE http://127.0.0.1:8000/sessions/<会话id>

# 新建一段对话（不再销毁旧记录：旧会话留在列表里）
curl -X POST http://127.0.0.1:8000/reset

# 任务状态与重连续流（断连后凭 task_id + from_seq 接回）
curl http://127.0.0.1:8000/agent/tasks/<任务id>
curl "http://127.0.0.1:8000/agent/tasks/<任务id>/stream?from_seq=0"

# 健康检查（app 字段为服务身份标识，run.bat 靠它识别端口占用者）/ 厂商状态
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/providers
```

## 项目结构

```
app/
├── config.py        # .env 加载、厂商注册表、目标解析（支持按厂商覆盖 base_url）
├── llm.py           # OpenAI SDK 统一封装（流式/非流式、function calling、思考链转写、错误转译）
├── conversation.py  # 主对话上下文管理与截断（可由持久化历史重建）
├── sessions.py      # 会话持久化：data/sessions.db（WAL + 版本化迁移 + 多进程占用检测）
├── tasks.py         # 任务运行器：后台执行、事件带 seq/ts 进缓冲并广播（断连 = 退订）
├── agent.py         # ReAct 状态机引擎：任务循环、双层终止、多 Agent 派发/critic、动态技能清单/预激活
├── skills.py        # 技能注册表：SKILL.md 解析/校验/自产、启停状态、安装器（Git/本地）
├── mcp.py           # MCP 连接定义模型 + 运行时管理器（client factory 测试缝、stdio/streamable-http 会话）
├── mcp_auth.py      # MCP 动态 OAuth 授权（token 文件存储 + 授权码/PKCE 贴码 + 设备码 + 授权状态）
├── observation.py   # 观察值视图 + 旁存续读（内存句柄池 LRU、read_tool_result 元工具）
├── audit.py         # 任务审计日志：logs/tasks/*.jsonl 全事件留痕（含子 agent 轨迹与会话归属）
├── tools.py         # 工具注册表 + 占位工具集 + 技能元工具（use_skill/search_skills/create_skill）
├── export.py        # 对话导出格式化（markdown / json 纯函数 + 任务轨迹协作树渲染）
├── cli.py           # 终端入口（/chat 流式 + /agent 任务 + /export 导出；进程内单会话）
├── server.py        # HTTP API + WebUI 入口（/chat/stream、/agent/stream、/agent/stop、/sessions*、/skills*、/mcp*、/market、/export）
└── static/
    └── index.html   # WebUI（聊天流式 + 会话路由与历史重放 + 任务时间线轨迹树 + 停止按钮 + / 技能弹层 + 统一市场：技能/MCP 双类资产已安装/在线浏览 + 单次确认卡，无框架，单文件）
services/
└── catalog/         # 在线目录爬取服务（独立进程，隔离区 = 唯一外网元数据交互点）
    ├── app.py       # /internal/* 窄合同 + /health
    ├── schema.py    # 统一条目/目录包 schema + 不可信输入清洗
    ├── store.py     # SQLite 缓存 + 源状态 + 平台凭证仓
    ├── refresher.py # 一次爬取 + 后台定时刷新（降级读缓存）
    ├── sources/     # 平台适配器（skills.sh HTML 降级 / LobeHub M2M API / 官方 MCP Registry）
    └── README.md    # ⚠ 隔离边界与独立部署升级路径
skills/
└── 时间报告/
    └── SKILL.md     # 内置演示技能包（其余技能经市场安装或 Agent create_skill 自产）
tests/
├── fakes.py             # 预约定测试缝隙（LLM fake + MCP 会话 fake + 目录源/HTTP fake）
├── test_config.py
├── test_conversation.py
├── test_tools.py
├── test_llm_tools.py
├── test_llm_thinking.py
├── test_agent.py
├── test_skills.py       # 技能注册表、元工具与安装器
├── test_skills_api.py   # 技能管理 HTTP API
├── test_mcp.py          # 连接定义模型 + 运行时管理器（fake client factory）
├── test_mcp_auth.py     # 动态 OAuth 授权流（token 存储/设备码/贴码/管理面，注入缝 fake）
├── test_mcp_api.py      # MCP 管理面 HTTP API
├── test_mcp_anchor.py   # 真协议锚点（官方 SDK stdio 跑通 echo server）
├── test_observation.py  # 观察值视图与旁存续读
├── test_catalog_schema.py  # 目录 schema 与清洗
├── test_catalog_app.py     # 爬取服务 HTTP 面
├── test_catalog_http.py    # 标准库 HTTP 客户端（本地实测，回归锚）
├── test_catalog_sources.py # 平台适配器（fixture 驱动）
├── test_catalog_mcp_registry.py # 官方 MCP Registry 适配器（fixture 驱动）
├── test_catalog_store.py   # SQLite 缓存 / 凭证仓 / 刷新器
├── test_catalog_pack.py    # 目录包获取（git 坐标 / ZIP + zip-slip 防护）
├── test_market_api.py      # 主服务 /market/* 代理与目录包安装
├── test_sessions.py        # 会话库（落库/迁移/多进程检测/命名/任务索引）
├── test_sessions_api.py    # 会话 HTTP 面（落库时机/恢复/CRUD/归属/轨迹回放）
├── test_tasks.py           # 任务生命周期解耦（真实 uvicorn：断连不取消/重连/并发拒绝）
├── test_webui_reconnect.py # WebUI 重连逻辑（node 跑页面里的真实实现）
├── test_export.py
└── test_server_stream.py
```

术语表见 [CONTEXT.md](CONTEXT.md)；架构决策见 [docs/adr/](docs/adr/)（[ADR-0004](docs/adr/0004-task-lifecycle-decoupled-from-http.md) 记录「任务生命周期不绑定 HTTP 连接」）；设计规格见 [docs/specs/](docs/specs/)。

## 配置说明

见 [.env.example](.env.example)。只需填写实际使用的厂商 Key；`LLM_PROVIDER` 指定默认厂商；`LLM_MODEL` 仅覆盖默认厂商的模型；`MAX_CONTEXT_MESSAGES` 控制上下文保留的历史消息条数（最小 2）；`CONTEXT_WINDOW_TOKENS` / `CONTEXT_OUTPUT_RESERVE_TOKENS` 定义上下文预算（入模预算 = 窗口 − 输出预留，token 为零依赖估算口径；`/chat` 响应与 SSE `done` 帧的 `context_meta` 报出每次入模的估算占用与预算）；`AGENT_MAX_ITERATIONS` 控制 Agent 任务的最大思考轮数（最小 1，超过则以 partial 进展摘要收尾）。

多 Agent 编排：`AGENT_MAX_SUBTASKS` 控制子任务总数上限（默认 6，派发闸门：跨批累计、含回炉重派）；`AGENT_MAX_PARALLEL` 控制子任务并行度（默认 3）；`AGENT_CRITIC_ENABLED` 控制 critic 收尾审查（默认开：每任务一次审查调用、最多一次整体回炉，置 0 关闭）。

技能相关：`SKILLS_DIR` 指定技能包目录（默认 `skills/`，相对项目根）；`SKILLS_CATALOG_MAX` 控制技能清单注入 system prompt 的条数上限（默认 30，超出部分模型可用 `search_skills` 检索）；`CHAT_MAX_TOOL_TURNS` 控制聊天通道迷你工具循环的轮数上限（默认 4）；`CATALOG_BASE_URL` 指向技能在线目录爬取服务（默认 `http://127.0.0.1:8100`，留空则关闭在线目录并隐藏入口）；`CATALOG_AUTOSTART` 控制主服务是否自动托管本机爬取服务（默认开，外部部署/自行管理时置 0）。爬取服务自身配置（`CATALOG_HOST` / `CATALOG_PORT` / `CATALOG_DB` / `CATALOG_REFRESH_HOURS` / `CATALOG_SOURCES`）见 [services/catalog/README.md](services/catalog/README.md)。

MCP 与观察值：`MCP_CONFIG` 指定 MCP 连接定义文件（默认 `mcp/servers.json`，相对项目根，条目形态见 [docs/specs/0003-mcp-market.md](docs/specs/0003-mcp-market.md)）；`MCP_MAX_TOOLS` 为 MCP 工具总数上限（默认 64，超限在装配期报错提示禁用部分服务，0 = 不限）；`MCP_TOKEN_FILE` 为 OAuth token 文件（默认 `mcp/tokens.json`，`auth: oauth` 服务的授权产物：权限收紧、不入库、不进 `.env` 明文）；`OBSERVATION_MAX_CHARS` 为工具观察值视图大小（默认 4000；截断时全文旁存句柄池、模型经 `read_tool_result` 续读，0 = 全量直灌）。

会话持久化：`SESSION_DB` 指定会话库路径（默认 `data/sessions.db`，相对项目根，见 [docs/specs/0005-session-persistence.md](docs/specs/0005-session-persistence.md)）。主对话历史与会话元数据落在这里，服务重启后仍在；与审计日志分开存放（会话是一等状态，`logs/` 是可丢弃留痕）。`data/` 已被 `.gitignore` 忽略。库版本高于代码版本、或被另一活进程占用时，启动会打印中文原因并退出。

如需按厂商覆盖接入端点，设置 `<厂商>_BASE_URL`（如 `GLM_BASE_URL`）。典型场景：GLM Coding Plan 套餐 Key 只对编码专用端点生效，需设置 `GLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4`，否则标准端点会报 1113 余额不足。

## 三厂商 function calling 冒烟

Agent 任务依赖各家端点支持 function calling（OpenAI tools 协议）。验收方式：对每家厂商执行

```bash
python -m app.cli --provider <厂商>
/agent 现在时间加 3 天是星期几
```

预期：至少经过一次 `行动: get_current_time` 或 `行动: calculator`，最终给出正确答案。端点不支持 tools 时会返回明确的中文错误（不做文本协议降级），此时请更换厂商。GLM Coding Plan 套餐 Key 需设置编码专用端点（见配置说明）。

## 运行测试

```bash
python -m unittest discover tests -v
```

测试不发起真实请求：LLM 层用脚本化 fake（tests/fakes.py），工具为纯函数占位集，技能安装测试用临时目录与本地 git 仓库离线克隆。

## 已知范围限制

- 会话历史持久化在 `SESSION_DB`（默认 `data/sessions.db`，已 gitignore）：服务重启后主对话与任务轨迹都还在；**页面展示全量历史、发给模型仍按 `MAX_CONTEXT_MESSAGES` 截断**（长会话的上下文仍是短板，自动摘要未做）
- **不做「进程重启后续跑同一任务」**：重启后一切可见、可回看、可重跑，但不续跑——那需要工具副作用幂等性与 MCP 会话/厂商流等不可恢复资源的重建语义。中断的任务在时间线上标注「⚠ 已中断（进程重启）」并保留中断前已有的进展，附「重跑」按钮（以同一任务文本发起**新任务**，不是续跑）
- 单进程边界：检测到另一活进程占用同一会话库会拒绝启动（心跳 + pid 存活判定）；不做多进程部署与跨进程任务广播。停机时置取消并最多等 5 秒让在跑任务收尾（任务线程是守护线程，超时即随进程退出）
- 旁存句柄池不落盘：重启后轨迹里「续读某句柄」的调用无法满足，UI 与错误信息如实说明（观察值 `is_error` 事件随审计日志落盘，回放时原样呈现）
- 多 Agent 编排为一层深度（子 agent 不能再派发）；critic 默认开启 = 每任务多一次审查调用、最多一次整体重跑（`AGENT_CRITIC_ENABLED=0` 可关）；MCP 工具不随派发默认下放，需编排器显式点名
- MCP 接入支持 stdio 与 streamable-http（sse 协议已废弃，不做）；密钥支持静态 env/header（`${VAR}` 占位 + `.env`，推荐长期 PAT）与 `auth: oauth` 动态授权流（授权码 + PKCE 终端贴码 / 无头设备码；token 存 `MCP_TOKEN_FILE` 独立文件、过期自动刷新，刷新失败降级为连接错误提示重新授权；OAuth 首次授权在装配期终端交互完成，无浏览器自助重定向服务）；mcpb 单文件包条目暂不支持安装（后续扩展点）；官方 MCP Registry 处于 preview 阶段（schema 可能变更），默认翻到游标耗尽（2000 页硬上限防失控）；stdio 不做沙箱——连接定义的命令以 argv 直起子进程（不经 shell），配置 stdio 服务 = 授权本机执行该命令（确认卡已披露命令原文）
- 第三方技能包是提示注入面（生态已有恶意技能实测报告）：格式严格校验 + 字段白名单 + 长度上限兜底，内容不做自动审计，仅安装可信来源
- 技能在线目录：skills.sh 官方 API 为 Vercel OIDC 专属，走页面内嵌数据降级解析（**站点改版需跟进适配器**）；LobeHub 需一次注册（限 5 次/30 分钟/IP）；目录包附带脚本/资源一律丢弃（纯提示词边界）；`services/catalog/catalog.db` 含平台凭证，已被 .gitignore 忽略
- CLI 仍为进程内单会话，**不读写** WebUI 的会话库（两条入口的历史互不相干）

## 后续规划

- MCP 市场后续扩展：mcpb 单文件包下载安装、ClawHub 适配器、已验证源免确认白名单；`/` 技能弹层混入在线目录
- 技能平台原生搜索与在线浏览（skills.sh / ClawHub / LobeHub API）、zip 上传、版本与更新检查
- 多会话管理增强：会话保留策略与自动清理、会话导入/导出为可移植格式
- 任务恢复增强：工具副作用幂等性契约 + 进程重启后从审计日志续跑同一任务
- 基于持久化历史的自动摘要压缩（长会话上下文短板）
- 网关特化逻辑（路由、鉴权、审计）
