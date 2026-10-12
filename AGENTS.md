# AGENTS.md

给 Agent 的项目规则入口。细节文档见 [README.md](README.md)。

## 项目定位

内部网关场景特化的多厂商 LLM 对话服务（DeepSeek / GLM / MiniMax，均走 OpenAI 兼容 API），双入口：终端 CLI + FastAPI WebUI/HTTP API。当前是第八步：会话内记忆与上下文预算（已落地，规格 docs/specs/0006；前七步三厂商对话、ReAct 循环、技能市场、技能在线目录、MCP 市场、多 Agent 协作、多会话持久化与断连续流均已落地）。术语表见 [CONTEXT.md](CONTEXT.md)，设计规格见 [docs/specs/](docs/specs/)。

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
- `app/conversation.py` — 主对话上下文；入模统一走 `app/context_budget.py` 预算分层装配（`Conversation.assemble` 是 CLI 侧薄封装，server 会话通道走 `assemble_with_summary`），`from_history` 由持久化历史重建
- `app/context_budget.py` — 上下文预算与装配（SPEC-0006）：零依赖 token 估算纯函数、`assemble_messages` 预算分层装填（system → 记忆块 → 最近 N 条保底 → 更早对话）、`assemble_with_summary` 滚动摘要吸收（覆盖水位单调、失败降级硬截断）、`context_meta` 账目；`MAX_CONTEXT_MESSAGES` 是「最近保底」而非截断上限
- `app/sessions.py` — 会话持久化（SPEC-0005）：`data/sessions.db`（WAL + `PRAGMA user_version` 版本化迁移 + 多进程心跳检测 fail loud）；消息含思考链/工具步骤/厂商归属/失败状态
- `app/tasks.py` — 任务运行器（SPEC-0005）：任务后台执行、事件带单调 `seq` 进缓冲并广播给订阅者；断连 = 退订（不置取消、不持锁）；同会话并发闸门 `TaskBusy`
- `app/agent.py` — ReAct 状态机引擎（见 docs/adr/0001、0002、0003）：任务轨迹独立于主对话，双层终止（finish 工具 + `AGENT_MAX_ITERATIONS` 硬上限 + 死循环止损）；多 Agent 编排（`dispatch_task` 特殊工具 + `DISPATCHING`/`WAITING` 状态、子 agent 动态装配深度限一层、工具继承闸门（MCP 显式点名）、critic 收尾与回炉封顶、`CancelToken` 叫停）；技能走上下文通道（动态清单 + run(activated=) 预激活）
- `app/skills.py` — 技能注册表：SKILL.md 解析/校验（frontmatter 三字段白名单）、自产（create_skill）、启停状态（`skills/.installed.json`）、安装器（Git 适配器/本地导入）；文件态是唯一事实源，热重载
- `app/mcp.py` — MCP 连接定义模型（`MCP_CONFIG` 解析/校验/${VAR} 占位/命名清洗 fail loud + 认证声明 `auth`/`auth_flow`）+ 运行时管理器（client factory 测试缝、默认官方 mcp SDK 会话 stdio / streamable-http、错误展平到叶子原因、授权闸门与重授权）
- `app/mcp_auth.py` — MCP 动态 OAuth 授权（issue #44）：token 文件存储（`MCP_TOKEN_FILE`，权限收紧/不入库/不进 .env）+ 授权码 PKCE 终端贴码 + 设备码（RFC 8628）+ 授权状态三态；网络与交互全走注入缝
- `app/audit.py` — 任务审计日志：每任务一份 `logs/tasks/*.jsonl`（全事件含子 agent 轨迹，失败/叫停留痕；头部带会话归属）；`logs/` 不入库
- `app/observation.py` — 观察值视图 + 旁存续读（内存句柄池 LRU、read_tool_result 元工具）；视图随剩余预算收缩至 `OBSERVATION_MIN_CHARS` 保底（两通道共享原语）
- `app/tools.py` — 工具注册表（function calling 通道）+ 占位工具集 + 技能元工具（use_skill/search_skills/create_skill）；新增工具只改注册表，引擎不感知（聊天通道另有工具允许集判定，见 server）；full_result=True 的观察值完整回灌不截断
- `app/cli.py` / `app/server.py` — 两个入口；server 的会话历史落会话库、任务在后台跑（SSE 只订阅），管理面用独立锁、会话用每会话锁，定位单用户/低并发；聊天通道带 `CHAT_MAX_TOOL_TURNS` 有界迷你工具循环，`/技能名` 前缀确定性路由任务通道并预激活
- `services/catalog/` — 技能在线目录爬取服务（独立进程，独立 requirements）：**外网元数据交互唯一集中点（隔离区）**，零密钥、零技能包写权限，`/internal/*` 仅对内网；独立部署升级 = 本包原样搬出 + 主服务改 `CATALOG_BASE_URL`（详见其 README）；主服务自身不外网抓目录，MCP 运行时可按用户显式配置直连远程端点（SPEC-0003 网络边界决策）
- `app/static/index.html` — 无框架单文件 WebUI（聊天流式 + 任务时间线轨迹树（多 Agent 分区 + 回炉标记 + 停止按钮）+ `/` 技能弹层 + 统一市场视图：技能 / MCP 双类资产、已安装/在线浏览双子面板 + 单次确认卡）
- `tests/fakes.py` — 预约定测试缝隙：脚本化 LLM fake + 执行器 fake（InlineExecutor/RecordingExecutor，多 Agent 并发缝）+ MCP 会话 fake（client factory 注入）+ MCP OAuth fake（授权协作对象/HTTP 注入）+ 目录源/HTTP fake；测试不发真实请求（git 安装测试用本地仓库离线克隆）
- 约定：新增厂商只改 `config.py` 的 `PROVIDERS` + `.env`；错误信息从用户视角写；配置类改动要同步 `.env.example` 和 README；技能包只写 SKILL.md 纯提示词（frontmatter 仅 name/description/tools，不携带可执行脚本）；**外网抓取只允许发生在 `services/catalog/`**，主服务不直接外网抓取（MCP 运行时按用户显式配置直连除外，含 OAuth 授权端点）

