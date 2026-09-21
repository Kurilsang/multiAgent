"""测试替身:预约定缝隙的 fake 实现。

主缝隙 = LLM 客户端抽象:引擎与入口层的测试用脚本化 fake,
不发起真实请求;工具经注册表注册假工具或直接用占位工具集,
杜绝真实时钟与网络。
"""

import json

from app.llm import ResponseToolCalls, TextDelta, ToolCall
from app.tools import Tool, ToolRegistry


def text_round(text: str) -> list:
    """一轮纯文本回复（模型未调用工具 → 直接作为最终答案）。"""
    return [TextDelta(text)]


def tool_round(name, arguments, call_id="call_1", thought=""):
    """一轮工具调用回复（可带思考文本）。"""
    events = []
    if thought:
        events.append(TextDelta(thought))
    events.append(
        ResponseToolCalls(
            (
                ToolCall(
                    id=call_id,
                    name=name,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                ),
            )
        )
    )
    return events


class FakeLLMClient:
    """按脚本逐轮响应 chat_events；记录每次收到的 messages/tools 供断言。

    脚本条目若为 Exception 实例，则在该轮直接抛出（模拟上游错误）。
    脚本耗尽后再被调用即失败，用于守住「计划外的模型调用」。
    """

    def __init__(self, script):
        self._script = [
            round_events if isinstance(round_events, Exception) else list(round_events)
            for round_events in script
        ]
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def chat_events(self, messages, provider=None, model=None, tools=None):
        self.calls.append(([dict(m) for m in messages], [dict(t) for t in (tools or [])]))
        if not self._script:
            raise AssertionError("脚本耗尽：引擎发起了计划外的模型调用")
        round_events = self._script.pop(0)
        if isinstance(round_events, Exception):
            raise round_events
        for event in round_events:
            yield event


def make_registry(name="echo", result="ok", error=None) -> ToolRegistry:
    """注册单个假工具的注册表；error 非 None 时执行即抛错。"""

    def run():
        if error is not None:
            raise error
        return result

    registry = ToolRegistry()
    registry.register(
        Tool(
            name=name,
            description="假工具",
            parameters={"type": "object", "properties": {}, "required": []},
            func=run,
        )
    )
    return registry
