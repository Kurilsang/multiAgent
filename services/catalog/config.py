"""爬取服务配置：全部来自环境变量，独立于主服务 .env。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

CATALOG_ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class CatalogSettings:
    """爬取服务运行配置（环境变量注入，便于独立部署）。"""

    host: str = "127.0.0.1"  # 默认仅本机回环：/internal/* 不对公网暴露
    port: int = 8100
    db_path: Path = CATALOG_ROOT / "catalog.db"
    refresh_hours: float = 6.0
    sources: tuple[str, ...] = ("skills-sh", "lobehub")

    @classmethod
    def from_env(cls) -> "CatalogSettings":
        sources = tuple(
            name.strip()
            for name in os.environ.get("CATALOG_SOURCES", "skills-sh,lobehub").split(",")
            if name.strip()
        )
        return cls(
            host=os.environ.get("CATALOG_HOST", "127.0.0.1"),
            port=int(os.environ.get("CATALOG_PORT", "8100")),
            db_path=Path(
                os.environ.get("CATALOG_DB", str(CATALOG_ROOT / "catalog.db"))
            ),
            refresh_hours=float(os.environ.get("CATALOG_REFRESH_HOURS", "6")),
            sources=sources or ("skills-sh", "lobehub"),
        )
