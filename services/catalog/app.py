"""爬取服务 HTTP 面：/health + /internal/* 窄接口（仅对内网暴露）。

⚠ 隔离边界：本应用是唯一与外网交互的组件（经 sources 适配器），
主服务只消费这里的统一条目/目录包；接口集刻意保持窄小，升级独立
部署时本包原样搬出，主服务只改 CATALOG_BASE_URL。
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .refresher import refresh_now
from .schema import CatalogError
from .store import CatalogStore

MAX_PAGE_SIZE = 100


def create_app(sources: dict, store: CatalogStore) -> FastAPI:
    """组装爬取服务应用（sources/store 注入，便于测试替换）。"""
    app = FastAPI(
        title="multiagent-catalog",
        description="技能在线目录爬取服务（隔离区：唯一外网面，/internal/* 仅对内网）",
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "app": "multiagent-catalog"}

    @app.get("/internal/sources")
    def list_sources() -> dict:
        known = {item["source"]: item for item in store.sources()}
        for name in sources:
            known.setdefault(
                name,
                {
                    "source": name,
                    "last_refresh": "",
                    "last_error": "",
                    "entry_count": 0,
                    "stale": True,
                },
            )
        return {"sources": sorted(known.values(), key=lambda item: item["source"])}

    @app.get("/internal/search")
    def search(
        q: str = "", source: str = "", kind: str = "", page: int = 1, page_size: int = 20
    ) -> dict:
        page = max(1, page)
        page_size = min(max(1, page_size), MAX_PAGE_SIZE)
        items, total = store.search(
            q=q, source=source, kind=kind, page=page, page_size=page_size
        )
        return {
            "items": [entry.to_dict() for entry in items],
            "total": total,
            "page": page,
            "page_size": page_size,
            "refreshed_at": store.refreshed_at(),
        }

    @app.get("/internal/detail")
    def detail(id: str, source: str) -> dict:
        source_impl = _require_source(sources, source)
        try:
            return source_impl.detail(id).to_dict()
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.get("/internal/pack/{ref:path}")
    def pack(ref: str, source: str) -> dict:
        source_impl = _require_source(sources, source)
        try:
            return source_impl.fetch_pack(ref).to_dict()
        except CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from exc

    @app.post("/internal/refresh")
    def refresh(payload: dict | None = None) -> dict:
        requested = (payload or {}).get("source") or ""
        names = [requested] if requested else None
        return {"results": refresh_now(sources, store, names)}

    return app


def _require_source(sources: dict, name: str):
    source = sources.get(name)
    if source is None:
        raise HTTPException(status_code=404, detail=f"未配置的源: {name}")
    return source
