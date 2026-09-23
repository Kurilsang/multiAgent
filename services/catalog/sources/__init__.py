"""源适配器接口：各技能平台向统一条目/目录包的归一合同。

适配器是隔离区里真正「碰外网」的代码；差异（HTML vs API、ZIP vs git）
全部在适配器内部消化，主服务只认统一 schema（见 schema.py）。
"""

from __future__ import annotations

from typing import Protocol

from ..schema import CatalogEntry, SkillDetail, SkillPack

__all__ = ["CatalogSource", "build_sources"]


class CatalogSource(Protocol):
    """一个技能平台源。ref/install_ref 的含义由各源自定（如 id 或 identifier）。"""

    name: str

    def crawl(self, max_pages: int = 3) -> list[CatalogEntry]:
        """枚举目录条目（刷新用）；网络失败抛 CatalogError。"""
        ...

    def detail(self, ref: str) -> SkillDetail:
        """确认卡预览：条目 + SKILL.md 全文 + 审计明细。"""
        ...

    def fetch_pack(self, ref: str) -> SkillPack:
        """目录包获取：SKILL.md 文件集（附带文件列 extra_files）。"""
        ...


def build_sources(settings) -> dict[str, CatalogSource]:
    """按配置装配启用的源。

    适配器注册表：18/20 号工单接入真实源（skills-sh / lobehub）后在此注册；
    当前返回空集（目录浏览需至少一个源，见 /internal/sources 的空态）。
    """
    return {}
