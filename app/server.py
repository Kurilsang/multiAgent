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
    GET  /mcp             MCP 连接定义列表（状态 + 工具 + 授权状态 + 加载错误）
    POST /mcp/enable|disable|remove  启用/禁用/删除 MCP 服务 {"name": "..."}
    POST /mcp/reload      重读连接定义并整体重连
    POST /mcp/reauth      OAuth 服务重新授权（清 token 并重连）{"name": "..."}
    POST /mcp/install     安装 {"source": "catalog"|"local", "catalog_id"/"path": "...", "env_values": {...}}
    GET  /skills/presets  预设平台源清单（一键安装用）
    GET  /skills/{name}   技能详情（含 SKILL.md 原文）
    POST /skills/{name}/enable     启用技能
    POST /skills/{name}/disable    禁用技能
    DELETE /skills/{name}          删除技能包
    POST /skills/install  安装 {"source": "git"|"local", "url"|"path": "...", "subpath": "..."}
    POST /skills/install  安装 {"source": "catalog"|"git"|"local", ...}
    GET  /market/sources  在线目录各源状态（爬取服务代理）
    GET  /market/search   在线目录搜索 ?q=&source=&kind=&page=&page_size=
    GET  /market/detail   在线目录详情 ?id=&source=（SKILL.md 预览 + 审计徽标）
    POST /market/refresh  异步触发目录爬取 {"source"?: "..."}（立即返回，结果经 /market/sources）
    GET  /export          导出主对话历史，?format=markdown(默认)|json
    POST /reset           清空对话上下文
