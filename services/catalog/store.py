"""目录存储：条目缓存 + 源状态。

C17 提供内存实现（MemoryStore，测试与骨架用）；SQLite 实现见 19 号工单。
接口约定：搜索**只读缓存**（外网查询只发生在爬取/详情/取包时），
源状态含 last_refresh / last_error / stale（降级读缓存的判定依据）。
"""

from __future__ import annotations

from typing import Protocol

from .schema import CatalogEntry


class CatalogStore(Protocol):
    def replace_source(
        self, source: str, entries: list[CatalogEntry], refreshed_at: str
    ) -> None: ...

    def record_error(self, source: str, error: str) -> None: ...

    def search(
        self, q: str = "", source: str = "", page: int = 1, page_size: int = 20
    ) -> tuple[list[CatalogEntry], int]: ...

    def sources(self) -> list[dict]: ...

    def refreshed_at(self) -> str: ...


_DEFAULT_STATE = {"last_refresh": "", "last_error": "", "entry_count": 0}


class MemoryStore:
    """内存存储：按源分组保序（爬取顺序 = 榜单热度顺序）。"""

    def __init__(self):
        self._entries: dict[str, list[CatalogEntry]] = {}
        self._state: dict[str, dict] = {}
        self._refreshed_at = ""

    def replace_source(
        self, source: str, entries: list[CatalogEntry], refreshed_at: str
    ) -> None:
        self._entries[source] = list(entries)
        self._state[source] = {
            "last_refresh": refreshed_at,
            "last_error": "",
            "entry_count": len(entries),
        }
        self._refreshed_at = refreshed_at

    def record_error(self, source: str, error: str) -> None:
        state = self._state.setdefault(source, dict(_DEFAULT_STATE))
        state["last_error"] = error
        state["entry_count"] = len(self._entries.get(source, ()))

    def search(
        self, q: str = "", source: str = "", page: int = 1, page_size: int = 20
    ) -> tuple[list[CatalogEntry], int]:
        needle = q.strip().casefold()
        hits: list[CatalogEntry] = []
        for name, entries in self._entries.items():
            if source and name != source:
                continue
            for entry in entries:
                if needle and needle not in (
                    entry.name + "\n" + entry.description
                ).casefold():
                    continue
                hits.append(entry)
        start = (page - 1) * page_size
        return hits[start : start + page_size], len(hits)

    def sources(self) -> list[dict]:
        result = []
        for name in sorted(self._state):
            state = self._state[name]
            result.append(
                {
                    "source": name,
                    "last_refresh": state["last_refresh"],
                    "last_error": state["last_error"],
                    "entry_count": state["entry_count"],
                    "stale": bool(state["last_error"]) or not state["last_refresh"],
                }
            )
        return result

    def refreshed_at(self) -> str:
        return self._refreshed_at
