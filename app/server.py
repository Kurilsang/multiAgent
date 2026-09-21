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

import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .config import PROVIDERS, ConfigError, Settings
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
