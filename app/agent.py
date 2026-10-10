"""ReAct Agent 引擎：统一的任务状态机循环（单 Agent = 不派发的退化特例）。

显式状态机（docs/adr/0001、ADR-0003）：节点即状态，迁移表是数据。
多 Agent 协作（docs/specs/0004）经新增状态与迁移边扩展：
THINKING→DISPATCHING→WAITING→OBSERVING，dispatch_task 是引擎特判的
特殊工具（与 finish 同先例，不进 ToolRegistry），子 agent 是本引擎的
动态装配运行（深度限一层：子 agent 的 schema 不含 dispatch_task）。
双层终止（docs/adr/0002）在每个 agent 内独立生效：模型侧 finish 工具
宣告语义完成，引擎侧迭代硬上限与死循环止损兜底。
轨迹独立于主对话：任务结束由调用方把「用户请求 + 最终答案」回写主对话。
技能走上下文通道（见 skills.py）：动态清单 + use_skill 按需展开，
run() 的 activated 参数用于 `/技能名` 显式点名的确定性预激活。
"""

import json
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from enum import Enum, auto

from .llm import LLMError, ReasoningDelta, ResponseToolCalls, TextDelta
from .observation import READ_TOOL_NAME, ResultPool, format_observation
from .skills import Skill, SkillRegistry, catalog_section
from .tools import DEFAULT_TOOL_NAMES, SKILL_TOOL_NAMES, ToolRegistry

# 判断节点连续看到相同（工具, 参数）达此次数即判死循环
DEAD_LOOP_THRESHOLD = 3
# 单条观察值回灌上限：工具输出可能巨大，截断保护上下文
DEFAULT_MAX_OBSERVATION_CHARS = 4000
# 轨迹消息上限（不含 system）：超出时保留任务消息与最近轨迹
MAX_TRACE_MESSAGES = 40
# 观察值预览在终端/摘要里的展示上限
OBSERVATION_PREVIEW_CHARS = 300

FINISH_TOOL_NAME = "finish"
DISPATCH_TOOL_NAME = "dispatch_task"

# 编排层派发闸门与并行度缺省（配置项 AGENT_MAX_SUBTASKS / AGENT_MAX_PARALLEL）
DEFAULT_MAX_SUBTASKS = 6
DEFAULT_MAX_PARALLEL = 3
# 回炉上限：critic 判 revise 后整体重跑的次数封顶（ADR-0003 派发闸门，非终止层）
REWORK_LIMIT = 1
# critic 子 agent 的迭代上限：审查是单轮交付结论，不需要长循环
CRITIC_MAX_ITERATIONS = 2

# 子 agent 未点名工具时的默认下放集：技能元工具 + 占位工具 + 旁存续读。
# 其余注册表工具（尤其 mcp__* 外部动作面）必须由编排器显式点名才下放。
WORKER_DEFAULT_TOOL_NAMES = (*SKILL_TOOL_NAMES, READ_TOOL_NAME, *DEFAULT_TOOL_NAMES)

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

_DISPATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": DISPATCH_TOOL_NAME,
        "description": (
            "把一个或多个子任务派发给子 agent 并行执行，返回各子任务结果。"
            "子任务必须自包含（子 agent 看不到本对话上下文）；需单独调用"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subtasks": {
                    "type": "array",
                    "minItems": 1,
                    "description": "子任务列表（可并行的独立子任务放一起）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "自包含任务书：写清背景、目标与验收要求",
                            },
                            "persona": {
                                "type": "string",
                                "description": "可选：子 agent 的角色/system 补充",
                            },
                            "tools": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "可选：需要下放给子 agent 的工具名"
                                    "（缺省只给技能元工具与占位工具，MCP 工具必须点名）"
                                ),
                            },
                            "skills": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "可选：预激活的技能名",
                            },
                            "provider": {
                                "type": "string",
                                "description": "可选：子任务使用的厂商（缺省沿用当前目标）",
                            },
                            "model": {
                                "type": "string",
                                "description": "可选：子任务使用的模型（缺省沿用当前目标）",
                            },
                        },
                        "required": ["task"],
                    },
                }
            },
            "required": ["subtasks"],
        },
    },
}