"""

import atexit
import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from json import dumps as json_dumps
from json import loads as json_loads
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from .agent import (
    ActionObserved,
    ActionStarted,
    AgentEngine,
    CancelToken,
    TaskFailed,
    TaskFinished,
    TaskStarted,
    ThoughtDelta,
)
from .audit import TaskAuditLog, has_terminal, read_task_log, read_task_logs
from .config import PROJECT_ROOT, PROVIDERS, ConfigError, Settings, resolve_target
from .context_budget import context_meta as build_context_meta
from .conversation import Conversation
from .export import to_json, to_markdown, traces_to_markdown
from .llm import LLMClient, LLMError, ReasoningDelta, ResponseToolCalls
from .mcp import (
    McpError,
    McpManager,
    load_servers,
    parse_install_text,
    resolve_config_path,
    sdk_client_factory,
    write_dotenv,
)
from .mcp_auth import default_oauth
from .observation import (
    READ_TOOL_NAME,
    ResultPool,
    format_observation,
    read_tool_result_tool,
)
from .sessions import (
    DEFAULT_SESSION_ID,
    SessionStore,
    SessionStoreError,
    session_name_from,
)
from .skills import (
    PRESET_SOURCES,
    SkillEntry,
    SkillError,
    SkillRegistry,
    catalog_section,
    install_from_dir,
    install_from_git,
    parse_external_skill,
    resolve_skills_dir,
)
from .tasks import SubscriberOverflow, TaskBusy, TaskHub, TaskRecord
from .tools import SKILL_TOOL_NAMES, default_registry, skill_tools

settings = Settings()
llm = LLMClient(settings)
# 审计日志落点（None = logs/tasks；测试注入临时目录保持封闭）
audit_log_dir: Path | None = None
# 会话库落点（None = SESSION_DB 配置；测试注入临时库保持封闭）
session_db_path: Path | None = None

# 「已中断」的用户可见文案（一处定义，回放帧与状态查询共用）
INTERRUPTED_NOTE = "⚠ 已中断（进程重启）：以上为中断前已有的进展"


def _open_session_store(path: Path | None = None) -> SessionStore:
    """打开会话库并认领数据目录（多进程共享即拒绝启动）。"""
    resolved = path or resolve_config_path(settings.session_db, PROJECT_ROOT)
    store = SessionStore(resolved)
    store.claim()
    store.ensure_default_session()
    store.start_heartbeat()
    return store


# 会话库惰性打开：导入本模块不产生磁盘写入（测试套件保持封闭，
# 不往仓库 data/ 落库）；真实启动在 __main__ 里显式预热以 fail loud。
_session_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    """取会话库（首次访问时打开）。"""
    global _session_store
    if _session_store is None:
        _session_store = _open_session_store(session_db_path)
    return _session_store


def reset_session_store(path: Path | None = None) -> None:
    """关掉当前会话库并改用新路径（测试缝：注入临时库保持封闭）。"""
    global _session_store, session_db_path
    if _session_store is not None:
        _session_store.close()
        _session_store = None
    session_db_path = path


class _SessionStoreProxy:
    """会话库惰性代理：调用点写法不变，首次触碰才真正打开。"""

    def __getattr__(self, name):
        return getattr(get_session_store(), name)


session_store = _SessionStoreProxy()


def _shutdown() -> None:
    """停机：先让在跑任务收尾落盘，再关会话库。

    顺序要紧——atexit 是后进先出，所以这里合成一个钩子显式排序；
    分开注册会让库先关、任务再写终态（写不进去）。等待是**有上限**的：
    任务线程是守护线程，不能让退出无限期挂着（SPEC-0005 T1 尽力而为）。
    """
    task_hub.stop_all()
    deadline = time.time() + 5.0
    while time.time() < deadline and task_hub.running_tasks():
        time.sleep(0.05)
    if _session_store is not None:
        _session_store.close()


atexit.register(_shutdown)


def _build_wiring(
    skills_dir: Path,
    mcp_config: Path | None = None,
    mcp_client_factory=None,
    mcp_oauth=None,
):
    """构建工具/技能/MCP 注册表与 Agent 引擎（测试可换技能目录重建整套）。

    技能元工具先注册（create_skill 的校验基准含元工具），随后 MCP 连接建连、
    工具以 mcp__* 灌入注册表（技能包可声明其为依赖），最后热加载技能包。
    mcp_config 缺省为 None = 不加载任何 MCP 条目（测试保持封闭）。
    mcp_oauth 为 OAuth 授权协作对象（issue #44）；缺省用默认实现（token 文件见
    MCP_TOKEN_FILE），测试注入假实现保证零真实授权。
    """
    tools = default_registry()
    skills = SkillRegistry(skills_dir, tools=tools)
    for tool in skill_tools(skills):
        tools.register(tool)
    pool = ResultPool()
    tools.register(
        read_tool_result_tool(pool, chunk_size=settings.observation_max_chars)
    )
    servers, mcp_parse_errors = load_servers(path=mcp_config)
    mcp = McpManager(
        servers,
        client_factory=mcp_client_factory or sdk_client_factory,
        max_tools=settings.mcp_max_tools,
        config_path=mcp_config,
        registry=tools,
        oauth=mcp_oauth if mcp_oauth is not None else default_oauth(),
    )
    mcp.load_errors.extend(mcp_parse_errors)
    mcp.connect(reserved=tools.names())
    errors = skills.reload()
    engine = AgentEngine(
        llm,
        tools,
        max_iterations=settings.agent_max_iterations,
        max_observation_chars=settings.observation_max_chars,
        skills=skills,
        catalog_max=settings.skills_catalog_max,
        observation_pool=pool,
        critic_enabled=settings.agent_critic_enabled,
        max_subtasks=settings.agent_max_subtasks,
        max_parallel=settings.agent_max_parallel,
    )
    return tools, skills, errors, engine, mcp


tool_registry, skill_registry, skill_load_errors, agent_engine, mcp_manager = (
    _build_wiring(
        resolve_skills_dir(settings.skills_dir, PROJECT_ROOT),
        mcp_config=resolve_config_path(settings.mcp_config, PROJECT_ROOT),
        mcp_client_factory=sdk_client_factory,
    )
)
atexit.register(mcp_manager.close)

# 管理面锁：只护短临界区（技能 / MCP 写操作），任务在跑时不再阻塞管理操作
# （SPEC-0005 T1）。会话历史的一致性由 SQLite 的原子追加保证。
_admin_lock = threading.Lock()
# 任务登记处：任务生命周期独立于 HTTP 连接（断连 = 退订，不置取消、不持锁）
task_hub = TaskHub()
# 停机钩子：置全部在跑任务的取消标志，让它们尽快收尾并落盘终态。
# 尽力而为——任务线程是守护线程，进程退出时不会无限等（SPEC-0005 T1）。
atexit.register(task_hub.stop_all)

STATIC_DIR = Path(__file__).parent / "static"

# 安装时密钥值的落点（测试注入点：替换为临时文件即可断言写入内容）
_DOTENV_PATH = PROJECT_ROOT / ".env"

app = FastAPI(
    title="multiagent",
    description="内部网关场景 Agent —— 第七步多会话持久化与断连续流进行中（会话落库 + 任务生命周期与 HTTP 连接解耦已落地；前六步三厂商对话、ReAct 循环、技能市场、在线目录、MCP 市场、多 Agent 协作为其底座）",
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """WebUI 聊天页面。"""
    return FileResponse(STATIC_DIR / "index.html")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    session_id: str | None = None

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
    context_meta: dict = Field(default_factory=dict)


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


# ---- 会话（SPEC-0005）：历史权威副本在会话库，内存只做当轮上下文 ----
# 会话运行锁：每会话一把，保证「同一会话同时最多一个在跑任务 / 聊天轮次」；
# 不同会话互不阻塞（这是把全局锁拆开的收益所在）。
_session_locks: dict[str, threading.Lock] = {}
_session_locks_lock = threading.Lock()


def _session_lock(session_id: str) -> threading.Lock:
    with _session_locks_lock:
        lock = _session_locks.get(session_id)
        if lock is None:
            lock = threading.Lock()
            _session_locks[session_id] = lock
        return lock


def _resolve_session(session_id: str | None) -> str:
    """解析请求的会话标识：缺省落默认会话，显式指定但不存在则 404。"""
    if not session_id or not session_id.strip():
        return session_store.ensure_default_session()
    sid = session_id.strip()
    if session_store.get_session(sid) is None:
        raise HTTPException(status_code=404, detail=f"未找到会话: {sid}")
    return sid


def _name_session_from_first_message(session_id: str, text: str) -> None:
    """无名会话以首条用户消息命名（命名规则只在 app/sessions.py 一处）。"""
    session = session_store.get_session(session_id)
    if session is None or session["name"]:
        return
    name = session_name_from(text)
    if name:
        session_store.rename_session(session_id, name)


def _conversation_for(session_id: str) -> Conversation:
    """按会话库里的历史重建模型上下文（展示全量、入模截断）。"""
    history = session_store.messages(session_id)
    return Conversation.from_history(
        [{"role": m["role"], "content": m["content"]} for m in history],
        max_messages=settings.max_context_messages,
    )


class SessionNameRequest(BaseModel):
    name: str = Field(min_length=1)


class SessionCreateRequest(BaseModel):
    name: str = ""


@app.post("/reset")
def reset() -> dict:
    """新建一段对话（SPEC-0005 Q6）：旧会话留在列表里，不销毁任何记录。"""
    session_id = session_store.create_session()
    return {"status": "ok", "session_id": session_id}


@app.get("/sessions")
def list_sessions() -> dict:
    """会话列表（最近更新在前），附默认会话标识与各会话任务状态。

    running_task_id / interrupted 让 WebUI 侧栏显示状态点（#57），并让刷新后
    能发现「本会话还有任务在跑」而接回（T5）。
    """
    sessions = session_store.list_sessions()
    for entry in sessions:
        running = task_hub.running_task_id(entry["id"])
        entry["running_task_id"] = running
        entry["interrupted"] = any(
            task["status"] == "running" and task["task_id"] != running
            for task in session_store.list_tasks(entry["id"])
        )
    return {
        "sessions": sessions,
        "default_session_id": session_store.ensure_default_session(),
    }


@app.post("/sessions")
def create_session(req: SessionCreateRequest | None = None) -> dict:
    """新建会话；不传 name 时由首条消息命名（WebUI 先建会话再发首条消息）。

    name 缺省留空，等首条消息落库时用 session_name_from 补上——
    命名规则只有一处实现（app/sessions.py），前端不再复制一份截断逻辑。
    """
    session_id = session_store.create_session(req.name if req else "")
    return {"status": "ok", "session_id": session_id}


@app.get("/sessions/{session_id}/messages")
def session_messages(session_id: str) -> dict:
    """会话历史全量（展示用）：含思考链、工具步骤、失败标记与厂商归属。"""
    session = session_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"未找到会话: {session_id}")
    return {"session": session, "messages": session_store.messages(session_id)}


def _task_replay_state(entry: dict, running_task_id: str | None = None) -> dict:
    """由一个任务索引条目推出回放状态（三处共用，避免各推一遍）。

    判定规则：索引说在跑、但登记处没有它（进程重启留下的孤儿）——
    此时若审计日志也没有编排器终态，就是**已中断**；有终态则按终态记。
    """
    log = read_task_log(path=entry["log_path"]) if entry["log_path"] else None
    events = log["events"] if log else []
    status = entry["status"]
    interrupted = False
    if status == "running" and entry["task_id"] != running_task_id:
        interrupted = not has_terminal(events)
        status = "interrupted" if interrupted else "failed"
    return {
        "task_id": entry["task_id"],
        "task": entry["task"],
        "status": status,
        "started_at": entry["started_at"],
        "finished_at": entry["finished_at"],
        "interrupted": interrupted,
        "live": entry["task_id"] == running_task_id,
        "events": events,
    }


@app.get("/sessions/{session_id}/tasks")
def session_tasks(session_id: str) -> dict:
    """会话的任务轨迹（刷新后回放用）：索引 + **已编码的帧**。

    事件以审计 JSONL 为唯一权威副本，这里只是按索引读回。帧由服务端用
    同一个编码器产出（`_agent_event_frame`）——前端直接重放进同一套时间线
    渲染器，不再在 JS 里复刻一份编码逻辑（复刻必然漂移）。
    没有终态事件的判定为**已中断**（进程重启），如实标注而不是假装完成。
    无会话归属的旧日志不进任何会话时间线（只供人工排障）。
    """
    session = session_store.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"未找到会话: {session_id}")
    running = task_hub.running_task_id(session_id)
    tasks = []
    for entry in session_store.list_tasks(session_id):
        state = _task_replay_state(entry, running)
        state["frames"] = [
            {**_agent_event_frame(event), "seq": index}
            for index, event in enumerate(state.pop("events"), 1)
        ]
        if state["interrupted"]:
            # 中断标注也由服务端给出（前端只负责显示，不再各写一份文案）
            state["frames"].append(
                {
                    "type": "failed",
                    "reason": INTERRUPTED_NOTE,
                    "iterations": 0,
                    "seq": len(state["frames"]) + 1,
                }
            )
        tasks.append(state)
    return {"session": session, "tasks": tasks, "running_task_id": running}


@app.post("/sessions/{session_id}/rename")
def rename_session(session_id: str, req: SessionNameRequest) -> dict:
    try:
        session_store.rename_session(session_id, req.name)
    except SessionStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok", "session": session_store.get_session(session_id)}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str) -> dict:
    """硬删会话（审计日志保留：它是排障资产，不随会话消失）。"""
    try:
        session_store.delete_session(session_id)
    except SessionStoreError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


@app.get("/export")
def export_conversation(
    format: str = "markdown", traces: bool = False, session_id: str | None = None
) -> Response:
    """导出某个会话的主对话历史：markdown 便于粘贴排障，json 供程序处理。

    traces=true 显式附上任务轨迹（审计日志渲染的协作树），默认只导主对话。
    """
    sid = _resolve_session(session_id)
    messages = [
        {
            "role": m["role"],
            "content": m["content"],
            **({"provider": m["provider"], "model": m["model"]} if m["provider"] else {}),
        }
        for m in session_store.messages(sid)
    ]
    task_logs = read_task_logs(audit_log_dir) if traces else []
    fmt = format.strip().lower()
    if fmt in ("markdown", "md"):
        body = to_markdown(messages, default_provider=settings.llm_provider)
        if task_logs:
            body += "\n\n---\n\n" + traces_to_markdown(task_logs)
        return Response(
            body,
            media_type="text/markdown; charset=utf-8",
        )
    if fmt == "json":
        return Response(
            to_json(
                messages,
                default_provider=settings.llm_provider,
                traces=task_logs or None,
            ),
            media_type="application/json; charset=utf-8",
        )
    raise HTTPException(
        status_code=400, detail=f"不支持的导出格式: {format!r}，可选 markdown / json"
    )


def _record_failed_turn(session_id: str, detail: str) -> None:
    """失败留痕（不回滚）：让「我问过、它失败了」在历史里看得见。"""
    session_store.append(
        session_id,
        "assistant",
        f"出错了：{detail}",
        status="failed",
    )


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    sid = _resolve_session(req.session_id)
    _reject_if_busy(sid)
    with _session_lock(sid):
        # 用户消息先落库（模型调用前），失败不回滚
        session_store.append(sid, "user", req.message)
        _name_session_from_first_message(sid, req.message)
        try:
            messages_for_model = _conversation_for(sid).messages_for_api()
            result = llm.chat(messages_for_model, req.provider, req.model)
        except ConfigError as exc:
            _record_failed_turn(sid, str(exc))
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LLMError as exc:
            _record_failed_turn(sid, str(exc))
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:
            _record_failed_turn(sid, f"内部错误: {exc}")
            raise HTTPException(status_code=500, detail=f"内部错误: {exc}") from exc
        session_store.append(
            sid,
            "assistant",
            result.content,
            provider=result.provider,
            model=result.model,
        )
    return ChatResponse(
        reply=result.content,
        provider=result.provider,
        model=result.model,
        context_meta=build_context_meta(messages_for_model, settings),
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
            format_observation(
                f"工具 {call_name} 执行失败：{exc}",
                limit=tool.max_observation_chars,
                pool=agent_engine.observation_pool,
                default_limit=settings.observation_max_chars,
            ),
            True,
        )
    activated: tuple[str, ...] = ()
    if call_name == "use_skill":
        entry = skill_registry.get(str(arguments.get("name", "")).strip())
        if entry is not None:
            activated = entry.skill.tools
    if not tool.full_result:
        result = format_observation(
            result,
            limit=tool.max_observation_chars,
            pool=agent_engine.observation_pool,
            default_limit=settings.observation_max_chars,
        )
    frame, message, _ = observed(result, False)
    return frame, message, activated


def _chat_frames(req: ChatRequest, target, session_id: str) -> Iterator[str]:
    """聊天迷你工具循环：use_skill 激活后本轮可用其依赖工具。

    持久化（SPEC-0005 Q7）：用户消息在调用模型**之前**落库；思考链与工具
    轮次（行动/观察）随最终回复一并落库，否则刷新后的页面会比实时页少一截。
    失败留痕不回滚——「我问过、它失败了」比「页面干净」更有价值。
    """
    with _session_lock(session_id):
        session_store.append(session_id, "user", req.message)
        _name_session_from_first_message(session_id, req.message)
        conversation = _conversation_for(session_id)
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
        mcp_names = [name for name in tool_registry.names() if name.startswith("mcp__")]
        base_names = [*SKILL_TOOL_NAMES, READ_TOOL_NAME, *mcp_names]
        schemas = [tool_registry.get(name).openai_schema() for name in base_names]
        offered = set(base_names)
        reasoning_parts: list[str] = []
        steps: list[dict] = []
        try:
            for _ in range(settings.chat_max_tool_turns):
                text_parts: list[str] = []
                calls = []
                for event in llm.chat_events(messages, req.provider, req.model, schemas):
                    if isinstance(event, ReasoningDelta):
                        reasoning_parts.append(event.text)
                        yield _sse({"type": "reasoning_delta", "text": event.text})
                    elif isinstance(event, ResponseToolCalls):
                        calls = list(event.tool_calls)
                    else:
                        text_parts.append(event.text)
                        yield _sse({"type": "delta", "text": event.text})
                if not calls:
                    session_store.append(
                        session_id,
                        "assistant",
                        "".join(text_parts),
                        provider=target.provider,
                        model=target.model,
                        reasoning="".join(reasoning_parts),
                        steps=steps,
                    )
                    yield _sse(
                        {
                            "type": "done",
                            "provider": target.provider,
                            "model": target.model,
                            "context_meta": build_context_meta(messages, settings),
                        }
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
                    step = {
                        "type": "action",
                        "tool": call.name,
                        "arguments": _safe_args(call.arguments),
                    }
                    steps.append(step)
                    yield _sse(step)
                    frame, message, activated = _chat_tool_call(call, offered)
                    steps.append(frame)
                    yield _sse(frame)
                    messages.append(message)
                    for name in activated:
                        if name not in offered:
                            offered.add(name)
                            schemas.append(tool_registry.get(name).openai_schema())
            detail = (
                f"本轮技能/工具调用超过上限（{settings.chat_max_tool_turns} 轮），"
                "请拆分请求，或点「任务」以自主任务执行"
            )
            session_store.append(
                session_id,
                "assistant",
                f"出错了：{detail}",
                status="failed",
                reasoning="".join(reasoning_parts),
                steps=steps,
            )
            yield _sse({"type": "error", "detail": detail})
        except LLMError as exc:
            session_store.append(
                session_id,
                "assistant",
                f"出错了：{exc}",
                status="failed",
                reasoning="".join(reasoning_parts),
                steps=steps,
            )
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

    session_id = _resolve_session(req.session_id)
    _reject_if_busy(session_id)
    entry = skill_registry.match_prefix(req.message)
    if entry is not None:
        return _task_stream(
            session_id=session_id,
            task=req.message,
            provider=req.provider,
            model=req.model,
            activated=(entry.skill,),
        )
    return StreamingResponse(
        _chat_frames(req, target, session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


class AgentRequest(BaseModel):
    task: str = Field(min_length=1)
    provider: str | None = None
    model: str | None = None
    session_id: str | None = None
    # 重连续流：带 task_id 时接回既有任务，from_seq 之后的缺口先补发
    task_id: str | None = None
    from_seq: int = Field(default=0, ge=0)

    @field_validator("task")
    @classmethod
    def _reject_blank_task(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task 不能为空白")
        return value


def _event_field(event, name: str, default=None):
    """读事件字段：既支持引擎事件（dataclass），也支持审计日志事件（dict）。

    回放路径把 JSONL 读回来的 dict 直接喂进同一套帧编码器，避免第二套渲染。
    只在字段**缺失**时取默认值；显式写入的 null 按原值返回（不静默替换），
    以免把「日志里确实没有」和「日志里写着 null」混为一谈。
    """
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def _identity_fields(event) -> dict:
    """事件的 agent 身份字段；根 agent（编排器本尊）的帧保持向后兼容不含。"""
    agent_id = _event_field(event, "agent_id", "main")
    role = _event_field(event, "role", "orchestrator")
    parent_id = _event_field(event, "parent_id", None)
    if agent_id == "main" and role == "orchestrator" and parent_id is None:
        return {}
    return {
        "agent_id": agent_id,
        "agent_name": _event_field(event, "agent_name", ""),
        "role": role,
        "parent_id": parent_id,
    }


def _agent_event_frame(event) -> dict:
    """把引擎事件（或审计日志事件 dict）编码为 SSE 载荷。

    子 agent 事件附 agent 身份字段；两类来源共用同一编码，回放不另写一套。
    """
    kind = event.get("type") if isinstance(event, dict) else None
    if kind == "TaskStarted" or isinstance(event, TaskStarted):
        frame = {"type": "task_started"}
    elif kind == "ThoughtDelta" or isinstance(event, ThoughtDelta):
        frame = {"type": "thought_delta", "text": _event_field(event, "text", "")}
    elif kind == "ActionStarted" or isinstance(event, ActionStarted):
        frame = {
            "type": "action",
            "tool": _event_field(event, "tool_name", ""),
            "arguments": _event_field(event, "arguments", {}),
        }
    elif kind == "ActionObserved" or isinstance(event, ActionObserved):
        frame = {
            "type": "observation",
            "tool": _event_field(event, "tool_name", ""),
            "text": _event_field(event, "result", ""),
            "is_error": _event_field(event, "is_error", False),
        }
    elif kind == "TaskFinished" or isinstance(event, TaskFinished):
        frame = {
            "type": "final",
            "status": _event_field(event, "status", ""),
            "answer": _event_field(event, "answer", ""),
            "iterations": _event_field(event, "iterations", 0),
        }
    elif kind == "TaskFailed" or isinstance(event, TaskFailed):
        frame = {
            "type": "failed",
            "reason": _event_field(event, "reason", ""),
            "iterations": _event_field(event, "iterations", 0),
        }
    else:
        raise RuntimeError(f"未知的引擎事件类型: {type(event).__name__}")
    frame.update(_identity_fields(event))
    return frame


# ---- 任务执行：后台运行 + SSE 只做订阅（SPEC-0005 T1） ----
# 任务生命周期不再绑定 HTTP 连接：断连 = 退订，既不置取消标志也不持有
# 会话锁，因此任务照常跑完、服务不会被一次刷新拖垮（修掉既有缺陷：
# 收尾分支从不执行 → 锁永久泄漏 → /export 超时、停止按钮失效）。


def _reject_if_busy(session_id: str) -> None:
    """同会话已有在跑任务或正在进行的聊天轮次时明确拒绝（不排队、不阻塞）。

    两个来源都要查：任务在后台跑（task_hub），而聊天轮次持有会话锁
    （`_session_lock`）。只查前者的话，一次长聊天期间发起的任务会溜进来，
    同一会话就会出现两个模型调用 + 交错的落库写入。
    """
    running = task_hub.running_task_id(session_id)
    if running is not None:
        raise HTTPException(
            status_code=409,
            detail=f"当前会话已有任务在跑（{running}）：请先停止它或等它结束",
        )
    if _session_lock(session_id).locked():
        raise HTTPException(
            status_code=409,
            detail="当前会话正有一轮对话在进行：请等它结束再发起",
        )


def _task_terminal_writeback(record: TaskRecord) -> None:
    """终态回写：completed / partial 落「请求 + 最终答案」到会话库。

    FAILED 与叫停不落答案（主对话保持一问一答的连贯性，见 CONTEXT.md）；
    答案带该任务实际的厂商/模型归属，导出与页面据此显示。
    任务索引一并补终态，供刷新后回放轨迹。
    """
    session_store.finish_task(record.task_id, record.status)
    if record.status in ("completed", "partial") and record.answer:
        session_store.append(record.session_id, "user", record.task)
        session_store.append(
            record.session_id,
            "assistant",
            record.answer,
            status=record.status,
            provider=record.provider or None,
            model=record.model or None,
        )
    elif record.status == "failed":
        session_store.append(
            record.session_id,
            "assistant",
            f"任务失败：{record.reason}",
            status="failed",
        )


def _start_task(
    *,
    session_id: str,
    task: str,
    provider: str | None,
    model: str | None,
    activated: tuple = (),
) -> TaskRecord:
    """登记并启动后台任务（审计日志逐事件落盘由 hub 负责）。"""
    audit = TaskAuditLog(task, directory=audit_log_dir, session_id=session_id)
    target = resolve_target(settings, provider, model)
    try:
        record = task_hub.start(
            session_id=session_id,
            task=task,
            runner=lambda cancel: agent_engine.run(
                task, provider, model, activated=activated, cancel=cancel
            ),
            audit=audit,
            on_terminal=_task_terminal_writeback,
            provider=target.provider,
            model=target.model,
        )
    except TaskBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # 任务索引：记录归属与日志落点（事件仍以 JSONL 为唯一权威副本）
    session_store.record_task(
        record.task_id, session_id, task, status="running", log_path=str(audit.path)
    )
    return record


def _task_frames(record: TaskRecord, from_seq: int = 0) -> Iterator[str]:
    """把任务事件流转成 SSE：先补发缺口，再转实时；断连即退订。

    消费过慢导致积压超限时**不静默截断**：补一帧 truncated 告知客户端
    从哪个序号重连（SPEC-0005 T5 的诚实语义）。
    """
    yield _sse(
        {
            "type": "task_meta",
            "task_id": record.task_id,
            "session_id": record.session_id,
            "task": record.task,
        }
    )
    try:
        for seq, ts, event in record.subscribe(from_seq):
            frame = _agent_event_frame(event)
            frame["seq"] = seq
            frame["ts"] = ts
            frame["task_id"] = record.task_id
            yield _sse(frame)
    except SubscriberOverflow as exc:
        yield _sse(
            {
                "type": "truncated",
                "task_id": record.task_id,
                "from_seq": exc.last_seq,
                "detail": f"事件积压超出上限（{exc.reason}）："
                f"请从 from_seq={exc.last_seq} 重连续流",
            }
        )

def _task_stream(
    *,
    session_id: str,
    task: str,
    provider: str | None,
    model: str | None,
    activated: tuple = (),
    task_id: str | None = None,
    from_seq: int = 0,
) -> StreamingResponse:
    """发起（或重连）任务流：非 200 的配置错误在开流之前抛出。"""
    try:
        resolve_target(settings, provider, model)
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if task_id:
        record = task_hub.get(task_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f"未找到任务: {task_id}（进程可能已重启，任务不再可接续）",
            )
    else:
        _reject_if_busy(session_id)
        record = _start_task(
            session_id=session_id,
            task=task,
            provider=provider,
            model=model,
            activated=activated,
        )
    return StreamingResponse(
        _task_frames(record, from_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/agent/stream")
def agent_stream(req: AgentRequest) -> StreamingResponse:
    """Agent 任务 SSE 流（断连不取消；凭 task_id + from_seq 可重连续流）。"""
    session_id = _resolve_session(req.session_id)
    return _task_stream(
        session_id=session_id,
        task=req.task,
        provider=req.provider,
        model=req.model,
        task_id=req.task_id,
        from_seq=req.from_seq,
    )


@app.get("/agent/tasks/{task_id}")
def task_status(task_id: str) -> dict:
    """任务状态查询：供 UI 决定是否重连、从哪个序号接。

    内存里没有但审计日志在（进程重启过）= 从日志补出状态并标 interrupted，
    不谎报 404，也不假装任务还在跑。
    """
    record = task_hub.get(task_id)
    if record is None:
        entry = session_store.get_task(task_id)
        if entry is None or not entry["log_path"]:
            raise HTTPException(status_code=404, detail=f"未找到任务: {task_id}")
        log = read_task_log(path=entry["log_path"])
        events = log["events"] if log else []
        interrupted = not has_terminal(events)
        return {
            "task_id": task_id,
            "session_id": entry["session_id"],
            "task": entry["task"],
            "status": "interrupted" if interrupted else entry["status"],
            "answer": "",
            "reason": "已中断（进程重启）" if interrupted else "",
            "iterations": 0,
            "events": len(events),
            "finished": True,
            "replayed": True,
        }
    return {
        "task_id": record.task_id,
        "session_id": record.session_id,
        "task": record.task,
        "status": record.status,
        "answer": record.answer,
        "reason": record.reason,
        "iterations": record.iterations,
        "events": record.event_count,
        "finished": record.finished,
    }


@app.get("/agent/tasks/{task_id}/stream")
def task_reattach(task_id: str, from_seq: int = 0) -> StreamingResponse:
    """重连续流：先补发 seq > from_seq 的历史事件，再转实时。

    进程重启后内存缓冲已空，此时从审计日志补历史（#59）：任务已死，
    补完即结束，并**明确标注已中断**，不假装它还活着。
    """
    record = task_hub.get(task_id)
    if record is None:
        return _replay_dead_task(task_id, from_seq)
    return StreamingResponse(
        _task_frames(record, from_seq),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _replay_dead_task(task_id: str, from_seq: int) -> StreamingResponse:
    """从审计日志补出已不在内存里的任务事件（重启后的轨迹接续）。"""
    entry = session_store.get_task(task_id)
    if entry is None or not entry["log_path"]:
        raise HTTPException(
            status_code=404,
            detail=f"未找到任务: {task_id}（进程可能已重启，且无审计日志可回放）",
        )
    log = read_task_log(path=entry["log_path"])
    events = log["events"] if log else []
    interrupted = not has_terminal(events)

    def frames() -> Iterator[str]:
        yield _sse(
            {
                "type": "task_meta",
                "task_id": task_id,
                "session_id": entry["session_id"],
                "task": entry["task"],
                "replayed": True,
            }
        )
        for index, event in enumerate(events, 1):
            if index <= from_seq:
                continue
            frame = _agent_event_frame(event)
            frame["seq"] = index
            frame["task_id"] = task_id
            yield _sse(frame)
        if interrupted:
            yield _sse(
                {
                    "type": "failed",
                    "reason": INTERRUPTED_NOTE,
                    "iterations": 0,
                    "task_id": task_id,
                }
            )

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


class AgentStopRequest(BaseModel):
    task_id: str | None = None
    session_id: str | None = None


@app.post("/agent/stop")
def agent_stop(req: AgentStopRequest | None = None) -> dict:
    """叫停任务：按 task_id 定位（缺省停该会话的在跑任务），以 partial 收尾。"""
    if req is not None and req.task_id:
        stopped = task_hub.stop(req.task_id)
        return {
            "status": "ok" if stopped else "idle",
            "stopped": stopped,
            "task_id": req.task_id,
        }
    if req is not None and req.session_id:
        session_id = _resolve_session(req.session_id)
        running = task_hub.running_task_id(session_id)
        if running is None:
            return {"status": "idle", "stopped": False}
        return {
            "status": "ok",
            "stopped": task_hub.stop(running),
            "task_id": running,
        }
    running = task_hub.running_tasks()
    if not running:
        return {"status": "idle", "stopped": False}
    for record in running:
        record.cancel.cancel()
    return {
        "status": "ok",
        "stopped": True,
        "task_id": running[0].task_id,
        "stopped_tasks": [record.task_id for record in running],
    }


# ---- MCP 管理面 ----


@app.get("/mcp")
def list_mcp() -> dict:
    return {
        "servers": mcp_manager.listing(),
        "load_errors": list(mcp_manager.load_errors),
    }


class McpActionRequest(BaseModel):
    name: str = Field(min_length=1)


def _mcp_set_enabled(name: str, enabled: bool) -> dict:
    with _admin_lock:
        try:
            item = mcp_manager.set_enabled(name, enabled)
        except McpError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "ok", "server": item}


@app.post("/mcp/enable")
def enable_mcp(req: McpActionRequest) -> dict:
    return _mcp_set_enabled(req.name, True)


@app.post("/mcp/disable")
def disable_mcp(req: McpActionRequest) -> dict:
    return _mcp_set_enabled(req.name, False)


@app.post("/mcp/remove")
def remove_mcp(req: McpActionRequest) -> dict:
    with _admin_lock:
        try:
            mcp_manager.remove(req.name)
        except McpError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": "ok"}


@app.post("/mcp/reload")
def reload_mcp() -> dict:
    with _admin_lock:
        errors = mcp_manager.reload()  # 先重载再取列表（dict 按序求值）
        return {
            "status": "ok",
            "servers": mcp_manager.listing(),
            "load_errors": errors,
        }


@app.post("/mcp/reauth")
def reauth_mcp(req: McpActionRequest) -> dict:
    """OAuth 重授权：清 token 并重连该服务（issue #44 管理面重授权入口）。"""
    with _admin_lock:
        try:
            item = mcp_manager.reauthorize(req.name)
        except McpError as exc:
            code = 404 if str(exc).startswith("未找到") else 400
            raise HTTPException(status_code=code, detail=str(exc)) from exc
        return {"status": "ok", "server": item}


