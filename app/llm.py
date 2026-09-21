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
class ToolCall:
    """模型发起的一次工具调用；arguments 为原始 JSON 串，由调用方解析。"""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ChatResult:
    content: str
    provider: str
    model: str
    tool_calls: tuple[ToolCall, ...] = ()


# ---- 流式事件：引擎据此实时转发思考并拿到结构化工具调用 ----


@dataclass(frozen=True)
class TextDelta:
    """一段增量回复文本。"""

    text: str


@dataclass(frozen=True)
class ResponseToolCalls:
    """流结束时的聚合工具调用（若无则为空，事件本身不出现）。"""

    tool_calls: tuple[ToolCall, ...]


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
        tools: list[dict] | None = None,
    ) -> ChatResult:
        """非流式对话，返回完整回复与（可能的）工具调用。"""
        target = resolve_target(self._settings, provider, model)
        content_parts: list[str] = []
        tool_calls: tuple[ToolCall, ...] = ()
        for event in self.chat_events(messages, provider, model, tools):
            if isinstance(event, TextDelta):
                content_parts.append(event.text)
            else:
                tool_calls = event.tool_calls
        return ChatResult(
            content="".join(content_parts),
            provider=target.provider,
            model=target.model,
            tool_calls=tool_calls,
        )

    def chat_stream(
        self,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
    ) -> Iterator[str]:
        """流式对话，逐段产出回复文本。"""
        for event in self.chat_events(messages, provider, model):
            if isinstance(event, TextDelta):
                yield event.text

    def chat_events(
        self,
        messages: list[dict],
        provider: str | None = None,
        model: str | None = None,
        tools: list[dict] | None = None,
    ) -> Iterator[TextDelta | ResponseToolCalls]:
        """流式对话事件：逐段产出 TextDelta，流结束时若有工具调用再产出
        ResponseToolCalls。引擎据此实时转发思考文本并获取结构化动作。"""
        target = resolve_target(self._settings, provider, model)
        client = self._client_for(target)
        extra = {"tools": tools} if tools else {}
        # 各厂商分片习惯不同，tool_calls 统一按 index 聚合
        pending: dict[int, dict] = {}
        try:
            stream = client.chat.completions.create(
                model=target.model,
                messages=messages,
                stream=True,
                **extra,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                # 部分 OpenAI 兼容网关会发出 delta 为 None/空的 chunk，取值需容错
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    yield TextDelta(content)
                _accumulate_tool_calls(pending, getattr(delta, "tool_calls", None))
        except Exception as exc:
            raise LLMError(_friendly_error(target.provider, exc)) from exc
        if pending:
            yield ResponseToolCalls(tuple(_materialize_tool_calls(pending)))


def _accumulate_tool_calls(pending: dict[int, dict], chunks) -> None:
    """把增量 tool_calls 分片按 index 聚合进 pending（原地更新）。

    各厂商分片习惯不同：有的首片带全量，有的把 arguments 拆成多段，
    统一按 index 聚合、字符串片段拼接。
    """
    for chunk in chunks or ():
        slot = pending.setdefault(chunk.index, {"id": "", "name": "", "parts": []})
        if chunk.id:
            slot["id"] = chunk.id
        function = getattr(chunk, "function", None)
        if function is not None:
            if function.name:
                slot["name"] = function.name
            if function.arguments:
                slot["parts"].append(function.arguments)


def _materialize_tool_calls(pending: dict[int, dict]) -> Iterator[ToolCall]:
    """把聚合结果按 index 顺序物化为 ToolCall。"""
    for index in sorted(pending):
        slot = pending[index]
        yield ToolCall(
            id=slot["id"] or f"call_{index}",
            name=slot["name"],
            arguments="".join(slot["parts"]),
        )


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
