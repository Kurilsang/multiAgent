"""对话上下文管理：维护单会话消息历史并按上限截断。"""


class Conversation:
    """单会话内存上下文。

    消息截断策略：system 提示词始终保留，历史消息只保留最近
    max_messages 条，防止超出模型上下文窗口。
    """

    def __init__(self, system_prompt: str | None = None, max_messages: int = 20):
        if max_messages < 2:
            raise ValueError("max_messages 至少为 2（保留一轮完整对话）")
        self._system = system_prompt
        self._max_messages = max_messages
        self._history: list[dict] = []

    def add(self, role: str, content: str) -> None:
        if role not in ("user", "assistant"):
            raise ValueError(f"不支持的消息角色: {role!r}")
        self._history.append({"role": role, "content": content})

    def pop_last(self) -> dict | None:
        """回滚最近一条消息（如请求失败时撤回未得到回复的 user 消息）。"""
        return self._history.pop() if self._history else None

    def messages_for_api(self) -> list[dict]:
        """组装实际发给模型的消息列表：system + 截断后的历史。

        返回消息的浅拷贝，避免调用方修改污染内部状态。
        """
        messages = []
        if self._system:
            messages.append({"role": "system", "content": self._system})
        messages.extend(dict(m) for m in self._history[-self._max_messages :])
        return messages

    def reset(self) -> None:
        self._history.clear()

    def __len__(self) -> int:
        return len(self._history)