_BASE_PROTOCOL = (
    "你是自主执行任务的助手。逐步思考，需要事实或计算时调用提供的工具；"
    "获得足够信息后，调用 finish 工具交付面向用户的最终答案。"
)

_DISPATCH_SECTION = (
    "## 多 Agent 协作\n"
    "任务复杂或可并行时，调用 dispatch_task 把子任务派发给子 agent 并行执行：\n"
    "- 每个子任务的 task 必须自包含（写清背景、目标与验收要求），子 agent 看不到本对话上下文；\n"
    "- tools 只填确实需要下放的工具名（mcp__ 开头的外部工具不会默认下放）；"
    "skills 填需要预激活的技能名；\n"
    "- 可为子任务指定 provider/model，不填则沿用当前目标；\n"
    "- dispatch_task 需单独调用（同一回合不要夹带其他工具）；子任务结果会作为观察值返回。"
)

_CRITIC_PROTOCOL = (
    "你是任务产物的审查者（critic）。对照任务目标审查交付答案的正确性、"
    "完整性与可用性，不需要调用工具。调用 finish 交付审查结论："
    "认可交付时 answer 以「通过」开头；需要返工时 answer 以「返工」开头"
    "并附具体、可执行的改进意见。"
)


class AgentState(Enum):
    THINKING = auto()
    ACTING = auto()
    DISPATCHING = auto()
    WAITING = auto()
    OBSERVING = auto()
    JUDGING = auto()
    FINISHED = auto()
    FAILED = auto()


# 迁移表：数据驱动的状态机骨架（见 ADR-0001、ADR-0003）。
# FINISHED / FAILED 是终态，只能迁入、不能迁出。
TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.THINKING: {
        AgentState.ACTING,
        AgentState.DISPATCHING,
        AgentState.FINISHED,
        AgentState.FAILED,
    },
    AgentState.ACTING: {AgentState.OBSERVING},
    AgentState.DISPATCHING: {AgentState.WAITING},
    AgentState.WAITING: {AgentState.OBSERVING},
    AgentState.OBSERVING: {AgentState.JUDGING},
    AgentState.JUDGING: {AgentState.THINKING, AgentState.FINISHED, AgentState.FAILED},
    AgentState.FINISHED: set(),
    AgentState.FAILED: set(),
}

_TERMINALS = {AgentState.FINISHED, AgentState.FAILED}


# ---- 任务事件：引擎的对外输出，CLI 与 SSE 都按事件渲染 ----


@dataclass(frozen=True, kw_only=True)
class AgentIdentity:
    """事件的 agent 归属（多 Agent 轨迹树）。

    单 agent（编排器本尊）的缺省身份是 main/编排器/orchestrator；
    子 agent 事件带自己的 agent_id 与 parent_id（指向派发者）。
    """

    agent_id: str = "main"
    agent_name: str = "编排器"
    role: str = "orchestrator"  # orchestrator | worker | critic
    parent_id: str | None = None


@dataclass(frozen=True)
class TaskStarted(AgentIdentity):
    task: str


@dataclass(frozen=True)
class ThoughtDelta(AgentIdentity):
    """思考文本增量（逐字实时转发）。"""

    text: str


@dataclass(frozen=True)
class ActionStarted(AgentIdentity):
    """模型发起的工具调用。"""

    tool_name: str
    arguments: dict


@dataclass(frozen=True)
class ActionObserved(AgentIdentity):
    """工具执行结果（观察值）；is_error 标记失败回灌。"""

    tool_name: str
    result: str
    is_error: bool


@dataclass(frozen=True)
class TaskFinished(AgentIdentity):
    """任务结束；status 为 completed（达成）或 partial（预算耗尽/被叫停）。"""

    status: str
    answer: str
    iterations: int


@dataclass(frozen=True)
class TaskFailed(AgentIdentity):
    """任务失败（死循环止损、响应无法解析、上游请求失败等）。"""

    reason: str
    iterations: int


AgentEvent = (
    TaskStarted | ThoughtDelta | ActionStarted | ActionObserved | TaskFinished | TaskFailed
)


class CancelToken:
    """任务取消标志：POST /agent/stop 或 Ctrl+C 置位，引擎在迭代边界退出。"""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class SubtaskSpec:
    """一个子任务的装配规格（dispatch_task 解析产物）。"""

    task: str
    persona: str = ""
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    provider: str | None = None
    model: str | None = None


