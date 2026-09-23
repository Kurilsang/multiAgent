"""独立进程入口：python -m services.catalog"""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import CatalogSettings
from .sources import build_sources
from .store import MemoryStore


def main() -> None:
    settings = CatalogSettings.from_env()
    app = create_app(build_sources(settings), MemoryStore())
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
