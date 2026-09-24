"""官方 MCP Registry 适配器（隔离区内、匿名只读 REST API）。

契约（registry.modelcontextprotocol.io，preview 阶段）：
GET /v0.1/servers?limit=&version=latest&cursor=   游标分页列表
GET /v0.1/servers/{name}/versions/latest          单版本详情（路径参数 URL 编码）
条目形状 {"server": ServerJSON, "_meta": {"io.modelcontextprotocol.registry/official": {...}}}。
状态语义：deleted 不上架；deprecated 灰显（statusMessage 作原因）；发布者身份经
namespace 验证 → validated。缓存模型是整批替换，按全量游标拉取（水位即
source_state.last_refresh）；ServerJSON 原样作 manifest（server.json）交付确认卡/安装。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..schema import CatalogDetail, CatalogEntry, CatalogError, CatalogPack

BASE_URL = "https://registry.modelcontextprotocol.io"
PAGE_TIMEOUT = 20
PAGE_SIZE = 100
USER_AGENT = "multiagent-catalog/1.0 (+https://github.com/Kurilsang/multiAgent)"
_OFFICIAL_META = "io.modelcontextprotocol.registry/official"


class McpRegistrySource:
    """官方 MCP Registry 源。ref/install_ref = server 全名（reverse-DNS）。"""

    name = "mcp-registry"

    def __init__(self, settings=None, creds_store=None, client=None):
        self._client = client

    def crawl(self, max_pages: int = 3) -> list[CatalogEntry]:
        entries: list[CatalogEntry] = []
        cursor = ""
        for _ in range(max(1, max_pages)):
            url = f"{BASE_URL}/v0.1/servers?limit={PAGE_SIZE}&version=latest"
            if cursor:
                url += f"&cursor={quote(cursor, safe='')}"
            data = self._json(self._get(url), "目录列表")
            for raw in data.get("servers") or []:
                entry = self._to_entry(raw)
                if entry is not None:
                    entries.append(entry)
            cursor = str((data.get("metadata") or {}).get("nextCursor") or "")
            if not cursor:
                break
        if not entries:
            raise CatalogError(
                "MCP Registry 解析为空（API 结构可能已变更），请检查适配器", 502
            )
        return entries

    def detail(self, ref: str) -> CatalogDetail:
        """确认卡数据源：ServerJSON 原样作 manifest（含包/远程与密钥声明）。"""
        data = self._json(self._get(self._detail_url(ref)), "详情")
        entry = self._to_entry(data)
        if entry is None:
            raise CatalogError(f"条目已下架（deleted）：{ref}", 404)
        server = data.get("server") or {}
        return CatalogDetail(
            entry=entry,
            manifest_text=json.dumps(server, ensure_ascii=False, indent=2),
            manifest_path="server.json",
        )

    def fetch_pack(self, ref: str) -> CatalogPack:
        data = self._json(self._get(self._detail_url(ref)), "详情")
        if self._to_entry(data) is None:
            raise CatalogError(f"条目已下架（deleted）：{ref}", 404)
        text = json.dumps(data.get("server") or {}, ensure_ascii=False, indent=2)
        return CatalogPack(
            files=(("server.json", text),),
            extra_files=(),
            origin=ref,
            source=self.name,
            manifest_path="server.json",
            kind="mcp",
        )

    # ---- 传输 ----

    def _detail_url(self, ref: str) -> str:
        return f"{BASE_URL}/v0.1/servers/{quote(ref, safe='')}/versions/latest"

    def _get(self, url: str):
        client = self._client if self._client is not None else _default_client()
        try:
            response = client.get(
                url, headers={"User-Agent": USER_AGENT}, timeout=PAGE_TIMEOUT
            )
        except Exception as exc:
            raise CatalogError(f"MCP Registry 请求失败: {exc}", 502) from exc
        if response.status_code != 200:
            raise CatalogError(
                f"MCP Registry 返回错误（HTTP {response.status_code}）", 502
            )
        return response

    @staticmethod
    def _json(response, what: str) -> dict:
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise CatalogError(f"{what}不是有效 JSON", 502) from exc
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _to_entry(raw) -> CatalogEntry | None:
        if not isinstance(raw, dict):
            return None
        server = raw.get("server") or {}
        meta = (raw.get("_meta") or {}).get(_OFFICIAL_META) or {}
        status = str(meta.get("status") or "active").lower()
        if status == "deleted":  # 违规下架：不进索引
            return None
        packages = server.get("packages") or []
        tags = [
            str(item.get("registryType"))
            for item in packages
            if isinstance(item, dict) and item.get("registryType")
        ]
        name = str(server.get("name") or "")
        return CatalogEntry.from_raw(
            {
                "id": name,
                "name": server.get("title") or name,
                "description": server.get("description"),
                "origin": (server.get("repository") or {}).get("url")
                or name.split("/")[0],
                "detail_url": server.get("websiteUrl") or "",
                "install_ref": name,
                "tags": tags[:3],
                "validated": True,  # namespace 经 Registry 发布者验证（≠安全审计）
                "kind": "mcp",
                "status": "deprecated" if status == "deprecated" else "active",
                "status_message": meta.get("statusMessage") or "",
            },
            source=McpRegistrySource.name,
        )


def _default_client():
    from ..stdlib_http import UrllibHttpClient

    return UrllibHttpClient()