@dataclass(frozen=True)
class SubtaskOutcome:
    """一个子任务的执行结果（回灌编排器与终态聚合的依据）。"""

    name: str
    status: str  # completed | partial | failed
    answer: str
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()


class _SubtaskBudget:
    """派发闸门（非终止层，见 ADR-0003）：子任务总数跨批累计、含回炉重派。"""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def reserve(self, count: int) -> str | None:
        if self.used + count > self.limit:
            return (
                f"子任务数已达上限（{self.limit} 个，已用 {self.used}）："
                f"本次派发 {count} 个被拒绝，请合并子任务或直接自行完成"
            )
        self.used += count
        return None


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


def truncate_text(text: str, limit: int) -> str:
    """超长文本截断为 limit 字符以内，末尾以省略号提示。"""
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
                lines.append(f"[思考] {truncate_text(thought, 200)}")
            names = [tc["function"]["name"] for tc in message.get("tool_calls", [])]
            if names:
                lines.append(f"[动作] 调用 {'、'.join(names)}")
        elif role == "tool":
            lines.append(f"[观察] {truncate_text(message['content'], 200)}")
    return "\n".join(lines)


def _cancelled_answer(trace: list[dict], iterations: int) -> str:
    note = "（任务已被用户叫停）"
    if iterations == 0:
        return note
    return _progress_summary(trace, iterations) + "\n\n" + note


def _cancelled_terminal(trace: list[dict], iterations: int) -> TaskFinished:
    """被叫停的终态：partial + 已有进展摘要（三处叫停点共用同一形状）。"""
    return TaskFinished(
        status="partial",
        answer=_cancelled_answer(trace, iterations),
        iterations=iterations,
    )


def _subtask_status_block(outcomes: list[SubtaskOutcome]) -> str:
    """终态聚合的子任务状态清单（附在最终答案尾部）。"""
    lines = ["---", "子任务状态："]
    lines.extend(f"- {o.name}：{_status_label(o.status)}" for o in outcomes)
    return "\n".join(lines)


def _status_label(status: str) -> str:
    return {"completed": "完成", "partial": "部分完成", "failed": "失败"}.get(
        status, status
    )


@dataclass(frozen=True)
class _WorkerDone:
    """worker 结束哨兵（经事件队列回传，携带该子任务的结果）。"""

    outcome: SubtaskOutcome


@dataclass(frozen=True)
class _WorkerJob:
    """一个子 agent 的执行作业（装配产物 + 身份 + 下放披露）。"""

    engine: "AgentEngine"
    spec: SubtaskSpec
    identity: dict
    granted: tuple[str, ...]
    activated: tuple[Skill, ...]


