"""对话记录导出：把主对话历史格式化为便于存档与排障的文本。

Markdown 供人阅读、可直接整段粘贴给 AI 排障；JSON 供程序化处理。
两个格式化函数均为纯函数（时间可注入），不做 IO。
"""

import json
from datetime import datetime

_ROLE_LABELS = {"user": "用户", "assistant": "助手"}


def to_markdown(messages: list[dict], *, default_provider: str, now: datetime | None = None) -> str:
    """把历史消息渲染成带序号与角色标注的 Markdown。"""
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# multiagent 对话导出",
        "",
        f"- 导出时间：{timestamp}",
        f"- 默认厂商：{default_provider}",
        f"- 消息数：{len(messages)}",
        "",
        "---",
        "",
    ]
    for index, message in enumerate(messages, 1):
        role = _ROLE_LABELS.get(message["role"], message["role"])
        lines.append(f"## {index}. [{role}]")
        lines.append("")
        lines.append(message["content"])
        lines.append("")
    return "\n".join(lines)


def to_json(
    messages: list[dict],
    *,
    default_provider: str,
    now: datetime | None = None,
    traces: list[dict] | None = None,
) -> str:
    """把历史消息打包成带元信息的 JSON 串；traces 非空时附任务轨迹原始事件。"""
    payload = {
        "exported_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "default_provider": default_provider,
        "messages": messages,
    }
    if traces:
        payload["traces"] = traces
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---- 任务轨迹导出：审计日志事件 → 协作树 markdown ----

_ROLE_LABEL = {"orchestrator": "编排器", "worker": "子 agent", "critic": "审查 agent"}


def traces_to_markdown(task_logs: list[dict], *, now: datetime | None = None) -> str:
    """把审计日志渲染成协作树 markdown（子 agent 分区缩进一层）。"""
    timestamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    lines = ["# multiagent 任务轨迹导出", "", f"- 导出时间：{timestamp}", ""]
    for entry in task_logs:
        lines.append(f"## 任务：{entry['task']}")
        lines.append("")
        lines.extend(_render_task_events(entry.get("events", [])))
        lines.append("")
    return "\n".join(lines)


def _render_task_events(events: list[dict]) -> list[str]:
    """逐事件渲染；同一 agent 的连续思考增量合并为一行。"""
    out: list[str] = []
    thought: list[str] = []
    thought_event: dict = {}

    def flush() -> None:
        if thought:
            out.append(_line("思考：" + "".join(thought), thought_event))
            thought.clear()

    for event in events:
        kind = event.get("type")
        if kind == "ThoughtDelta":
            if not thought:
                thought_event = event
            thought.append(event.get("text", ""))
            continue
        flush()
        out.append(_line(_describe_event(event), event))
    flush()
    return out


def _describe_event(event: dict) -> str:
    kind = event.get("type")
    if kind == "TaskStarted":
        return f"开始：{event.get('task', '')}"
    if kind == "ActionStarted":
        arguments = json.dumps(event.get("arguments", {}), ensure_ascii=False)
        return f"行动：{event.get('tool_name', '')}({arguments})"
    if kind == "ActionObserved":
        suffix = "（失败）" if event.get("is_error") else ""
        return f"观察{suffix}：{event.get('result', '')}"
    if kind == "TaskFinished":
        return f"结果（{event.get('status', '')}）：{event.get('answer', '')}"
    if kind == "TaskFailed":
        return f"失败：{event.get('reason', '')}"
    return str(event)


def _line(text: str, event: dict) -> str:
    """编排器事件顶格，子 agent / 审查事件缩进一层（深度限一层的协作树）。"""
    role = event.get("role", "orchestrator")
    if role == "orchestrator":
        return f"- {text}"
    name = event.get("agent_name") or _ROLE_LABEL.get(role, role)
    return f"  - [{name}] {text}"
