"""对话上下文管理：维护单会话消息历史，入模走预算分层装配（SPEC-0006）。"""

from .context_budget import assemble_messages


class Conversation:
    """单会话内存上下文。

    入模策略（SPEC-0006）：按预算分层装配——system → 记忆块 →
    最近 max_messages 条保底 → 更早对话（预算有剩余才逐字入模）。
    max_messages 是**保底下限**而非截断上限。装填逻辑统一在
    app/context_budget.py（assemble_messages/assemble_with_summary），
    本类的 assemble 是 CLI 侧的薄封装。
    """

    def __init__(self, system_prompt: str | None = None, max_messages: int = 20):
        if max_messages < 2:
            raise ValueError("max_messages 至少为 2（保留一轮完整对话）")
        self._system = system_prompt
        self._max_messages = max_messages
        self._history: list[dict] = []

    @classmethod
    def from_history(
        cls,
        history: list[dict],
        system_prompt: str | None = None,
        max_messages: int = 20,
    ) -> "Conversation":
        """由持久化历史重建上下文（docs/specs/0005）。

        只取 role/content：provider/model/思考链/工具步骤是展示面信息，
        不进模型上下文。入模装配与内存态完全一致（走 assemble）。
        """
        conversation = cls(system_prompt=system_prompt, max_messages=max_messages)
        for message in history:
            conversation.add(message["role"], message["content"])
        return conversation

    def add(self, role: str, content: str) -> None:
        if role not in ("user", "assistant"):
            raise ValueError(f"不支持的消息角色: {role!r}")
        self._history.append({"role": role, "content": content})

    def pop_last(self) -> dict | None:
        """回滚最近一条消息（如请求失败时撤回未得到回复的 user 消息）。"""
        return self._history.pop() if self._history else None

    def assemble(
        self, budget_tokens: int, memory_blocks: list[str] | None = None
    ) -> tuple[list[dict], dict]:
        """按预算装配入模消息（唯一入模实现点）。

        返回 (消息数组, 装配元数据)。返回消息为拷贝，
        避免调用方修改污染内部状态。
        """
        return assemble_messages(
            self._system,
            self._history,
            budget_tokens=budget_tokens,
            recent_floor=self._max_messages,
            memory_blocks=memory_blocks,
        )

    def history(self) -> list[dict]:
        """完整历史快照（消息为拷贝），供导出等只读用途；不受截断影响。"""
        return [dict(m) for m in self._history]

    def reset(self) -> None:
        self._history.clear()

    def __len__(self) -> int:
        return len(self._history)
