# multiagent

针对内部网关场景特化的 Agent 项目。第一步：多厂商大模型对话对接（DeepSeek / GLM / MiniMax），支持上下文与运行时切换厂商。

## 特性

- **统一接入**：三家厂商均通过 OpenAI 兼容 API 接入，一个 SDK 覆盖全部
- **上下文支持**：内存维护单会话历史，自动按上限截断（保留 system + 最近 N 条）
- **运行时切换**：CLI 中 `/model glm` 随时切换厂商；API 请求中传 `provider` 字段
- **流式输出**：CLI 逐字流式显示回复
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

## CLI 命令

| 命令 | 说明 |
|------|------|
| `/model` | 查看可用厂商及配置状态 |
| `/model <name>` | 切换厂商（deepseek / glm / minimax） |
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
├── llm.py           # OpenAI SDK 统一封装（流式/非流式、错误转译）
├── conversation.py  # 会话历史管理与截断
├── cli.py           # 终端入口
├── server.py        # HTTP API + WebUI 入口
└── static/
    └── index.html   # WebUI 聊天页面（无框架，单文件）
tests/
├── test_config.py
└── test_conversation.py
```

## 配置说明

见 [.env.example](.env.example)。只需填写实际使用的厂商 Key；`LLM_PROVIDER` 指定默认厂商；`LLM_MODEL` 仅覆盖默认厂商的模型；`MAX_CONTEXT_MESSAGES` 控制上下文保留的历史消息条数（最小 2）。

如需按厂商覆盖接入端点，设置 `<厂商>_BASE_URL`（如 `GLM_BASE_URL`）。典型场景：GLM Coding Plan 套餐 Key 只对编码专用端点生效，需设置 `GLM_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4`，否则标准端点会报 1113 余额不足。

## 运行测试

```bash
python -m unittest discover tests -v
```

## 已知范围限制

- 单会话内存上下文：服务重启后历史清空；`/chat` 接口内部已加锁串行化，适合单用户/低并发使用
- HTTP API 暂为非流式返回，SSE 在后续规划中

## 后续规划

- 工具调用（function calling）与 Agent 化
- 多会话管理与持久化
- 流式 API（SSE）
- 网关特化逻辑（路由、鉴权、审计）