class McpInstallRequest(BaseModel):
    source: str  # catalog | local
    catalog_id: str = ""
    source_platform: str = ""
    path: str = ""
    env_values: dict[str, str] = Field(default_factory=dict)


@app.post("/mcp/install")
def install_mcp(req: McpInstallRequest) -> dict:
    """安装来源：目录条目合成连接定义 / 本地 server.json、mcp.json 导入。

    密钥值写入 .env（连接定义只留 ${VAR} 占位）；mcpb 单文件包标「暂不支持」。
    """
    with _admin_lock:
        source = (req.source or "").strip().lower()
        if source == "catalog":
            if not req.catalog_id or not req.source_platform:
                raise HTTPException(
                    status_code=400, detail="catalog 安装需要 catalog_id 与 source_platform"
                )
            payload = _market_proxy(
                "GET",
                "/internal/detail",
                params={"id": req.catalog_id, "source": req.source_platform},
            )
            text = str(payload.get("manifest_text") or "")
            label = f"catalog:{req.source_platform}"
        elif source == "local":
            if not req.path:
                raise HTTPException(status_code=400, detail="local 导入需要 path")
            file_path = Path(req.path)
            try:
                text = file_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"文件读取失败: {exc}") from exc
            label = f"import:{file_path}"
        else:
            raise HTTPException(status_code=400, detail="source 必为 catalog 或 local")
        try:
            candidates, warnings = parse_install_text(text, source=label)
        except McpError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if req.env_values:
            write_dotenv(_DOTENV_PATH, req.env_values)
        results = []
        for name, server_def, error in candidates:
            if server_def is None:
                results.append({"name": name, "status": "invalid", "detail": error})
                continue
            status, detail = mcp_manager.install(
                server_def, environ_update=req.env_values
            )
            results.append({"name": name, "status": status, "detail": detail})
        return {"source": label, "results": results, "warnings": warnings}


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


