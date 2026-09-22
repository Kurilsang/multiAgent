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


def to_json(messages: list[dict], *, default_provider: str, now: datetime | None = None) -> str:
    """把历史消息打包成带元信息的 JSON 串。"""
    payload = {
        "exported_at": (now or datetime.now()).isoformat(timespec="seconds"),
        "default_provider": default_provider,
        "messages": messages,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