class AgentEngine:
    """消费 LLM 流式事件并驱动思考→行动→观察→判断循环（可派发子 agent）。

    多 Agent 能力全为可选装配：allow_dispatch=False 的实例是子 agent
    （schema 不含 dispatch_task，深度限一层）；critic_enabled 只对编排器生效。
    """

    def __init__(
        self,
        llm,
        tools: ToolRegistry,
        max_iterations: int = 8,
        max_observation_chars: int = DEFAULT_MAX_OBSERVATION_CHARS,
        skills: SkillRegistry | None = None,
        catalog_max: int = 30,
        observation_pool: ResultPool | None = None,
        persona: str = "",
        allow_dispatch: bool = True,
        critic_enabled: bool = False,
        max_subtasks: int = DEFAULT_MAX_SUBTASKS,
        max_parallel: int = DEFAULT_MAX_PARALLEL,
        executor=None,
        llm_factory: Callable[[SubtaskSpec], object] | None = None,
    ):
        if not tools.names():
            raise ValueError("Agent 引擎至少需要一个工具，否则无法形成行动-观察回路")
        if max_iterations < 1:
            raise ValueError("max_iterations 至少为 1")
        if max_subtasks < 1:
            raise ValueError("max_subtasks 至少为 1")
        if max_parallel < 1:
            raise ValueError("max_parallel 至少为 1")
        self.llm = llm
        self._tools = tools
        self._skills = skills
        self._catalog_max = catalog_max
        self._max_iterations = max_iterations
        self._max_observation_chars = max_observation_chars
        self._persona = persona
        self._allow_dispatch = allow_dispatch
        self._critic_enabled = critic_enabled
        self._max_subtasks = max_subtasks
        self._max_parallel = max_parallel
        self._executor = executor
        self._llm_factory = llm_factory
        self.observation_pool = observation_pool or ResultPool()
        # 本次运行的 agent 身份（事件标注用）；缺省 = 编排器本尊
        self._ident: dict = {}
        # finish / dispatch_task 是协议工具：随请求注入 schema，不属于注册表
        protocol = [_FINISH_SCHEMA]
        if allow_dispatch:
            protocol.insert(0, _DISPATCH_SCHEMA)
        self._schemas = [*tools.openai_schemas(), *protocol]

    def run(
        self,
        task: str,
        provider: str | None = None,
        model: str | None = None,
        activated: tuple[Skill, ...] = (),
        *,
        agent_id: str = "main",
        agent_name: str = "编排器",
        role: str = "orchestrator",
        parent_id: str | None = None,
        cancel: CancelToken | None = None,
    ) -> Iterator[AgentEvent]:
        """执行一次任务，逐个产出事件；终态事件必为 TaskFinished 或 TaskFailed。

        activated 为 `/技能名` 显式点名的预激活技能：guide 直接进 system
        prompt，不等模型 use_skill（清单与自主激活仍同时在场）。
        编排器（role=orchestrator）且 critic_enabled 时，交付前追加一次
        critic 收尾审查（revise 则整体重跑一次封顶，见 ADR-0003）。
        """
        self._ident = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "role": role,
            "parent_id": parent_id,
        }
        budget = _SubtaskBudget(self._max_subtasks)
        if role == "orchestrator" and self._critic_enabled:
            return self._run_with_critic(task, provider, model, activated, cancel, budget)
        return self._run_once(task, provider, model, activated, cancel, budget)

    # ---- critic 收尾：评审者/执行者分工（docs/specs/0004） ----

    def _run_with_critic(
        self,
        task: str,
        provider: str | None,
        model: str | None,
        activated: tuple[Skill, ...],
        cancel: CancelToken | None,
        budget: _SubtaskBudget,
    ) -> Iterator[AgentEvent]:
        current_task = task
        for attempt in range(REWORK_LIMIT + 1):
            terminal = yield from self._run_once(
                current_task, provider, model, activated, cancel, budget
            )
            if not (isinstance(terminal, TaskFinished) and terminal.status == "completed"):
                return
            verdict, opinion = yield from self._run_critic(
                task, terminal.answer, provider, model, cancel
            )
            if verdict != "revise":
                return
            if attempt < REWORK_LIMIT:
                # 回炉：审查意见并入任务描述整体重跑一次（预算跨次累计）
                current_task = (
                    f"{task}\n\n（上一版答案未通过审查，审查意见如下，请据此改进后重新交付）\n"
                    f"{opinion}"
                )
                continue
            # 二次仍未过：按现状交付并附审查意见
            yield self._tag(
                replace(terminal, answer=f"{terminal.answer}\n\n（审查意见：{opinion}）")
            )
            return

    def _run_critic(
        self,
        task: str,
        answer: str,
        provider: str | None,
        model: str | None,
        cancel: CancelToken | None,
    ) -> Iterator[AgentEvent]:
        """审查子 agent：输入任务 + 交付答案，产出 pass / revise + 意见。"""
        critic_task = (
            f"请审查以下任务的交付答案。\n\n## 任务\n{task}\n\n## 交付答案\n{answer}"
        )
        spec = SubtaskSpec(task=critic_task, persona=_CRITIC_PROTOCOL)
        critic_tools = self._tools.subset(WORKER_DEFAULT_TOOL_NAMES)
        if not critic_tools.names():
            critic_tools = self._tools
        engine = AgentEngine(
            self._llm_for(spec),
            critic_tools,
            max_iterations=CRITIC_MAX_ITERATIONS,
            max_observation_chars=self._max_observation_chars,
            skills=self._skills,
            catalog_max=self._catalog_max,
            observation_pool=self.observation_pool,
            persona=_CRITIC_PROTOCOL,
            allow_dispatch=False,
        )
        verdict, opinion = "pass", ""
        for event in engine.run(
            critic_task,
            provider,
            model,
            agent_id="critic",
            agent_name="审查 agent",
            role="critic",
            parent_id=self._ident["agent_id"],
            cancel=cancel,
        ):
            yield event
            if isinstance(event, TaskFinished):
                opinion = event.answer
                verdict = "revise" if event.answer.strip().startswith("返工") else "pass"
            elif isinstance(event, TaskFailed):
                # 审查自身失败不阻塞交付（质量兜底让位于可用性）
                verdict, opinion = "pass", ""
        return verdict, opinion

    # ---- 主循环：思考→（行动|派发）→观察→判断 ----

    def _run_once(
        self,
        task: str,
        provider: str | None,
        model: str | None,
        activated: tuple[Skill, ...],
        cancel: CancelToken | None,
        budget: _SubtaskBudget | None,
    ) -> Iterator[AgentEvent]:
        yield self._tag(TaskStarted(task=task))
        trace: list[dict] = [{"role": "user", "content": task}]
        recent_actions: list[tuple[str, str]] = []
        outcomes: list[SubtaskOutcome] = []
        iterations = 0
        parsed_calls: list[_ParsedCall] = []
        pending_dispatch: tuple[_ParsedCall, list[_ParsedCall]] | None = None
        terminal: AgentEvent | None = None

        state = AgentState.THINKING
        while state not in _TERMINALS:
            if state is AgentState.THINKING:
                if self._cancelled(cancel):
                    terminal = _cancelled_terminal(trace, iterations)
                    state = self._goto(state, AgentState.FINISHED)
                    continue
                iterations += 1
                thought_parts: list[str] = []
                calls = []
                try:
                    stream = self.llm.chat_events(
                        self._messages_for_api(trace, activated),
                        provider,
                        model,
                        self._schemas,
                    )
                    try:
                        for event in stream:
                            # 叫停要在厂商流内即时生效（SPEC-0005 T1）：只在迭代
                            # 边界轮询的话，卡在一次长调用里时「停止」形同虚设
                            if self._cancelled(cancel):
                                break
                            if isinstance(event, TextDelta):
                                thought_parts.append(event.text)
                                yield self._tag(ThoughtDelta(text=event.text))
                            elif isinstance(event, ReasoningDelta):
                                # 思考链只透传展示，不写入轨迹：历史回放只保留正式内容
                                yield self._tag(ThoughtDelta(text=event.text))
                            elif isinstance(event, ResponseToolCalls):
                                calls = list(event.tool_calls)
                    finally:
                        close = getattr(stream, "close", None)
                        if close is not None:
                            close()
                except LLMError as exc:
                    state = self._goto(state, AgentState.FAILED)
                    terminal = TaskFailed(reason=str(exc), iterations=iterations)
                    continue

                if self._cancelled(cancel):
                    # 流内叫停：丢弃本轮半截产出，按 partial 收尾并保留已有进展
                    state = self._goto(state, AgentState.FINISHED)
                    terminal = _cancelled_terminal(trace, iterations)
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

                dispatch_call = next(
                    (call for call in parsed_calls if call.name == DISPATCH_TOOL_NAME),
                    None,
                )
                trace.append(_assistant_message(thought, parsed_calls))
                if dispatch_call is not None:
                    # dispatch_task 优先：同一回合的其他工具调用不执行（见 _DISPATCH_SECTION）
                    ignored = [c for c in parsed_calls if c is not dispatch_call]
                    pending_dispatch = (dispatch_call, ignored)
                    state = self._goto(state, AgentState.DISPATCHING)
                    continue
                state = self._goto(state, AgentState.ACTING)
            elif state is AgentState.ACTING:
                for call in parsed_calls:
                    yield self._tag(
                        ActionStarted(tool_name=call.name, arguments=call.arguments)
                    )
                    result, is_error = self._execute(call)
                    yield self._tag(
                        ActionObserved(
                            tool_name=call.name, result=result, is_error=is_error
                        )
                    )
                    trace.append(_tool_message(call.id, result))
                    recent_actions.append((call.name, call.arguments_json))
                state = self._goto(state, AgentState.OBSERVING)
            elif state is AgentState.DISPATCHING:
                dispatch_call, ignored = pending_dispatch  # type: ignore[misc]
                yield self._tag(
                    ActionStarted(
                        tool_name=DISPATCH_TOOL_NAME, arguments=dispatch_call.arguments
                    )
                )
                specs, error = self._plan_subtasks(dispatch_call.arguments, budget)
                if error is not None:
                    # 派发被拒（预算/参数）：错误观察值回灌，模型可自愈调整
                    yield self._tag(
                        ActionObserved(
                            tool_name=DISPATCH_TOOL_NAME, result=error, is_error=True
                        )
                    )
                    trace.append(_tool_message(dispatch_call.id, error))
                    for call in ignored:
                        trace.append(_tool_message(call.id, _IGNORED_NOTE))
                    recent_actions.append(
                        (DISPATCH_TOOL_NAME, dispatch_call.arguments_json)
                    )
                    pending_dispatch = None
                    state = self._goto(state, AgentState.WAITING)
                    continue
                report, is_error, batch_outcomes = yield from self._dispatch(
                    specs, provider, model, cancel
                )
                yield self._tag(
                    ActionObserved(
                        tool_name=DISPATCH_TOOL_NAME, result=report, is_error=is_error
                    )
                )
                trace.append(_tool_message(dispatch_call.id, report))
                for call in ignored:
                    trace.append(_tool_message(call.id, _IGNORED_NOTE))
                recent_actions.append((DISPATCH_TOOL_NAME, dispatch_call.arguments_json))
                outcomes.extend(batch_outcomes)
                pending_dispatch = None
                state = self._goto(state, AgentState.WAITING)
            elif state is AgentState.WAITING:
                state = self._goto(state, AgentState.OBSERVING)
            elif state is AgentState.OBSERVING:
                state = self._goto(state, AgentState.JUDGING)
            else:  # JUDGING：双层终止的引擎侧谓词，不发起额外模型调用
                if self._cancelled(cancel):
                    terminal = _cancelled_terminal(trace, iterations)
                    state = self._goto(state, AgentState.FINISHED)
                elif self._is_dead_loop(recent_actions):
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
        terminal = self._aggregate(terminal, outcomes)
        yield self._tag(terminal)
        return terminal

    # ---- 派发：子 agent 动态装配 + 并行执行 + 结果回灌 ----

    def _dispatch(
        self,
        specs: list[SubtaskSpec],
        provider: str | None,
        model: str | None,
        cancel: CancelToken | None,
    ) -> Iterator[tuple]:
        """并行执行子任务，实时透传子 agent 事件，返回 (报告, is_error, 结果列表)。"""
        results: queue.Queue = queue.Queue()
        jobs: list[_WorkerJob] = []
        for index, spec in enumerate(specs, 1):
            identity = {
                "agent_id": f"sub-{index}-{id(self)}",
                "agent_name": f"子任务{index}",
                "role": "worker",
                "parent_id": self._ident["agent_id"],
            }
            try:
                engine, granted, activated = self._worker_engine(spec)
            except ValueError as exc:
                results.put(
                    _WorkerDone(
                        SubtaskOutcome(
                            name=identity["agent_name"],
                            status="failed",
                            answer=f"子任务装配失败：{exc}",
                        )
                    )
                )
                continue
            jobs.append(_WorkerJob(engine, spec, identity, granted, activated))

        own_executor = self._executor is None
        executor = self._executor or self._make_executor()
        try:
            for job in jobs:
                executor.submit(_run_worker, job, cancel, results)
            outcomes: list[SubtaskOutcome] = []
            expected = len(specs)
            while len(outcomes) < expected:
                item = results.get()
                if isinstance(item, _WorkerDone):
                    outcomes.append(item.outcome)
                else:
                    yield item  # 子 agent 事件（已由其引擎标注身份）实时透传
        finally:
            if own_executor:
                executor.shutdown(wait=True)

        # 结果按派发顺序排列（子任务编号），装配失败项也占位其中
        by_name = {o.name: o for o in outcomes}
        ordered = [
            by_name[f"子任务{index}"]
            for index in range(1, len(specs) + 1)
            if f"子任务{index}" in by_name
        ]
        report = _compose_report(ordered)
        is_error = bool(ordered) and all(o.status == "failed" for o in ordered)
        return report, is_error, ordered

    def _make_executor(self):
        from concurrent.futures import ThreadPoolExecutor

        return ThreadPoolExecutor(
            max_workers=self._max_parallel, thread_name_prefix="subagent"
        )

    def _plan_subtasks(
        self, arguments: dict, budget: _SubtaskBudget | None
    ) -> tuple[list[SubtaskSpec], str | None]:
        """校验 dispatch_task 参数并装配子任务规格；失败返回错误文本（回灌自愈）。"""
        raw = arguments.get("subtasks")
        if not isinstance(raw, list) or not raw:
            return [], "subtasks 必须是非空数组"
        specs: list[SubtaskSpec] = []
        for item in raw:
            if not isinstance(item, dict):
                return [], "每个子任务必须是 JSON 对象"
            task_text = str(item.get("task") or "").strip()
            if not task_text:
                return [], "子任务缺少 task（必须是自包含的任务书）"
            specs.append(
                SubtaskSpec(
                    task=task_text,
                    persona=str(item.get("persona") or "").strip(),
                    tools=tuple(_name_list(item.get("tools"))),
                    skills=tuple(_name_list(item.get("skills"))),
                    provider=_optional_str(item.get("provider")),
                    model=_optional_str(item.get("model")),
                )
            )
        if budget is not None:
            error = budget.reserve(len(specs))
            if error is not None:
                return [], error
        return specs, None

    def _worker_engine(
        self, spec: SubtaskSpec
    ) -> tuple["AgentEngine", tuple[str, ...], tuple[Skill, ...]]:
        """动态装配子 agent：默认最小工具集 + 显式点名；MCP 等外部工具不默认下放。"""
        granted = [name for name in WORKER_DEFAULT_TOOL_NAMES if self._has_tool(name)]
        for name in spec.tools:
            if self._has_tool(name) and name not in granted:
                granted.append(name)
        if not granted:
            raise ValueError("未下放任何工具（默认工具集为空且未点名 tools）")
        skills = []
        for name in spec.skills:
            entry = self._skills.get(name) if self._skills is not None else None
            if entry is not None and entry.enabled:
                skills.append(entry.skill)
        engine = AgentEngine(
            self._llm_for(spec),
            self._tools.subset(granted),
            max_iterations=self._max_iterations,
            max_observation_chars=self._max_observation_chars,
            skills=self._skills,
            catalog_max=self._catalog_max,
            observation_pool=self.observation_pool,
            persona=spec.persona,
            allow_dispatch=False,
        )
        return engine, tuple(granted), tuple(skills)

    def _has_tool(self, name: str) -> bool:
        try:
            self._tools.get(name)
            return True
        except KeyError:
            return False

    def _llm_for(self, spec: SubtaskSpec):
        return self._llm_factory(spec) if self._llm_factory is not None else self.llm

    def _aggregate(
        self, terminal: AgentEvent, outcomes: list[SubtaskOutcome]
    ) -> AgentEvent:
        """终态聚合：任一子任务 failed/partial → 整体最多 partial；答案附状态清单。"""
        if not outcomes or not isinstance(terminal, TaskFinished):
            return terminal
        status = terminal.status
        if status == "completed" and any(o.status != "completed" for o in outcomes):
            status = "partial"
        return replace(
            terminal,
            status=status,
            answer=terminal.answer + "\n\n" + _subtask_status_block(outcomes),
        )

    def _tag(self, event: AgentEvent) -> AgentEvent:
        """给本引擎产出的事件标注 agent 身份（子 agent 事件由各自引擎标注）。"""
        return replace(event, **self._ident)

    def _execute(self, call: _ParsedCall) -> tuple[str, bool]:
        """执行一次工具调用；失败转为错误观察值回灌，模型可自愈。

        观察值默认按视图大小截断（全文旁存句柄池，read_tool_result 续读）；
        full_result 工具（如 use_skill 的技能正文）是刻意加载的上下文，完整回灌。
        """
        try:
            tool = self._tools.get(call.name)
        except KeyError as exc:
            return str(exc), True
        try:
            result = tool.run(call.arguments)
        except Exception as exc:  # 工具内部错误不终止任务，交给模型决断
            return (
                format_observation(
                    f"工具 {call.name} 执行失败：{exc}",
                    limit=tool.max_observation_chars,
                    pool=self.observation_pool,
                    default_limit=self._max_observation_chars,
                ),
                True,
            )
        if tool.full_result:
            return result, False
        return (
            format_observation(
                result,
                limit=tool.max_observation_chars,
                pool=self.observation_pool,
                default_limit=self._max_observation_chars,
            ),
            False,
        )

    def _is_dead_loop(self, recent_actions: list[tuple[str, str]]) -> bool:
        if len(recent_actions) < DEAD_LOOP_THRESHOLD:
            return False
        return (
            len(set(recent_actions[-DEAD_LOOP_THRESHOLD:])) == 1
        )

    @staticmethod
    def _cancelled(cancel: CancelToken | None) -> bool:
        return cancel is not None and cancel.cancelled

    def _messages_for_api(
        self, trace: list[dict], activated: tuple[Skill, ...] = ()
    ) -> list[dict]:
        """system 协议 + 截断后的轨迹；截断保留任务消息与最近轨迹。

        技能清单每次调用现场生成，安装/卸载/启停即时反映（动态清单）。
        """
        trimmed = trace
        if len(trace) > MAX_TRACE_MESSAGES:
            trimmed = [trace[0], *trace[-(MAX_TRACE_MESSAGES - 1) :]]
        return [
            {"role": "system", "content": self._system_prompt(activated)},
            *trimmed,
        ]

    def _system_prompt(self, activated: tuple[Skill, ...] = ()) -> str:
        """基础协议 + 预激活技能的受控文本块 + 动态技能清单（编排器附协作说明）。"""
        sections = [_BASE_PROTOCOL]
        if self._persona:
            sections.insert(0, self._persona)
        if self._allow_dispatch:
            sections.append(_DISPATCH_SECTION)
        for skill in activated:
            dependencies = "、".join(skill.tools) or "无"
            sections.append(
                f"## 已激活技能：{skill.name}\n{skill.guide}\n"
                f"（建议使用的工具：{dependencies}）"
            )
        if self._skills is not None:
            catalog = catalog_section(self._skills, self._catalog_max)
            if catalog:
                sections.append(catalog)
        return "\n\n".join(sections)

    @staticmethod
    def _goto(current: AgentState, nxt: AgentState) -> AgentState:
        if nxt not in TRANSITIONS[current]:
            raise RuntimeError(f"非法状态迁移：{current.name} → {nxt.name}")
        return nxt


