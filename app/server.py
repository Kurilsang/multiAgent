"""HTTP API 服务入口（FastAPI，单会话内存上下文）。

启动:
    python -m app.server
    # 或
    uvicorn app.server:app --host 127.0.0.1 --port 8000

接口:
    GET  /                WebUI 聊天页面
    GET  /health          健康检查
    GET  /providers       列出厂商及配置状态
    POST /chat            发送一条消息 {"message": "...", "provider": "glm"(可选), "model": "..."(可选)}
    POST /reset           清空对话上下文
"""

import json
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .config import PROVIDERS, ConfigError, Settings, resolve_target
from .conversation import Conversation
from .llm import LLMClient, LLMError

settings = Settings()
llm = LLMClient(settings)
conversation = Conversation(
    system_prompt=None, max_messages=settings.max_context_messages
)

# 单会话范围：串行化对话轮次，避免并发 /chat 互相污染同一份历史
_chat_lock = threading.Lock()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="multiagent", description="内部网关场景 Agent —— 第一步：多厂商 LLM 对话")


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
    return {"status": "ok"}


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
                for chunk in llm.chat_stream(
                    conversation.messages_for_api(), req.provider, req.model
                ):
                    collected.append(chunk)
                    yield _sse({"type": "delta", "text": chunk})
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