@app.get("/skills/presets")
def skills_presets() -> dict:
    return {"presets": [dict(preset) for preset in PRESET_SOURCES]}


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
    with _admin_lock:
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
    with _admin_lock:
        try:
            skill_registry.remove(name)
        except SkillError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "ok"}


class InstallRequest(BaseModel):
    source: str  # catalog | git | local（在处理函数中校验，给出中文错误）
    url: str = ""
    path: str = ""
    subpath: str = ""
    catalog_id: str = ""
    source_platform: str = ""


# ---- 技能在线目录代理（/market/*）：目录通路零意外外网，一切经爬取服务 ----


class _CatalogResponse:
    """爬取服务响应（合同：status_code / json()，与测试替身同形）。"""

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _UrllibCatalogClient:
    """标准库 HTTP 客户端（GET/POST JSON），服务间窄接口专用。

    刻意不用第三方 HTTP 库：主服务对爬取服务只发两种请求，标准库足够，
    且免疫「依赖随传递依赖漂移」——曾因 openai 3.x 改携 httpx2、直接
    import httpx 在运行时炸（测试缝全绿但真实传输层未被执行），故锚定
    标准库并以回归测试守住。
    """

    def __init__(self, base_url: str, timeout: float = 15.0):
        self._base = base_url.rstrip("/")
        self._timeout = timeout

    def request(self, method, path, params=None, json=None, timeout=None, **_):
        url = self._base + path
        if params:
            kept = {k: v for k, v in params.items() if v not in (None, "")}
            if kept:
                url += "?" + urllib.parse.urlencode(kept)
        json_body = json
        data = None
        headers = {}
        if json_body is not None:
            data = json_dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request, timeout=timeout or self._timeout
            ) as response:
                return _CatalogResponse(
                    response.status, _load_json(response.read().decode("utf-8", "replace"))
                )
        except urllib.error.HTTPError as exc:  # 4xx/5xx 也是有效响应（载荷带 detail）
            return _CatalogResponse(exc.code, _load_json(exc.read().decode("utf-8", "replace")))
        # URLError / 超时向上抛，由 _market_proxy 统一转 502 中文降级


