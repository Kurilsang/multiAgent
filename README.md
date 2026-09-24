# multiagent

针对内部网关场景特化的 Agent 项目。在多厂商大模型对话（DeepSeek / GLM / MiniMax）之上，引入 ReAct 式单 Agent 自主执行循环：思考→行动→观察→判断，双入口全链路流式；第三步落地技能市场：SKILL.md 文件化技能包、动态清单与双通道技能调用；第四步接入技能在线目录：独立爬取服务抓目录，浏览点装一键完成。

## 特性

- **统一接入**：三家厂商均通过 OpenAI 兼容 API 接入，一个 SDK 覆盖全部
- **上下文支持**：内存维护单会话历史，自动按上限截断（保留 system + 最近 N 条）
- **Agent 任务**：`/agent` 发起自主多步任务——状态机编排（见 docs/adr/0001）、双层终止（见 docs/adr/0002）、死循环止损、partial 进展摘要
- **工具 + 技能**：工具走 function calling 通道；技能为文件化提示词包（`skills/<名称>/SKILL.md`），动态清单 + `use_skill` 按需激活，Agent 可用 `create_skill` 自产技能
- **技能市场**：预设平台 / Git URL / 本地目录安装（覆盖 anthropics/skills、skills.sh 等 SKILL.md 生态），WebUI 管理启停/删除/查看，热生效无需重启
- **技能在线目录**：独立爬取服务（`services/catalog/`，隔离区）抓取 skills.sh / LobeHub 目录——SQLite 缓存 + 定时刷新 + 陈旧标注；市场页「在线浏览」搜索翻页 → 单次确认卡（SKILL.md 预览 + Socket/Snyk 等审计徽标）→ 一键安装；主服务不直接外网抓目录（MCP 运行时连接按用户显式配置直连，见 [docs/specs/0003-mcp-market.md](docs/specs/0003-mcp-market.md)）
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
| `/reset` | 清空对话上下文 |
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
curl "http://127.0.0.1:8000/market/search?q=pdf&page=1&page_size=10"
curl http://127.0.0.1:8000/market/sources
curl "http://127.0.0.1:8000/market/detail?id=owner/repo/skill&source=skills-sh"
curl -X POST http://127.0.0.1:8000/market/refresh -H "Content-Type: application/json" -d '{}'

# 导出主对话历史（?format=json 可选，默认 markdown）
curl http://127.0.0.1:8000/export
curl "http://127.0.0.1:8000/export?format=json"

# 清空对话上下文
curl -X POST http://127.0.0.1:8000/reset

# 健康检查（app 字段为服务身份标识，run.bat 靠它识别端口占用者）/ 厂商状态
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/providers
```

## 项目结构

```
app/
├── config.py        # .env 加载、厂商注册表、目标解析（支持按厂商覆盖 base_url）
├── llm.py           # OpenAI SDK 统一封装（流式/非流式、function calling、思考链转写、错误转译）
├── conversation.py  # 主对话历史管理与截断
├── agent.py         # ReAct 状态机引擎：任务循环、双层终止、动态技能清单/预激活
├── skills.py        # 技能注册表：SKILL.md 解析/校验/自产、启停状态、安装器（Git/本地）
├── tools.py         # 工具注册表 + 占位工具集 + 技能元工具（use_skill/search_skills/create_skill）
├── export.py        # 对话导出格式化（markdown / json 纯函数）
├── cli.py           # 终端入口（/chat 流式 + /agent 任务 + /export 导出）
├── server.py        # HTTP API + WebUI 入口（/chat/stream、/agent/stream、/skills、/market、/export）
└── static/
    └── index.html   # WebUI（聊天流式 + 任务时间线 + / 技能弹层 + 技能市场：已安装/在线浏览 + 确认卡，无框架，单文件）
services/
└── catalog/         # 技能在线目录爬取服务（独立进程，隔离区 = 唯一外网面）
    ├── app.py       # /internal/* 窄合同 + /health
    ├── schema.py    # 统一条目/目录包 schema + 不可信输入清洗
    ├── store.py     # SQLite 缓存 + 源状态 + 平台凭证仓
    ├── refresher.py # 一次爬取 + 后台定时刷新（降级读缓存）
    ├── sources/     # 平台适配器（skills.sh HTML 降级 / LobeHub M2M API）
    └── README.md    # ⚠ 隔离边界与独立部署升级路径
skills/
└── 时间报告/
    └── SKILL.md     # 内置演示技能包（其余技能经市场安装或 Agent create_skill 自产）
