"""HTTP API 服务入口（FastAPI，单会话内存上下文）。

启动:
    python -m app.server
    # 或
    uvicorn app.server:app --host 127.0.0.1 --port 8000

接口:
    GET  /                WebUI 聊天页面
    GET  /health          健康检查（含 app 身份标识，供 run.bat 识别端口占用者）
    GET  /providers       列出厂商及配置状态
    POST /chat            发送一条消息 {"message": "...", "provider": "glm"(可选), "model": "..."(可选)}
    POST /chat/stream     同 /chat，逐 token SSE 流式推送（含 reasoning_delta 思考帧）
    POST /agent/stream    发起自主任务 {"task": "...", ...}，思考/动作/观察 SSE 事件流
    GET  /export          导出主对话历史，?format=markdown(默认)|json
    POST /reset           清空对话上下文
"""

import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .agent import (
    ActionObserved,
    ActionStarted,
    AgentEngine,
    TaskFailed,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
    demo_time_report_skill,
)
from .config import PROVIDERS, ConfigError, Settings, resolve_target
from .conversation import Conversation
from .export import to_json, to_markdown
from .llm import LLMClient, LLMError, ReasoningDelta
from .tools import default_registry

settings = Settings()
llm = LLMClient(settings)
conversation = Conversation(
    system_prompt=None, max_messages=settings.max_context_messages
)
agent_engine = AgentEngine(
    llm,
    default_registry(),
    max_iterations=settings.agent_max_iterations,
    skills=(demo_time_report_skill(),),
)

# 单会话范围：串行化对话轮次，避免并发 /chat 互相污染同一份历史
_chat_lock = threading.Lock()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="multiagent",
    description="内部网关场景 Agent —— 第二步：ReAct 单 Agent（多厂商对话 + 工具调用 + 全链路流式）",
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """WebUI 聊天页面。"""
    return FileResponse(STATIC_DIR / "index.html")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None

    @field_validator("message")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message 不能为空白")
        return value


class ChatResponse(BaseModel):
    reply: str
    provider: str
    model: str


@app.get("/health")
def health() -> dict:
    """健康检查。

    app 字段是本服务的身份标识：run.bat 在启动前探测目标端口，
    只有 /health 自报 multiagent 才会 kill 旧实例，避免误杀其他服务。
    """
    return {"status": "ok", "app": "multiagent"}


@app.get("/providers")
def providers() -> dict:
    return {
        "default": settings.llm_provider,
        "providers": {
            name: {
                "default_model": info.default_model,
                "configured": bool(settings.api_key_for(name)),
            }
            for name, info in PROVIDERS.items()
        },
    }


@app.post("/reset")
def reset() -> dict:
    with _chat_lock:
        conversation.reset()
    return {"status": "ok"}


@app.get("/export")
def export_conversation(format: str = "markdown") -> Response:
    """导出主对话历史：markdown 便于直接粘贴给 AI 排障，json 供程序处理。"""
    with _chat_lock:
        messages = conversation.history()
    fmt = format.strip().lower()
    if fmt in ("markdown", "md"):
        return Response(
            to_markdown(messages, default_provider=settings.llm_provider),
            media_type="text/markdown; charset=utf-8",
        )
    if fmt == "json":
        return Response(
            to_json(messages, default_provider=settings.llm_provider),
            media_type="application/json; charset=utf-8",
        )
    raise HTTPException(
        status_code=400, detail=f"不支持的导出格式: {format!r}，可选 markdown / json"
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    with _chat_lock:
        conversation.add("user", req.message)
        try:
            result = llm.chat(conversation.messages_for_api(), req.provider, req.model)
        except ConfigError as exc:
            conversation.pop_last()
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LLMError as exc:
            conversation.pop_last()
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            conversation.pop_last()
            raise HTTPException(status_code=500, detail=f"内部错误: {exc}") from exc
        conversation.add("assistant", result.content)
    return ChatResponse(
        reply=result.content, provider=result.provider, model=result.model
    )


def _sse(data: dict) -> str:
    """编码一条 SSE 事件帧。"""
    return "data: " + json.dumps(data, ensure_ascii=False) + "\n\n"


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """逐 token SSE 聊天；行为与 /chat 一致，改为流式推送。

    非 200 的配置错误在开流之前抛出（仍是 HTTP 状态码），
    开流之后的上游错误以 error 事件帧推送。
    思考链以 reasoning_delta 帧透传展示，不写入主对话历史。
    """
    try:
        target = resolve_target(settings, req.provider, req.model)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def generate():
        with _chat_lock:
            conversation.add("user", req.message)
            collected: list[str] = []
            try:
                for event in llm.chat_events(
                    conversation.messages_for_api(), req.provider, req.model
                ):
                    if isinstance(event, ReasoningDelta):
                        yield _sse({"type": "reasoning_delta", "text": event.text})
                    else:
                        collected.append(event.text)
                        yield _sse({"type": "delta", "text": event.text})
                conversation.add("assistant", "".join(collected))
                yield _sse(
                    {"type": "done", "provider": target.provider, "model": target.model}
                )
            except LLMError as exc:
                conversation.pop_last()
                yield _sse({"type": "error", "detail": str(exc)})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


class AgentRequest(BaseModel):
    task: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None

    @field_validator("task")
    @classmethod
    def _reject_blank_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task 不能为空白")
        return value


def _agent_event_frame(event) -> dict:
    """把引擎事件编码为 SSE 载荷。"""
    if isinstance(event, TaskStarted):
        return {"type": "task_started"}
    if isinstance(event, ThoughtDelta):
        return {"type": "thought_delta", "text": event.text}
    if isinstance(event, ActionStarted):
        return {"type": "action", "tool": event.tool_name, "arguments": event.arguments}
    if isinstance(event, ActionObserved):
        return {
            "type": "observation",
            "tool": event.tool_name,
            "text": event.result,
            "is_error": event.is_error,
        }
    if isinstance(event, TaskFinished):
        return {
            "type": "final",
            "status": event.status,
            "answer": event.answer,
            "iterations": event.iterations,
        }
    if isinstance(event, TaskFailed):
        return {"type": "failed", "reason": event.reason, "iterations": event.iterations}
    raise RuntimeError(f"未知的引擎事件类型: {type(event).__name__}")


@app.post("/agent/stream")
def agent_stream(req: AgentRequest) -> StreamingResponse:
    """Agent 任务 SSE：思考逐字实时转发，动作/观察按步推送，终态带原因。

    非 200 的配置错误在开流之前抛出；引擎自身会把上游错误编码为
    failed 帧，因此流内不会抛出异常。
    """
    try:
        resolve_target(settings, req.provider, req.model)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def generate():
        # 与聊天共用同一把锁：任务回写主对话时互斥
        with _chat_lock:
            answer: str | None = None
            for event in agent_engine.run(req.task, req.provider, req.model):
                yield _sse(_agent_event_frame(event))
                if isinstance(event, TaskFinished):
                    answer = event.answer
            if answer is not None:
                # 与 CLI 路径一致：completed / partial 回写「请求 + 最终答案」
                conversation.add("user", req.task)
                conversation.add("assistant", answer)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