## 当前状态与下一步

- 已完成：三厂商对话、上下文截断、运行时切厂商、ReAct 单 Agent 循环（function calling + 工具/技能注册表）、CLI + WebUI 全链路流式 SSE、双层终止与死循环止损、思考链统一转写与展示（reasoning_content / 内联 `<think>` → ReasoningDelta）、对话导出（WebUI 导出按钮、CLI `/export`、HTTP `GET /export`）、技能市场（SKILL.md 文件化技能包、动态清单 + use_skill/search_skills/create_skill 元工具、聊天通道自主激活与 `/技能名` 前缀路由、Git/本地安装与启停删管理 API、WebUI `/` 弹层与市场视图）、技能在线目录（services/catalog 爬取服务隔离区：skills.sh HTML 降级 + LobeHub M2M 市场 API、SQLite 缓存 + 定时刷新、/market/* 代理、目录包安装 source=catalog、WebUI 在线浏览 + 单次确认卡）、MCP 市场（#32-#39 全部落地：目录 schema 泛化 kind + 通用 manifest 合同、连接定义与启动装配 stdio 真协议闭环、观察值视图与旁存续读 read_tool_result、远程 streamable-http 接入 + 断连重连、管理面与聊天通道工具暴露、官方 MCP Registry 适配器与安装来源、WebUI 统一市场与 MCP 单次确认卡、文档与配置同步收口）、动态 OAuth 授权流（#44：`auth`/`auth_flow` 连接定义声明 + token 文件存储 MCP_TOKEN_FILE + 授权码 PKCE 终端贴码 + 设备码 + 过期自动刷新 + 管理面授权状态与 /mcp/reauth + WebUI 徽章与重新授权）、多 Agent 协作（#46-#53 全部落地：事件 agent 标识 + 审计日志 JSONL + /export 含轨迹、状态机 DISPATCHING/WAITING + dispatch_task 特殊工具、子 agent 动态装配与工具继承闸门 + AGENT_MAX_SUBTASKS 派发闸门 + 终态聚合、线程池并行 AGENT_MAX_PARALLEL + 执行器注入缝、critic 收尾审查 + 回炉一次封顶 + AGENT_CRITIC_ENABLED、POST /agent/stop 叫停 + 断连兜底、WebUI 轨迹树 + CLI 汇总、文档与配置同步 + ADR-0003）、多会话持久化与断连续流（SPEC-0005 **全部落地**：#55 任务生命周期与 HTTP 连接解耦、#56 会话持久化闭环、#57 会话侧栏与 CRUD、#58 任务归属与轨迹回放（含「已中断」标注与「重跑」）、#59 断连续流与重启后从审计日志补历史、#60 收口（ADR-0004 + 术语 + 解禁指针））、会话内记忆与上下文预算（SPEC-0006 **全部落地**：#62 上下文用量可见（token 估算 + 预算 + context_meta）、#63 预算分层装配器（MAX_CONTEXT_MESSAGES 变保底）、#64 滚动摘要与记忆块（会话库迁移 v3 + 覆盖水位单调）、#65 观察值预算降级（共享原语）、#66 任务通道接入降级、#67 文档收口（ADR-0005 + 术语））
- 已知限制：**会话与任务轨迹已持久化**（`SESSION_DB` 默认 `data/sessions.db`，重启后仍在；页面展示全量、入模按上下文预算分层装配——超预算旧对话吸收进滚动摘要、观察值随预算收缩到保底，`MAX_CONTEXT_MESSAGES` 为「最近保底」条数）；token 为零依赖保守估算、摘要质量依赖模型（原话留会话库可回查）；跨会话长期记忆只留记忆块接口未实现；**不做进程重启后续跑同一任务**（可回看、可重跑，不续跑——需工具副作用幂等性；中断任务标注「已中断」并附「重跑」按钮，明确是新任务）；SSE 断连**不再取消任务**（断连 = 退订，任务照跑完；凭 task_id + from_seq 可重连续流，网络中断自动退避重连）；单进程边界（多进程占用同一会话库即拒绝启动；停机最多等 5 秒让任务收尾）；CLI 仍为进程内单会话、不读写会话库；旁存句柄池不落盘（重启后轨迹里的续读调用无法满足，如实提示）；任务显式触发（CLI `/agent`、HTTP `/agent/stream`）或 `/技能名` 点名，无自动路由；端点不支持 tools 时直接报错（无文本协议降级）；第三方技能包是提示注入面（格式校验兜底，不做内容审计）；skills.sh 官方 API 为 OIDC 专属（走 HTML 降级，页面改版需跟进适配器）；LobeHub 需一次注册（限 5 次/30 分钟/IP）；目录包附带脚本/资源一律丢弃（纯提示词边界）；MCP 支持 stdio + streamable-http（sse 不做），密钥 = 静态 env/header 或 `auth: oauth` 动态授权流（授权码贴码/设备码；token 存 `MCP_TOKEN_FILE` 独立文件不入库；首次授权在装配期终端交互完成），mcpb 单文件包暂不可安装（后续扩展点），Registry 目录为游标拉取（updated_since 真增量未做；默认翻到见底、2000 页硬上限防失控，极端规模截断有告警），stdio 无沙箱（argv 直起、确认卡披露命令原文）；多 Agent 编排一层深度（子 agent 不能再派发）、critic 默认开（每任务一次审查调用 + 最多一次整体重跑，`AGENT_CRITIC_ENABLED=0` 关）、MCP 工具不随派发默认下放（编排器显式点名）
- 下一步：ClawHub 适配器与已验证源免确认白名单、`/` 弹层混入在线目录、网关逻辑（路由/鉴权/审计）、多 Agent 角色分工（具名角色配置）、会话保留策略与自动清理、跨会话长期记忆（记忆块第二种来源：抽取/存储/召回）

## Agent skills

### Issue tracker

GitHub Issues（`gh` CLI），spec 双写 GitHub issue 与 `docs/specs/`。See `docs/agents/issue-tracker.md`.

### Triage labels

默认五标签（needs-triage / needs-info / ready-for-agent / ready-for-human / wontfix）。See `docs/agents/triage-labels.md`.

### Domain docs

single-context（根目录 `CONTEXT.md` + `docs/adr/`）。See `docs/agents/domain.md`.
