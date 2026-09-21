# AGENTS.md

给 Agent 的项目规则入口。细节文档见 [README.md](README.md)。

## 项目定位

内部网关场景特化的多厂商 LLM 对话服务（DeepSeek / GLM / MiniMax，均走 OpenAI 兼容 API），双入口：终端 CLI + FastAPI WebUI/HTTP API。当前是第一步：纯对话。

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
- `app/llm.py` — OpenAI SDK 封装；非流式 = 聚合流式实现；120s 超时；错误转译成面向用户的中文
- `app/conversation.py` — 单会话内存历史，按 `MAX_CONTEXT_MESSAGES` 截断（保留 system + 最近 N 条）
- `app/cli.py` / `app/server.py` — 两个入口；server 的 `/chat` 用线程锁串行化，定位单用户/低并发
- `app/static/index.html` — 无框架单文件 WebUI
- 约定：新增厂商只改 `config.py` 的 `PROVIDERS` + `.env`；错误信息从用户视角写；配置类改动要同步 `.env.example` 和 README

## 当前状态与下一步

- 已完成：三厂商对话、上下文截断、运行时切厂商、CLI 流式输出、WebUI + 非流式 HTTP API
- 已知限制：重启后历史清空；HTTP API 暂非流式
- 下一步：工具调用（function calling）、多会话持久化、SSE 流式 API、网关逻辑（路由/鉴权/审计）
