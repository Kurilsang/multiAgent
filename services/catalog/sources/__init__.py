"""源适配器接口：各技能平台向统一条目/目录包的归一合同。

适配器是隔离区里真正「碰外网」的代码；差异（HTML vs API、ZIP vs git）
全部在适配器内部消化，主服务只认统一 schema（见 schema.py）。
"""

from __future__ import annotations

from typing import Protocol

from ..schema import CatalogDetail, CatalogEntry, CatalogPack

__all__ = ["CatalogSource", "build_sources"]


class CatalogSource(Protocol):
    """一个目录平台源。ref/install_ref 的含义由各源自定（如 id 或 identifier）。"""

    name: str

    def crawl(self, max_pages: int = 3) -> list[CatalogEntry]:
        """枚举目录条目（刷新用）；网络失败抛 CatalogError。"""
        ...

    def detail(self, ref: str) -> CatalogDetail:
        """确认卡预览：条目 + manifest 全文 + 审计明细。"""
        ...

    def fetch_pack(self, ref: str) -> CatalogPack:
        """目录包获取：manifest 文件集（附带文件列 extra_files）。"""
        ...


def build_sources(settings, creds_factory=None) -> dict[str, CatalogSource]:
    """按配置装配启用的源（适配器差异在此归一为统一合同）。

    creds_factory：源名 → 凭证仓（dict 风格 get/update）；SQLite 持久仓由
    __main__ 注入，测试注入 dict 即可。
    """
    from .lobehub import LobeHubSource
    from .mcp_registry import McpRegistrySource
    from .skills_sh import SkillsShSource

    registry = {
        SkillsShSource.name: SkillsShSource,
        LobeHubSource.name: LobeHubSource,
        McpRegistrySource.name: McpRegistrySource,
    }
    sources: dict[str, CatalogSource] = {}
    for name in settings.sources:
        factory = registry.get(name)
        if factory is not None:
            creds = creds_factory(name) if creds_factory is not None else None
            sources[name] = factory(settings, creds_store=creds)
    return sources
