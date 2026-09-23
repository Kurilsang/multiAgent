# AGENTS.md

给 Agent 的项目规则入口。细节文档见 [README.md](README.md)。

## 项目定位

内部网关场景特化的多厂商 LLM 对话服务（DeepSeek / GLM / MiniMax，均走 OpenAI 兼容 API），双入口：终端 CLI + FastAPI WebUI/HTTP API。当前是第三步：技能市场（SKILL.md 文件化 + 动态清单 + 双通道调用）。术语表见 [CONTEXT.md](CONTEXT.md)。

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
- `app/tools.py` — 工具注册表（function calling 通道）+ 占位工具集 + 技能元工具（use_skill/search_skills/create_skill）；新增工具只改注册表，引擎不感知；full_result=True 的观察值完整回灌不截断
- `app/cli.py` / `app/server.py` — 两个入口；server 的 `/chat`、`/chat/stream`、`/agent/stream`、`/skills*` 共用线程锁串行化，定位单用户/低并发；聊天通道带 `CHAT_MAX_TOOL_TURNS` 有界迷你工具循环，`/技能名` 前缀确定性路由任务通道并预激活
- `app/static/index.html` — 无框架单文件 WebUI（聊天流式 + 任务时间线 + `/` 技能弹层 + 技能市场视图）
- `tests/fakes.py` — 预约定测试缝隙：脚本化 LLM fake；测试不发真实请求（git 安装测试用本地仓库离线克隆）
- 约定：新增厂商只改 `config.py` 的 `PROVIDERS` + `.env`；错误信息从用户视角写；配置类改动要同步 `.env.example` 和 README；技能包只写 SKILL.md 纯提示词（frontmatter 仅 name/description/tools，不携带可执行脚本）

## 当前状态与下一步

- 已完成：三厂商对话、上下文截断、运行时切厂商、ReAct 单 Agent 循环（function calling + 工具/技能注册表）、CLI + WebUI 全链路流式 SSE、双层终止与死循环止损、思考链统一转写与展示（reasoning_content / 内联 `<think>` → ReasoningDelta）、对话导出（WebUI 导出按钮、CLI `/export`、HTTP `GET /export`）、技能市场（SKILL.md 文件化技能包、动态清单 + use_skill/search_skills/create_skill 元工具、聊天通道自主激活与 `/技能名` 前缀路由、Git/本地安装与启停删管理 API、WebUI `/` 弹层与市场视图）
- 已知限制：重启后历史与轨迹清空；任务显式触发（CLI `/agent`、HTTP `/agent/stream`）或 `/技能名` 点名，无自动路由；端点不支持 tools 时直接报错（无文本协议降级）；SSE 断连任务不恢复；第三方技能包是提示注入面（格式校验兜底，不做内容审计）
- 下一步：多 Agent 协作（状态机已留后门：新增状态与迁移边）、MCP 工具适配器（灌入同一注册表）、技能平台原生搜索与在线浏览（skills.sh / ClawHub / LobeHub）、多会话持久化、网关逻辑（路由/鉴权/审计）