_IGNORED_NOTE = "（已忽略：本回合 dispatch_task 派发优先，该工具调用未执行）"


def _name_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _optional_str(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _run_worker(
    job: _WorkerJob,
    cancel: CancelToken | None,
    results: "queue.Queue",
) -> None:
    """子 agent 线程入口：事件逐个进队列（实时透传），结束以哨兵回传结果。"""
    outcome: SubtaskOutcome | None = None

    def record(status: str, answer: str) -> SubtaskOutcome:
        return SubtaskOutcome(
            name=job.identity["agent_name"],
            status=status,
            answer=answer,
            tools=job.granted,
            skills=tuple(s.name for s in job.activated),
        )

    try:
        for event in job.engine.run(
            job.spec.task,
            job.spec.provider,
            job.spec.model,
            activated=job.activated,
            agent_id=job.identity["agent_id"],
            agent_name=job.identity["agent_name"],
            role=job.identity["role"],
            parent_id=job.identity["parent_id"],
            cancel=cancel,
        ):
            results.put(event)
            if isinstance(event, TaskFinished):
                outcome = record(event.status, event.answer)
            elif isinstance(event, TaskFailed):
                outcome = record("failed", event.reason)
    except Exception as exc:  # 子 agent 内部异常不炸整个编排
        outcome = record("failed", f"子任务执行异常：{exc}")
    finally:
        if outcome is None:
            outcome = record("failed", "子任务未产出终态")
        results.put(_WorkerDone(outcome))


def _compose_report(outcomes: list[SubtaskOutcome]) -> str:
    """把子任务结果合成一条观察值回灌编排器（含能力下放披露）。"""
    lines: list[str] = ["子任务派发结果："]
    for outcome in outcomes:
        lines.append(f"### {outcome.name}（{_status_label(outcome.status)}）")
        lines.append(
            f"下放工具：{'、'.join(outcome.tools) or '缺省'}；"
            f"预激活技能：{'、'.join(outcome.skills) or '无'}"
        )
        lines.append(outcome.answer or "（无产出）")
    return "\n".join(lines)
