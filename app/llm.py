"""LLM 调用层：基于 OpenAI SDK 的统一对话客户端。

三家厂商均为 OpenAI 兼容 API，本层只负责按目标厂商构建 client
并发起 chat completions 请求，不管理对话历史（见 conversation.py）。
"""

from collections.abc import Iterator
from dataclasses import dataclass

from openai import OpenAI

from .config import ResolvedTarget, Settings, resolve_target

# 上游无响应时的等待上限；SDK 默认 600s，会长时间挂住 CLI 与 API 线程
REQUEST_TIMEOUT_SECONDS = 120.0


class LLMError(Exception):
    """对话请求失败，携带面向用户的可读信息。"""


@dataclass(frozen=True)
class ChatResult:
    content: str
    provider: str
    model: str


class LLMClient:
    """按厂商缓存 OpenAI client，提供流式 / 非流式对话。

    配置错误（ConfigError）在调用 chat/chat_stream 时同步抛出，
    上游请求错误在消费流时以 LLMError 抛出。
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._clients: dict[str, OpenAI] = {}

    def _client_for(self, target: ResolvedTarget) -> OpenAI:
        client = self._clients.get(target.provider)
        if client is None:
            client = OpenAI(
                api_key=target.api_key,
                base_url=target.base_url,
                timeout=REQUEST_TIMEOUT_SECONDS,
                max_retries=2,
            )
            self._clients[target.provider] = client
        return client

    def chat(
        self,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
    ) -> ChatResult:
        """非流式对话，返回完整回复。"""
        target = resolve_target(self._settings, provider, model)
        content = "".join(self._stream(target, messages))
        return ChatResult(content=content, provider=target.provider, model=target.model)

    def chat_stream(
        self,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
    ) -> Iterator[str]:
        """流式对话，逐段产出回复文本。"""
        target = resolve_target(self._settings, provider, model)
        return self._stream(target, messages)

    def _stream(self, target: ResolvedTarget, messages: list[dict]) -> Iterator[str]:
        client = self._client_for(target)
        try:
            stream = client.chat.completions.create(
                model=target.model,
                messages=messages,
                stream=True,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                # 部分 OpenAI 兼容网关会发出 delta 为 None/空的 chunk，取值需容错
                content = getattr(chunk.choices[0].delta, "content", None)
                if content:
                    yield content
        except Exception as exc:
            raise LLMError(_friendly_error(target.provider, exc)) from exc


def _friendly_error(provider: str, exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    if status == 401:
        return f"{provider} 认证失败（401），请检查 API Key 是否正确"
    if status == 404:
        return (
            f"{provider} 请求 404，请检查模型名或 base_url 是否有效"
        )
    if status == 429:
        return f"{provider} 触发限流或余额不足（429）"
    if status is not None:
        return f"{provider} 返回错误（HTTP {status}）: {exc}"
    return f"请求 {provider} 失败: {exc}"
