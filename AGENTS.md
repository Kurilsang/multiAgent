# AGENTS.md

给 Agent 的项目规则入口。细节文档见 [README.md](README.md)。

## 项目定位

内部网关场景特化的多厂商 LLM 对话服务（DeepSeek / GLM / MiniMax，均走 OpenAI 兼容 API），双入口：终端 CLI + FastAPI WebUI/HTTP API。当前是第五步：MCP 市场与 MCP 工具接入（进行中，规格 docs/specs/0003；第三、四步技能市场与技能在线目录为其底座）。术语表见 [CONTEXT.md](CONTEXT.md)，设计规格见 [docs/specs/](docs/specs/)。

## 怎么跑

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
copy .env.example .env                            # 填实际使用厂商的 API Key

python -m app.server                              # WebUI + HTTP API，127.0.0.1:8000
python -m app.cli [--provider glm] [--model xxx] [--system "..."]
python -m unittest discover tests -v              # 测试
```

## 技术栈

Python 3.12；openai SDK（统一三家厂商）、pydantic-settings（.env 配置）、FastAPI + uvicorn、rich（CLI）。

## 目录与约定

- `app/config.py` — 厂商注册表 + 目标解析；`LLM_MODEL` 只对默认厂商生效；`<厂商>_BASE_URL` 可覆盖端点（GLM Coding Plan 必须指向编码端点）
- `app/llm.py` — OpenAI SDK 封装；非流式 = 聚合流式实现；`chat_events` 产出 TextDelta/ResponseToolCalls；120s 超时；错误转译成面向用户的中文
- `app/conversation.py` — 主对话内存历史，按 `MAX_CONTEXT_MESSAGES` 截断（保留 system + 最近 N 条）
- `app/agent.py` — ReAct 状态机引擎（见 docs/adr/0001、0002）：任务轨迹独立于主对话，双层终止（finish 工具 + `AGENT_MAX_ITERATIONS` 硬上限 + 死循环止损）；技能走上下文通道（动态清单 + run(activated=) 预激活）
- `app/skills.py` — 技能注册表：SKILL.md 解析/校验（frontmatter 三字段白名单）、自产（create_skill）、启停状态（`skills/.installed.json`）、安装器（Git 适配器/本地导入）；文件态是唯一事实源，热重载
- `app/mcp.py` — MCP 连接定义模型（`MCP_CONFIG` 解析/校验/${VAR} 占位/命名清洗 fail loud）+ 运行时管理器（client factory 测试缝、默认官方 mcp SDK stdio 会话）
- `app/observation.py` — 观察值视图 + 旁存续读（内存句柄池 LRU、read_tool_result 元工具）
- `app/tools.py` — 工具注册表（function calling 通道）+ 占位工具集 + 技能元工具（use_skill/search_skills/create_skill）；新增工具只改注册表，引擎不感知（聊天通道另有工具允许集判定，见 server）；full_result=True 的观察值完整回灌不截断
- `app/cli.py` / `app/server.py` — 两个入口；server 的 `/chat`、`/chat/stream`、`/agent/stream`、`/skills*`、`/market*` 共用线程锁串行化，定位单用户/低并发；聊天通道带 `CHAT_MAX_TOOL_TURNS` 有界迷你工具循环，`/技能名` 前缀确定性路由任务通道并预激活
- `services/catalog/` — 技能在线目录爬取服务（独立进程，独立 requirements）：**外网元数据交互唯一集中点（隔离区）**，零密钥、零技能包写权限，`/internal/*` 仅对内网；独立部署升级 = 本包原样搬出 + 主服务改 `CATALOG_BASE_URL`（详见其 README）；主服务自身不外网抓目录，MCP 运行时可按用户显式配置直连远程端点（SPEC-0003 网络边界决策）
- `app/static/index.html` — 无框架单文件 WebUI（聊天流式 + 任务时间线 + `/` 技能弹层 + 技能市场视图：已安装/在线浏览双子面板 + 单次确认卡）
- `tests/fakes.py` — 预约定测试缝隙：脚本化 LLM fake；测试不发真实请求（git 安装测试用本地仓库离线克隆）
- 约定：新增厂商只改 `config.py` 的 `PROVIDERS` + `.env`；错误信息从用户视角写；配置类改动要同步 `.env.example` 和 README；技能包只写 SKILL.md 纯提示词（frontmatter 仅 name/description/tools，不携带可执行脚本）；**外网抓取只允许发生在 `services/catalog/`**，主服务不直接外网抓取（MCP 运行时按用户显式配置直连除外）

## 当前状态与下一步

- 已完成：三厂商对话、上下文截断、运行时切厂商、ReAct 单 Agent 循环（function calling + 工具/技能注册表）、CLI + WebUI 全链路流式 SSE、双层终止与死循环止损、思考链统一转写与展示（reasoning_content / 内联 `<think>` → ReasoningDelta）、对话导出（WebUI 导出按钮、CLI `/export`、HTTP `GET /export`）、技能市场（SKILL.md 文件化技能包、动态清单 + use_skill/search_skills/create_skill 元工具、聊天通道自主激活与 `/技能名` 前缀路由、Git/本地安装与启停删管理 API、WebUI `/` 弹层与市场视图）、技能在线目录（services/catalog 爬取服务隔离区：skills.sh HTML 降级 + LobeHub M2M 市场 API、SQLite 缓存 + 定时刷新、/market/* 代理、目录包安装 source=catalog、WebUI 在线浏览 + 单次确认卡）、MCP 进行中（#32-#34 已落：目录 schema 泛化 kind + 通用 manifest 合同、MCP 连接定义与启动装配 stdio 真协议闭环、观察值视图与旁存续读 read_tool_result）
- 已知限制：重启后历史与轨迹清空；任务显式触发（CLI `/agent`、HTTP `/agent/stream`）或 `/技能名` 点名，无自动路由；端点不支持 tools 时直接报错（无文本协议降级）；SSE 断连任务不恢复；第三方技能包是提示注入面（格式校验兜底，不做内容审计）；skills.sh 官方 API 为 OIDC 专属（走 HTML 降级，页面改版需跟进适配器）；LobeHub 需一次注册（限 5 次/30 分钟/IP）；目录包附带脚本/资源一律丢弃（纯提示词边界）；MCP 仅支持 stdio（远程 streamable-http 见 #36），mcpb 单文件包暂不可安装（见 #35），stdio 无沙箱（argv 直起、确认卡披露命令原文）
- 下一步：MCP 市场剩余票（#35 Registry 适配器与安装来源、#36 远程接入、#37 管理面与聊天暴露、#38 WebUI 确认卡、#39 文档收口）、多 Agent 协作（状态机已留后门：新增状态与迁移边）、ClawHub 适配器与已验证源免确认白名单、`/` 弹层混入在线目录、多会话持久化、网关逻辑（路由/鉴权/审计）

## Agent skills

### Issue tracker

GitHub Issues（`gh` CLI），spec 双写 GitHub issue 与 `docs/specs/`。See `docs/agents/issue-tracker.md`.

### Triage labels

默认五标签（needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix）。See `docs/agents/triage-labels.md`.

### Domain docs

single-context（根目录 `CONTEXT.md` + `docs/adr/`）。See `docs/agents/domain.md`.