def _load_json(text: str):
    try:
        return json_loads(text)
    except ValueError:
        return {}


def _catalog_client():
    """爬取服务客户端（测试注入点：替换为 fake 即可断言零外网）。"""
    return _UrllibCatalogClient(settings.catalog_base_url.strip())


def _catalog_reachable(timeout: float = 2.0) -> bool:
    try:
        response = _UrllibCatalogClient(
            settings.catalog_base_url.strip(), timeout=timeout
        ).request("GET", "/health")
        return response.status_code == 200
    except Exception:
        return False


def _ensure_catalog_service() -> None:
    """托管本机爬取服务：探活失败且允许自启时拉起子进程。

    独立进程语义不变（隔离边界 / 独立部署升级路径照旧）——只是省掉手动
    开第二个终端；已在跑（含外部实例）直接复用，CATALOG_AUTOSTART=0 关闭。
    """
    if not settings.catalog_base_url.strip():
        return
    if not getattr(settings, "catalog_autostart", False):
        return
    if _catalog_reachable():
        return
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "services.catalog"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return  # 拉不起来就走降级：/market/* 给中文不可用提示
    atexit.register(proc.terminate)


def _market_proxy(
    method: str, path: str, *, timeout: float | None = None, **kwargs
) -> dict:
    if not settings.catalog_base_url.strip():
        raise HTTPException(
            status_code=503, detail="在线目录未启用（未配置 CATALOG_BASE_URL）"
        )
    try:
        response = _catalog_client().request(method, path, timeout=timeout, **kwargs)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"在线目录暂不可用: {exc}（爬取服务未运行或不可达——本机会在主服务启动时自动托管，外部部署请启动 services/catalog）",
        ) from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", "")
        except Exception:
            detail = ""
        detail = detail or f"在线目录返回错误（HTTP {response.status_code}）"
        status = response.status_code if response.status_code < 500 else 502
        raise HTTPException(status_code=status, detail=detail)
    return response.json()