tests/
├── fakes.py             # 预约定测试缝隙（LLM fake + 目录源/HTTP fake）
├── test_config.py
├── test_conversation.py
├── test_tools.py
├── test_llm_tools.py
├── test_llm_thinking.py
├── test_agent.py
├── test_skills.py       # 技能注册表、元工具与安装器
├── test_skills_api.py   # 技能管理 HTTP API
├── test_catalog_schema.py  # 目录 schema 与清洗
├── test_catalog_app.py     # 爬取服务 HTTP 面
├── test_catalog_sources.py # 平台适配器（fixture 驱动）
├── test_catalog_store.py   # SQLite 缓存 / 凭证仓 / 刷新器
├── test_catalog_pack.py    # 目录包获取（git 坐标 / ZIP + zip-slip 防护）
├── test_market_api.py      # 主服务 /market/* 代理与目录包安装
├── test_export.py
└── test_server_stream.py
```

术语表见 [CONTEXT.md](CONTEXT.md)；架构决策见 [docs/adr/](docs/adr/)；设计规格见 [docs/specs/](docs/specs/)。

## 配置说明

见 [.env.example](.env.example)。只需填写实际使用的厂商 Key；`LLM_PROVIDER` 指定默认厂商；`LLM_MODEL` 仅覆盖默认厂商的模型；`MAX_CONTEXT_MESSAGES` 控制上下文保留的历史消息条数（最小 2）；`AGENT_MAX_ITERATIONS` 控制 Agent 任务的最大思考轮数（最小 1，超过则以 partial 进展摘要收尾）。

技能相关：`SKILLS_DIR` 指定技能包目录（默认 `skills/`，相对项目根）；`SKILLS_CATALOG_MAX` 控制技能清单注入 system prompt 的条数上限（默认 30，超出部分模型可用 `search_skills` 检索）；`CHAT_MAX_TOOL_TURNS` 控制聊天通道迷你工具循环的轮数上限（默认 4）；`CATALOG_BASE_URL` 指向技能在线目录爬取服务（默认 `http://127.0.0.1:8100`，留空则关闭在线目录并隐藏入口）。爬取服务自身配置（`CATALOG_HOST` / `CATALOG_PORT` / `CATALOG_DB` / `CATALOG_REFRESH_HOURS` / `CATALOG_SOURCES`）见 [services/catalog/README.md](services/catalog/README.md)。

MCP 与观察值：`MCP_CONFIG` 指定 MCP 连接定义文件（默认 `mcp/servers.json`，相对项目根，条目形态见 [docs/specs/0003-mcp-market.md](docs/specs/0003-mcp-market.md)）；`MCP_MAX_TOOLS` 为 MCP 工具总数上限（默认 64，超限在装配期报错提示禁用部分服务，0 = 不限）；`OBSERVATION_MAX_CHARS` 为工具观察值视图大小（默认 4000；截断时全文旁存句柄池、模型经 `read_tool_result` 续读，0 = 全量直灌）。

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

- 单会话内存上下文：服务重启后历史与任务轨迹清空；接口内部已加锁串行化，适合单用户/低并发使用
- Agent 任务为单 Agent 循环：多 Agent 协作仅预留状态机后门（新增状态与迁移边即可，见 docs/adr/0001）
- MCP 接入当前仅支持 stdio（远程 streamable-http 未实现，见 #36）；mcpb 单文件包条目暂不支持安装（见 #35）；stdio 不做沙箱——连接定义的命令以 argv 直起子进程（不经 shell），配置 stdio 服务 = 授权本机执行该命令
- 第三方技能包是提示注入面（生态已有恶意技能实测报告）：格式严格校验 + 字段白名单 + 长度上限兜底，内容不做自动审计，仅安装可信来源
- 技能在线目录：skills.sh 官方 API 为 Vercel OIDC 专属，走页面内嵌数据降级解析（**站点改版需跟进适配器**）；LobeHub 需一次注册（限 5 次/30 分钟/IP）；目录包附带脚本/资源一律丢弃（纯提示词边界）；`services/catalog/catalog.db` 含平台凭证，已被 .gitignore 忽略
- SSE 断连后任务不恢复，页面重开需重新发起

## 后续规划

- 多 Agent 协作（评审者/执行者分工，复用状态机引擎）
- MCP 市场后续票：官方 Registry 目录适配器与浏览点装、远程 streamable-http 接入、WebUI 统一市场与单次确认卡（#35-#39）
- 技能平台原生搜索与在线浏览（skills.sh / ClawHub / LobeHub API）、zip 上传、版本与更新检查
- 多会话管理与持久化
- 网关特化逻辑（路由、鉴权、审计）
