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
    POST /chat/stream     同 /chat，逐 token SSE 流式推送（含 reasoning_delta 思考帧、
                          action/observation 技能步骤帧）；消息以 /技能名 开头时
                          显式点名技能，确定性升级为任务通道（预激活）
    POST /agent/stream    发起自主任务 {"task": "...", ...}，思考/动作/观察 SSE 事件流
    GET  /skills          已装技能列表（元数据 + 启停状态 + 启动加载错误）
    GET  /skills/{name}   技能详情（含 SKILL.md 原文）
    POST /skills/{name}/enable     启用技能
    POST /skills/{name}/disable    禁用技能
    DELETE /skills/{name}          删除技能包
    GET  /export          导出主对话历史，?format=markdown(默认)|json
    POST /reset           清空对话上下文
"""

import json
import threading
from collections.abc import Iterator
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .agent import (
    DEFAULT_MAX_OBSERVATION_CHARS,
    ActionObserved,
    ActionStarted,
    AgentEngine,
    TaskFailed,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
    truncate_text,
)
from .config import PROJECT_ROOT, PROVIDERS, ConfigError, Settings, resolve_target
from .conversation import Conversation
from .export import to_json, to_markdown
from .llm import LLMClient, LLMError, ReasoningDelta, ResponseToolCalls
from .skills import SkillEntry, SkillError, SkillRegistry, catalog_section, resolve_skills_dir
from .tools import SKILL_TOOL_NAMES, default_registry, skill_tools

settings = Settings()
llm = LLMClient(settings)
conversation = Conversation(
    system_prompt=None, max_messages=settings.max_context_messages
)


def _build_wiring(skills_dir: Path):
    """构建工具/技能注册表与 Agent 引擎（测试可换技能目录重建整套）。

    技能元工具先注册（create_skill 的校验基准含元工具），再热加载技能包。
    """
    tools = default_registry()
    skills = SkillRegistry(skills_dir, tools=tools)
    for tool in skill_tools(skills):
        tools.register(tool)
    errors = skills.reload()
    engine = AgentEngine(
        llm,
        tools,
        max_iterations=settings.agent_max_iterations,
        skills=skills,
        catalog_max=settings.skills_catalog_max,
    )
    return tools, skills, errors, engine


tool_registry, skill_registry, skill_load_errors, agent_engine = _build_wiring(
    resolve_skills_dir(settings.skills_dir, PROJECT_ROOT)
)

# 单会话范围：串行化对话轮次，避免并发 /chat 互相污染同一份历史
_chat_lock = threading.Lock()

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="multiagent",
    description="内部网关场景 Agent —— 第三步：技能市场（SKILL.md 文件化 + 动态清单 + 双通道调用）",
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


# ---- 聊天通道：动态清单 + 有界迷你工具循环（技能自主激活） ----


def _chat_tool_call(
    call, offered: set[str]
) -> tuple[dict, dict, tuple[str, ...]]:
    """执行一次聊天工具调用，返回 (观察帧, tool 消息, 本次激活的依赖工具)。

    工具集闸门：仅元工具与已激活技能声明的依赖工具可调用；
    use_skill 成功后其依赖工具加入 offered（本轮内可用）。
    """
    call_name = call.name
    try:
        arguments = json.loads(call.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("参数不是 JSON 对象")
    except ValueError as exc:
        text = f"{call_name} 的参数无法解析: {exc}"
        return (
            {"type": "observation", "tool": call_name, "text": text, "is_error": True},
            {"role": "tool", "tool_call_id": call.id, "content": text},
            (),
        )

    def observed(text: str, is_error: bool) -> tuple[dict, dict, tuple[str, ...]]:
        frame = {"type": "observation", "tool": call_name, "text": text, "is_error": is_error}
        message = {"role": "tool", "tool_call_id": call.id, "content": text}
        return frame, message, ()

    try:
        tool = tool_registry.get(call_name)
    except KeyError:
        return observed(f"未注册的工具: {call_name}", True)
    if call_name not in offered:
        return observed(f"工具 {call_name} 未激活：请先用 use_skill 激活对应技能", True)
    try:
        result = tool.run(arguments)
    except Exception as exc:  # 工具内部错误不终止本轮，交给模型决断
        return observed(
            truncate_text(f"工具 {call_name} 执行失败：{exc}", DEFAULT_MAX_OBSERVATION_CHARS),
            True,
        )
    activated: tuple[str, ...] = ()
    if call_name == "use_skill":
        entry = skill_registry.get(str(arguments.get("name", "")).strip())
        if entry is not None:
            activated = entry.skill.tools
    if not tool.full_result:
        result = truncate_text(result, DEFAULT_MAX_OBSERVATION_CHARS)
    frame, message, _ = observed(result, False)
    return frame, message, activated


def _chat_frames(req: ChatRequest, target) -> Iterator[str]:
    """聊天迷你工具循环：use_skill 激活后本轮可用其依赖工具。

    仅最终文本（无工具调用那一轮）写入主对话；技能正文与工具中间态
    是上下文通道产物，不落历史（与思考链同等待遇）。
    达到 CHAT_MAX_TOOL_TURNS 上限时以 error 帧收尾并回滚本次提问。
    """
    with _chat_lock:
        conversation.add("user", req.message)
        system = catalog_section(skill_registry, settings.skills_catalog_max)
        messages: list[dict] = []
        if system:
            messages.append(
                {
                    "role": "system",
                    "content": "你在对话中按需借助技能：用户诉求匹配技能清单时，"
                    "先调用 use_skill(名称) 激活并按指引执行；"
                    "未列入清单的技能可用 search_skills 检索。\n\n" + system,
                }
            )
        messages.extend(conversation.messages_for_api())
        schemas = [tool_registry.get(name).openai_schema() for name in SKILL_TOOL_NAMES]
        offered = set(SKILL_TOOL_NAMES)
        try:
            for _ in range(settings.chat_max_tool_turns):
                text_parts: list[str] = []
                calls = []
                for event in llm.chat_events(messages, req.provider, req.model, schemas):
                    if isinstance(event, ReasoningDelta):
                        yield _sse({"type": "reasoning_delta", "text": event.text})
                    elif isinstance(event, ResponseToolCalls):
                        calls = list(event.tool_calls)
                    else:
                        text_parts.append(event.text)
                        yield _sse({"type": "delta", "text": event.text})
                if not calls:
                    conversation.add("assistant", "".join(text_parts))
                    yield _sse(
                        {"type": "done", "provider": target.provider, "model": target.model}
                    )
                    return
                assistant = {
                    "role": "assistant",
                    "content": "".join(text_parts),
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in calls
                    ],
                }
                messages.append(assistant)
                for call in calls:
                    yield _sse(
                        {"type": "action", "tool": call.name, "arguments": _safe_args(call.arguments)}
                    )
                    frame, message, activated = _chat_tool_call(call, offered)
                    yield _sse(frame)
                    messages.append(message)
                    for name in activated:
                        if name not in offered:
                            offered.add(name)
                            schemas.append(tool_registry.get(name).openai_schema())
            conversation.pop_last()
            yield _sse(
                {
                    "type": "error",
                    "detail": f"本轮技能/工具调用超过上限（{settings.chat_max_tool_turns} 轮），"
                    "请拆分请求，或点「任务」以自主任务执行",
                }
            )
        except LLMError as exc:
            conversation.pop_last()
            yield _sse({"type": "error", "detail": str(exc)})


def _safe_args(arguments_json: str) -> dict:
    try:
        arguments = json.loads(arguments_json or "{}")
    except ValueError:
        return {}
    return arguments if isinstance(arguments, dict) else {}


@app.post("/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """逐 token SSE 聊天；行为与 /chat 一致，改为流式推送。

    消息以 /技能名 开头且命中已启用技能时，确定性路由到任务通道并
    预激活该技能（帧型为任务事件流）；其余消息走聊天迷你工具循环。
    非 200 的配置错误在开流之前抛出，开流之后的上游错误以 error 帧推送。
    """
    try:
        target = resolve_target(settings, req.provider, req.model)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    entry = skill_registry.match_prefix(req.message)
    if entry is not None:
        return StreamingResponse(
            _agent_frames(req.message, req.provider, req.model, activated=(entry.skill,)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
    return StreamingResponse(
        _chat_frames(req, target),
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


def _agent_frames(
    task: str,
    provider: str | None,
    model: str | None,
    activated: tuple = (),
) -> Iterator[str]:
    """Agent 任务 SSE：思考逐字实时转发，动作/观察按步推送，终态带原因。

    completed / partial 回写「请求 + 最终答案」到主对话；
    activated 为 /技能名 显式点名的预激活技能。
    """
    with _chat_lock:
        answer: str | None = None
        for event in agent_engine.run(task, provider, model, activated=activated):
            yield _sse(_agent_event_frame(event))
            if isinstance(event, TaskFinished):
                answer = event.answer
        if answer is not None:
            conversation.add("user", task)
            conversation.add("assistant", answer)


@app.post("/agent/stream")
def agent_stream(req: AgentRequest) -> StreamingResponse:
    """Agent 任务 SSE 流。非 200 的配置错误在开流之前抛出；
    引擎自身会把上游错误编码为 failed 帧，因此流内不会抛出异常。
    """
    try:
        resolve_target(settings, req.provider, req.model)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return StreamingResponse(
        _agent_frames(req.task, req.provider, req.model),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


# ---- 技能管理面 ----


def _entry_json(entry: SkillEntry) -> dict:
    return {
        "name": entry.skill.name,
        "description": entry.skill.description,
        "tools": list(entry.skill.tools),
        "enabled": entry.enabled,
        "source": entry.source,
    }


@app.get("/skills")
def list_skills() -> dict:
    return {
        "skills": [_entry_json(entry) for entry in skill_registry.entries()],
        "load_errors": skill_load_errors,
    }


@app.get("/skills/{name}")
def get_skill(name: str) -> dict:
    entry = skill_registry.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"未找到技能: {name}")
    try:
        content = skill_registry.pack_file(name).read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"技能文件读取失败: {exc}") from exc
    return {**_entry_json(entry), "content": content}


def _set_enabled(name: str, enabled: bool) -> dict:
    with _chat_lock:
        try:
            entry = skill_registry.set_enabled(name, enabled)
        except SkillError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "skill": _entry_json(entry)}


@app.post("/skills/{name}/enable")
def enable_skill(name: str) -> dict:
    return _set_enabled(name, True)


@app.post("/skills/{name}/disable")
def disable_skill(name: str) -> dict:
    return _set_enabled(name, False)


@app.delete("/skills/{name}")
def delete_skill(name: str) -> dict:
    with _chat_lock:
        try:
            skill_registry.remove(name)
        except SkillError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
