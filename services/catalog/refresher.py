"""目录刷新器：一次爬取（手动/接口触发）+ 后台定时刷新。

单源失败只记错不毁全局（降级读缓存 + stale 标注）；刷新在存储层锁内
落库，搜索读不被外网 IO 阻塞。定时器为守护线程，随进程退出。
同进程爬取互斥：进行中的刷新不被并发触发重复爬（跳过并报 refreshing）。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from .store import CatalogStore

_refresh_guard = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh_now(
    sources: dict, store: CatalogStore, names: list[str] | None = None, max_pages: int = 0
) -> list[dict]:
    """一次爬取：逐源拉目录入库，返回逐源结果报告（含截断等非致命告警）。

    max_pages 是页数预算（0 = 各适配器默认上限）；主动传更大预算即「抓取更多」。
    已有刷新进行中时直接跳过（不并发重复爬外网）。
    """
    if not _refresh_guard.acquire(blocking=False):
        return [{"source": "*", "status": "refreshing", "detail": "已有刷新进行中"}]
    try:
        return _crawl_all(sources, store, names, max_pages)
    finally:
        _refresh_guard.release()


def _crawl_all(
    sources: dict, store: CatalogStore, names: list[str] | None, max_pages: int
) -> list[dict]:
    results: list[dict] = []
    for name in names if names is not None else sorted(sources):
        source = sources.get(name)
        if source is None:
            results.append(
                {"source": name, "status": "unknown_source", "detail": f"未配置的源: {name}"}
            )
            continue
        try:
            entries = source.crawl(max_pages=max_pages) if max_pages else source.crawl()
        except Exception as exc:  # 爬取失败属于常态：记录并继续其他源
            store.record_error(name, str(exc))
            results.append({"source": name, "status": "error", "detail": str(exc)})
            continue
        store.replace_source(name, list(entries), _now())
        warnings = list(
            dict.fromkeys(str(item) for item in getattr(source, "crawl_warnings", ()) if item)
        )
        if warnings:  # 如截断：缓存可用但不完整，落源状态（replace 已清旧告警）
            store.record_warning(name, "；".join(warnings))
        results.append(
            {"source": name, "status": "ok", "count": len(entries), "warnings": warnings}
        )
    return results


def refresh_loop(
    sources: dict,
    store: CatalogStore,
    interval_seconds: float,
    stop_event: threading.Event,
    on_cycle=None,
    names: list[str] | None = None,
) -> None:
    """后台刷新循环：先立即刷一轮，随后按间隔循环，直到 stop_event 置位。"""
    while not stop_event.is_set():
        results = refresh_now(sources, store, names)
        if on_cycle is not None:
            on_cycle(results)
        stop_event.wait(interval_seconds)


def start_scheduler(
    sources: dict, store: CatalogStore, interval_seconds: float, names: list[str] | None = None
) -> tuple[threading.Thread, threading.Event]:
    """启动后台刷新线程（守护线程）；返回 (线程, stop_event)，stop_event.set() 即停。"""
    stop_event = threading.Event()
    thread = threading.Thread(
        target=refresh_loop,
        kwargs={
            "sources": sources,
            "store": store,
            "interval_seconds": interval_seconds,
            "stop_event": stop_event,
            "names": names,
        },
        daemon=True,
        name="catalog-refresh",
    )
    thread.start()
    return thread, stop_event
