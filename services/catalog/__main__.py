"""独立进程入口：python -m services.catalog

装配：SQLite 目录缓存 + 启用源 + 后台定时刷新（CATALOG_REFRESH_HOURS）；
LobeHub 凭证存 catalog.db 的 creds 表（日志严禁输出凭证）。
"""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import CatalogSettings
from .refresher import start_scheduler
from .sources import build_sources
from .store import SqliteStore


def main() -> None:
    settings = CatalogSettings.from_env()
    store = SqliteStore(settings.db_path)
    sources = build_sources(settings, creds_factory=store.creds)
    _thread, _stop = start_scheduler(
        sources, store, interval_seconds=settings.refresh_hours * 3600
    )
    uvicorn.run(create_app(sources, store), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