@app.get("/market/sources")
def market_sources() -> dict:
    payload = _market_proxy("GET", "/internal/sources")
    with _refresh_lock:
        payload["refreshing"] = _refresh_running
    return payload


@app.get("/market/search")
def market_search(
    q: str = "", source: str = "", kind: str = "", page: int = 1, page_size: int = 20
) -> dict:
    return _market_proxy(
        "GET",
        "/internal/search",
        params={
            "q": q,
            "source": source,
            "kind": kind,
            "page": page,
            "page_size": page_size,
        },
    )


@app.get("/market/detail")
def market_detail(id: str, source: str) -> dict:
    return _market_proxy("GET", "/internal/detail", params={"id": id, "source": source})


# ---- 目录刷新：全量爬取（mcp-registry 单源实测数分钟），异步触发 ----

_REFRESH_TIMEOUT = 600.0
_REFRESH_QUEUE_SECONDS = 1800.0  # 上游已有爬取时的排队上限
_REFRESH_RETRY_SECONDS = 5.0  # 排队重试间隔（测试可注入缩短）
_refresh_lock = threading.Lock()
_refresh_running = False


def _set_refresh_running(value: bool) -> None:
    global _refresh_running
    with _refresh_lock:
        _refresh_running = value


def _run_market_refresh(payload: dict) -> None:
    """后台执行一次目录爬取；结果落爬取服务源状态（/market/sources 反映）。

    爬取服务同进程爬取互斥：上游已有爬取时本次触发排队重试（refreshing 标志
    在排队期间保持，UI 不会误报「刷新结束」），排队超上限才放弃并记日志。
    """
    try:
        deadline = time.time() + _REFRESH_QUEUE_SECONDS
        while True:
            result = _market_proxy(
                "POST", "/internal/refresh", json=payload, timeout=_REFRESH_TIMEOUT
            )
            busy = [
                item
                for item in (result.get("results") or [])
                if item.get("status") == "refreshing"
            ]
            if not busy:  # 已执行：结果逐源降级落源状态
                return
            if time.time() >= deadline:
                print("[market] 目录刷新排队超时：上游已有爬取进行中", file=sys.stderr)
                return
            time.sleep(_REFRESH_RETRY_SECONDS)
    except HTTPException as exc:
        print(f"[market] 目录刷新失败：{exc.detail}", file=sys.stderr)
    except Exception as exc:  # 后台线程兜底：不让异常掀翻进程
        print(f"[market] 目录刷新失败：{exc}", file=sys.stderr)
    finally:
        _set_refresh_running(False)


