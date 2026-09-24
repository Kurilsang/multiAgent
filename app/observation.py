"""观察值视图与旁存续读（SPEC-0003）。

进上下文的永远只是视图（默认 4000 字符，全局/工具级可配）；凡被截断的
完整结果旁存进内存句柄池（LRU + 总字节上限，超限淘汰最旧，不落盘），
模型经 read_tool_result 元工具按需续读——完整性交给模型按需取，
安全性由「每次只进视图」保证。full_result 工具（技能正文等刻意加载的
上下文）不受视图约束；续读走普通工具轮，计入迭代预算。
"""

from __future__ import annotations

from collections import OrderedDict

DEFAULT_VIEW_CHARS = 4000
DEFAULT_POOL_MAX_BYTES = 4 * 1024 * 1024
READ_TOOL_NAME = "read_tool_result"


class ResultPool:
    """截断观察值的完整文本池：确定性句柄（r1、r2…）+ LRU 淘汰。"""

    def __init__(self, max_bytes: int = DEFAULT_POOL_MAX_BYTES):
        self._max_bytes = max_bytes
        self._items: "OrderedDict[str, str]" = OrderedDict()
        self._bytes = 0
        self._counter = 0

    def put(self, text: str) -> str:
        self._counter += 1
        handle = f"r{self._counter}"
        self._items[handle] = text
        self._bytes += len(text)
        while self._bytes > self._max_bytes and self._items:
            _oldest, evicted = self._items.popitem(last=False)
            self._bytes -= len(evicted)
        return handle

    def get(self, handle: str) -> str | None:
        text = self._items.get(handle)
        if text is not None:
            self._items.move_to_end(handle)  # LRU：读取即续期
        return text


def format_observation(
    text: str, *, limit: int | None, pool: ResultPool, default_limit: int
) -> str:
    """观察值 → 视图（+ 旁存续读指引）；limit=0 或不超限则原样直灌。"""
    view_limit = default_limit if limit is None else limit
    if view_limit <= 0 or len(text) <= view_limit:
        return text
    handle = pool.put(text)
    return (
        f"{text[:view_limit]}\n"
        f"[…全文共 {len(text)} 字符，"
        f'{READ_TOOL_NAME}(handle="{handle}", offset={view_limit}) 续读]'
    )


def read_tool_result_tool(pool: ResultPool, chunk_size: int = DEFAULT_VIEW_CHARS):
    """read_tool_result 元工具：按句柄 + 偏移分段续读被截断的观察值。"""
    from .tools import Tool

    chunk = chunk_size if chunk_size > 0 else DEFAULT_VIEW_CHARS

    def read_tool_result(handle: str = "", offset: int = 0) -> str:
        text = pool.get(str(handle or ""))
        if text is None:
            raise ValueError(
                f"观察值句柄已失效或不存在：{handle!r}"
                f"（可能已被内存池淘汰或服务重启清空）"
            )
        try:
            start = max(0, int(offset))
        except (TypeError, ValueError):
            start = 0
        end = min(start + chunk, len(text))
        piece = text[start:end]
        if end >= len(text):
            return piece
        return (
            f"{piece}\n"
            f"[…共 {len(text)} 字符，已读至 {end}，"
            f'{READ_TOOL_NAME}(handle="{handle}", offset={end}) 续读]'
        )

    return Tool(
        name=READ_TOOL_NAME,
        description="续读被截断的工具结果全文：传入观察值尾注给出的 handle 与 offset",
        parameters={
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "观察值尾注给出的句柄（如 r1）",
                },
                "offset": {
                    "type": "integer",
                    "description": "续读起点（缺省 0 = 从头读）",
                },
            },
            "required": ["handle"],
        },
        func=read_tool_result,
        full_result=True,  # 续读内容就是用户点名要的全文分段，不再二次截断
    )
