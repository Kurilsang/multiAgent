"""ReAct Agent 引擎：单 Agent 任务的状态机循环。

显式状态机（docs/adr/0001）：节点即状态，迁移表是数据，
未来多 Agent 协作通过新增状态与迁移边扩展，引擎不变。
双层终止（docs/adr/0002）：模型侧 finish 工具宣告语义完成，
引擎侧迭代硬上限与死循环止损兜底。
轨迹独立于主对话：任务结束由调用方把「用户请求 + 最终答案」回写主对话。
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto

from .llm import LLMError, ResponseToolCalls, TextDelta
from .tools import ToolRegistry

# 判断节点连续看到相同（工具, 参数）达此次数即判死循环
DEAD_LOOP_THRESHOLD = 3
# 单条观察值回灌上限：工具输出可能巨大，截断保护上下文
DEFAULT_MAX_OBSERVATION_CHARS = 4000
# 轨迹消息上限（不含 system）：超出时保留任务消息与最近轨迹
MAX_TRACE_MESSAGES = 40
# 观察值预览在终端/摘要里的展示上限
OBSERVATION_PREVIEW_CHARS = 300

FINISH_TOOL_NAME = "finish"

_FINISH_SCHEMA = {
    "type": "function",
    "function": {
        "name": FINISH_TOOL_NAME,
        "description": "任务完成时调用，交付给用户的最终答案",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "给用户的最终答案"}
            },
            "required": ["answer"],
        },
    },
}

_BASE_PROTOCOL = (
    "你是自主执行任务的助手。逐步思考，需要事实或计算时调用提供的工具；"
    "获得足够信息后，调用 finish 工具交付面向用户的最终答案。"
)


class AgentState(Enum):
    THINKING = auto()
    ACTING = auto()
    OBSERVING = auto()
    JUDGING = auto()
    FINISHED = auto()
    FAILED = auto()


# 迁移表：数据驱动的状态机骨架（见 ADR-0001）。
# FINISHED / FAILED 是终态，只能迁入、不能迁出。
TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.THINKING: {AgentState.ACTING, AgentState.FINISHED, AgentState.FAILED},
    AgentState.ACTING: {AgentState.OBSERVING},
    AgentState.OBSERVING: {AgentState.JUDGING},
    AgentState.JUDGING: {AgentState.THINKING, AgentState.FINISHED, AgentState.FAILED},
    AgentState.FINISHED: set(),
    AgentState.FAILED: set(),
}

_TERMINALS = {AgentState.FINISHED, AgentState.FAILED}


# ---- 任务事件：引擎的对外输出，CLI 与 SSE 都按事件渲染 ----


@dataclass(frozen=True)
class TaskStarted:
    task: str


@dataclass(frozen=True)
class ThoughtDelta:
    """思考文本增量（逐字实时转发）。"""

    text: str


@dataclass(frozen=True)
class ActionStarted:
    """模型发起的工具调用。"""

    tool_name: str
    arguments: dict


@dataclass(frozen=True)
class ActionObserved:
    """工具执行结果（观察值）；is_error 标记失败回灌。"""

    tool_name: str
    result: str
    is_error: bool


@dataclass(frozen=True)
class TaskFinished:
    """任务结束；status 为 completed（达成）或 partial（预算耗尽）。"""

    status: str
    answer: str
    iterations: int


@dataclass(frozen=True)
class TaskFailed:
    """任务失败（死循环止损、响应无法解析、上游请求失败等）。"""

    reason: str
    iterations: int


AgentEvent = (
    TaskStarted | ThoughtDelta | ActionStarted | ActionObserved | TaskFinished | TaskFailed
)


@dataclass(frozen=True)
class _ParsedCall:
    """解析后的工具调用：arguments 为 dict，arguments_json 保留原始串。"""

    id: str
    name: str
    arguments: dict
    arguments_json: str


def _parse_call(call) -> _ParsedCall:
    try:
        arguments = json.loads(call.arguments)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{call.name} 的参数不是有效 JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError(f"{call.name} 的参数不是 JSON 对象")
    return _ParsedCall(
        id=call.id,
        name=call.name,
        arguments=arguments,
        arguments_json=call.arguments,
    )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _assistant_message(thought: str, calls: list[_ParsedCall]) -> dict:
    return {
        "role": "assistant",
        "content": thought,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in calls
        ],
    }


def _tool_message(call_id: str, result: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": result}


def _progress_summary(trace: list[dict], iterations: int) -> str:
    """预算耗尽时的轨迹摘要：让用户知道任务推进到了哪一步。"""
    lines = [f"已执行 {iterations} 轮仍未达成目标，以下是最近的进展："]
    for message in trace[-4:]:
        role = message["role"]
        if role == "assistant":
            thought = (message.get("content") or "").strip()
            if thought:
                lines.append(f"[思考] {_truncate(thought, 200)}")
            names = [tc["function"]["name"] for tc in message.get("tool_calls", [])]
            if names:
                lines.append(f"[动作] 调用 {'、'.join(names)}")
        elif role == "tool":
            lines.append(f"[观察] {_truncate(message['content'], 200)}")
    return "\n".join(lines)


class AgentEngine:
    """消费 LLM 流式事件并驱动思考→行动→观察→判断循环。"""

    def __init__(
        self,
        llm,
        tools: ToolRegistry,
        max_iterations: int = 8,
        max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
    ):
        if not tools.names():
            raise ValueError("Agent 引擎至少需要一个工具，否则无法形成行动-观察回路")
        if max_iterations < 1:
            raise ValueError("max_iterations 至少为 1")
        self.llm = llm
        self._tools = tools
        self._max_iterations = max_iterations
        self._max_observation_chars = max_observation_chars
        # finish 是协议工具：随每次请求注入 schema，但不属于注册表
        self._schemas = [*tools.openai_schemas(), _FINISH_SCHEMA]

    def run(
        self,
        task: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> Iterator[AgentEvent]:
        """执行一次任务，逐个产出事件；终态事件必为 TaskFinished 或 TaskFailed。"""
        yield TaskStarted(task=task)
        trace: list[dict] = [{"role": "user", "content": task}]
        recent_actions: list[tuple[str, str]] = []
        iterations = 0
        parsed_calls: list[_ParsedCall] = []
        terminal: AgentEvent | None = None

        state = AgentState.THINKING
        while state not in _TERMINALS:
            if state is AgentState.THINKING:
                iterations += 1
                thought_parts: list[str] = []
                calls = []
                try:
                    stream = self.llm.chat_events(
                        self._messages_for_api(trace), provider, model, self._schemas
                    )
                    for event in stream:
                        if isinstance(event, TextDelta):
                            thought_parts.append(event.text)
                            yield ThoughtDelta(text=event.text)
                        elif isinstance(event, ResponseToolCalls):
                            calls = list(event.tool_calls)
                except LLMError as exc:
                    state = self._goto(state, AgentState.FAILED)
                    terminal = TaskFailed(reason=str(exc), iterations=iterations)
                    continue

                thought = "".join(thought_parts)
                if not calls and not thought.strip():
                    state = self._goto(state, AgentState.FAILED)
                    terminal = TaskFailed(reason="模型响应为空", iterations=iterations)
                    continue
                if not calls:
                    # 无工具调用：模型直接给出最终答案
                    state = self._goto(state, AgentState.FINISHED)
                    terminal = TaskFinished(
                        status="completed", answer=thought, iterations=iterations
                    )
                    continue
                try:
                    parsed_calls = [_parse_call(call) for call in calls]
                except ValueError as exc:
                    state = self._goto(state, AgentState.FAILED)
                    terminal = TaskFailed(
                        reason=f"工具调用参数无法解析：{exc}", iterations=iterations
                    )
                    continue

                finish_call = next(
                    (call for call in parsed_calls if call.name == FINISH_TOOL_NAME), None
                )
                if finish_call is not None:
                    # 双层终止的模型侧：finish 交付语义完成，答案缺省回退到思考文本
                    answer = finish_call.arguments.get("answer") or thought
                    state = self._goto(state, AgentState.FINISHED)
                    terminal = TaskFinished(
                        status="completed", answer=answer, iterations=iterations
                    )
                    continue

                trace.append(_assistant_message(thought, parsed_calls))
                state = self._goto(state, AgentState.ACTING)
            elif state is AgentState.ACTING:
                for call in parsed_calls:
                    yield ActionStarted(tool_name=call.name, arguments=call.arguments)
                    result, is_error = self._execute(call)
                    result = _truncate(result, self._max_observation_chars)
                    yield ActionObserved(tool_name=call.name, result=result, is_error=is_error)
                    trace.append(_tool_message(call.id, result))
                    recent_actions.append((call.name, call.arguments_json))
                state = self._goto(state, AgentState.OBSERVING)
            elif state is AgentState.OBSERVING:
                state = self._goto(state, AgentState.JUDGING)
            else:  # JUDGING：双层终止的引擎侧谓词，不发起额外模型调用
                if self._is_dead_loop(recent_actions):
                    terminal = TaskFailed(
                        reason=f"连续 {DEAD_LOOP_THRESHOLD} 次相同的工具调用，判定死循环而中止",
                        iterations=iterations,
                    )
                    state = self._goto(state, AgentState.FAILED)
                elif iterations >= self._max_iterations:
                    terminal = TaskFinished(
                        status="partial",
                        answer=_progress_summary(trace, iterations),
                        iterations=iterations,
                    )
                    state = self._goto(state, AgentState.FINISHED)
                else:
                    state = self._goto(state, AgentState.THINKING)

        assert terminal is not None
        yield terminal

    def _execute(self, call: _ParsedCall) -> tuple[str, bool]:
        """执行一次工具调用；失败转为错误观察值回灌，模型可自愈。"""
        try:
            tool = self._tools.get(call.name)
        except KeyError as exc:
            return str(exc), True
        try:
            return tool.run(call.arguments), False
        except Exception as exc:  # 工具内部错误不终止任务，交给模型决断
            return f"工具 {call.name} 执行失败：{exc}", True

    def _is_dead_loop(self, recent_actions: list[tuple[str, str]]) -> bool:
        if len(recent_actions) < DEAD_LOOP_THRESHOLD:
            return False
        return (
            len(set(recent_actions[-DEAD_LOOP_THRESHOLD:])) == 1
        )

    def _messages_for_api(self, trace: list[dict]) -> list[dict]:
        """system 协议 + 截断后的轨迹；截断保留任务消息与最近轨迹。"""
        trimmed = trace
        if len(trace) > MAX_TRACE_MESSAGES:
            trimmed = [trace[0], *trace[-(MAX_TRACE_MESSAGES - 1) :]]
        return [{"role": "system", "content": _BASE_PROTOCOL}, *trimmed]

    @staticmethod
    def _goto(current: AgentState, nxt: AgentState) -> AgentState:
        if nxt not in TRANSITIONS[current]:
            raise RuntimeError(f"非法状态迁移：{current.name} → {nxt.name}")
        return nxt