@app.post("/market/refresh")
def market_refresh(payload: dict | None = None) -> dict:
    """触发一次目录爬取（异步）：立即返回，重复触发去重。

    全量爬取分钟级，同步等待会打爆代理超时（实测 15s 即断）；
    结果逐源降级落源状态，经 GET /market/sources 查询。
    """
    global _refresh_running
    if not settings.catalog_base_url.strip():
        raise HTTPException(
            status_code=503, detail="在线目录未启用（未配置 CATALOG_BASE_URL）"
        )
    with _refresh_lock:
        if _refresh_running:
            return {"status": "running"}
        _refresh_running = True
    threading.Thread(
        target=_run_market_refresh,
        args=(payload or {},),
        daemon=True,
        name="market-refresh",
    ).start()
    return {"status": "started"}


def _install_from_catalog(req: InstallRequest) -> dict:
    """目录包安装：文件集经既有校验闸门落盘，附带文件丢弃并警告。

    只落 SKILL.md 纯提示词（parse_external_skill：未知 frontmatter 字段
    一律丢弃，字段值不进上下文）；脚本/资源文件不落盘，在报告中可见。
    """
    pack = _market_proxy(
        "GET",
        f"/internal/pack/{req.catalog_id}",
        params={"source": req.source_platform},
    )
    extra = [str(name) for name in (pack.get("extra_files") or ())]
    packs: list[dict] = []
    results: list[dict] = []
    for item in pack.get("files") or ():
        path = str(item.get("path", ""))
        contents = str(item.get("contents", ""))
        if not path.endswith("SKILL.md"):
            extra.append(path)
            continue
        try:
            skill = parse_external_skill(contents)
        except SkillError as exc:
            results.append(
                {"name": Path(path).parent.name or path, "status": "invalid", "detail": str(exc)}
            )
            continue
        packs.append(
            {
                "name": skill.name,
                "description": skill.description,
                "guide": skill.guide,
                "tools": skill.tools,
            }
        )
    label = f"catalog:{req.source_platform or pack.get('source', '')}"
    results = results + skill_registry.install_packs(packs, source=label)
    warnings = []
    if extra:
        warnings.append("附带文件已丢弃（纯提示词边界）: " + "、".join(sorted(set(extra))[:20]))
    return {"source": label, "results": results, "warnings": warnings}


@app.post("/skills/install")
def install_skill(req: InstallRequest) -> dict:
    """安装技能：目录包（经爬取服务）/ Git 适配器 / 本地目录导入。"""
    source = req.source.strip().lower()
    with _admin_lock:
        try:
            if source == "catalog":
                if not req.catalog_id.strip():
                    raise SkillError("缺少目录条目 id")
                return _install_from_catalog(req)
            if source == "git":
                return install_from_git(skill_registry, req.url, req.subpath.strip())
            if source == "local":
                if not req.path.strip():
                    raise SkillError("缺少本地目录路径")
                return install_from_dir(skill_registry, req.path.strip(), req.subpath.strip())
        except SkillError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(
        status_code=400,
        detail=f"不支持的安装来源: {req.source!r}，可选 catalog / git / local",
    )


if __name__ == "__main__":
    import sys

    import uvicorn

    # 启动即预热会话库：版本不匹配 / 被其他进程占用都在这里 fail loud
    # （打印可读中文原因后退出，而不是甩一段 traceback）
    try:
        get_session_store()
    except SessionStoreError as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    _ensure_catalog_service()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
