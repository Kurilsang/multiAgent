# multiagent

针对内部网关场景特化的 Agent 项目。在多厂商大模型对话（DeepSeek / GLM / MiniMax）之上，引入 ReAct 式单 Agent 自主执行循环：思考→行动→观察→判断，双入口全链路流式。

## 特性

- **统一接入**：三家厂商均通过 OpenAI 兼容 API 接入，一个 SDK 覆盖全部
- **上下文支持**：内存维护单会话历史，自动按上限截断（保留 system + 最近 N 条）
- **Agent 任务**：`/agent` 发起自主多步任务——状态机编排（见 docs/adr/0001）、双层终止（见 docs/adr/0002）、死循环止损、partial 进展摘要
- **工具 + 技能**：工具走 function calling 通道，技能走上下文通道（占位集：`get_current_time` / `calculator` + 「时间报告」演示技能）
- **运行时切换**：CLI 中 `/model glm` 随时切换厂商；API 请求中传 `provider` 字段
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
python -m app.server

# 3b. HTTP API 服务（与 WebUI 同一服务）
python -m app.server          # 默认 127.0.0.1:8000

# 3c. 终端对话
python -m app.cli
python -m app.cli --provider deepseek
```

Windows 下也可以直接双击 **`run.bat`**：首次运行自动创建虚拟环境、安装依赖并生成 `.env`；随后菜单可选 WebUI（自动开浏览器）、终端对话、运行测试、更新依赖、编辑 `.env`。

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/model` | 查看可用厂商及配置状态 |
| `/model <name>` | 切换厂商（deepseek / glm / minimax） |
| `/agent <任务>` | 发起自主多步任务（ReAct 循环，完成后答案回写主对话） |
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
# 事件: {"type":"delta","text":"..."} ... {"type":"done","provider":"...","model":"..."}

# Agent 任务流式（思考逐字转发，动作/观察按步推送，终态带原因）
curl -N -X POST http://127.0.0.1:8000/agent/stream \
  -H "Content-Type: application/json" \
  -d '{"task": "现在时间加 3 天是星期几"}'
# 事件: task_started / thought_delta / action / observation / final | failed

# 清空对话上下文
curl -X POST http://127.0.0.1:8000/reset

# 健康检查 / 厂商状态
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/providers
```

## 项目结构

```
app/
├── config.py        # .env 加载、厂商注册表、目标解析（支持按厂商覆盖 base_url）
├── llm.py           # OpenAI SDK 统一封装（流式/非流式、function calling、错误转译）
├── conversation.py  # 主对话历史管理与截断
├── agent.py         # ReAct 状态机引擎：任务循环、双层终止、技能通道
├── tools.py         # 工具注册表 + 占位工具集（get_current_time / calculator）
├── cli.py           # 终端入口（/chat 流式 + /agent 任务）
├── server.py        # HTTP API + WebUI 入口（/chat/stream、/agent/stream）
└── static/
    └── index.html   # WebUI（聊天流式 + 任务时间线，无框架，单文件）
tests/
├── fakes.py             # 脚本化 LLM fake（预约定测试缝隙）
├── test_config.py
├── test_conversation.py
├── test_tools.py
├── test_llm_tools.py
├── test_agent.py
└── test_server_stream.py
```

术语表见 [CONTEXT.md](CONTEXT.md)；架构决策见 [docs/adr/](docs/adr/)。

## 配置说明

见 [.env.example](.env.example)。只需填写实际使用的厂商 Key；`LLM_PROVIDER` 指定默认厂商；`LLM_MODEL` 仅覆盖默认厂商的模型；`MAX_CONTEXT_MESSAGES` 控制上下文保留的历史消息条数（最小 2）；`AGENT_MAX_ITERATIONS` 控制 Agent 任务的最大思考轮数（最小 1，超过则以 partial 进展摘要收尾）。

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

测试不发起真实请求：LLM 层用脚本化 fake（tests/fakes.py），工具为纯函数占位集。

## 已知范围限制

- 单会话内存上下文：服务重启后历史与任务轨迹清空；接口内部已加锁串行化，适合单用户/低并发使用
- Agent 任务为单 Agent 循环：多 Agent 协作仅预留状态机后门（新增状态与迁移边即可，见 docs/adr/0001）
- MCP 工具接入未实现：工具注册表已预留适配位（远端工具灌入同一注册表即可）
- SSE 断连后任务不恢复，页面重开需重新发起

## 后续规划

- 多 Agent 协作（评审者/执行者分工，复用状态机引擎）
- MCP 工具适配器
- 多会话管理与持久化
- 网关特化逻辑（路由、鉴权、审计）
