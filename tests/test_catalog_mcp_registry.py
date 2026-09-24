"""官方 MCP Registry 适配器测试：fixture 驱动，零真实网络。"""

import json
import unittest

from services.catalog.sources import build_sources
from services.catalog.sources.mcp_registry import McpRegistrySource

from tests.fakes import FakeHttpClient, FakeResponse, load_catalog_fixture


class FakeCatalogSettings:
    sources = ("mcp-registry",)


class McpRegistrySourceTest(unittest.TestCase):
    def make_source(self, routes=None):
        self.client = FakeHttpClient(
            routes
            or {
                "/versions/latest": FakeResponse(
                    load_catalog_fixture("mcp-registry-detail.json")
                ),
                "cursor=page2cursor": FakeResponse(
                    load_catalog_fixture("mcp-registry-page2.json")
                ),
                "/v0.1/servers": FakeResponse(
                    load_catalog_fixture("mcp-registry-page1.json")
                ),
            }
        )
        return McpRegistrySource(client=self.client)

    def test_crawl_follows_cursor_and_maps_status(self):
        entries = self.make_source().crawl(max_pages=5)
        ids = [entry.id for entry in entries]
        self.assertEqual(
            ids,
            [
                "io.github.acme/filesystem",
                "com.legacy/old",
                "io.github.acme/remote-db",
            ],
        )
        self.assertNotIn("io.github.gone/deleted", ids)  # deleted 不上架
        legacy = entries[1]
        self.assertEqual(legacy.status, "deprecated")  # deprecated 灰显
        self.assertEqual(legacy.status_message, "改用 com.new/shiny")
        self.assertEqual(entries[0].kind, "mcp")
        self.assertTrue(entries[0].validated)  # Registry 发布者验证
        self.assertTrue(
            any("cursor=page2cursor" in url for _m, url, _k in self.client.calls)
        )

    def test_crawl_respects_max_pages(self):
        entries = self.make_source().crawl(max_pages=1)
        self.assertEqual(len(entries), 2)  # 只拉首页（deleted 已剔除）

    def test_detail_returns_manifest_with_declarations(self):
        detail = self.make_source().detail("io.github.acme/filesystem")
        self.assertEqual(detail.manifest_path, "server.json")
        payload = json.loads(detail.manifest_text)
        self.assertEqual(payload["name"], "io.github.acme/filesystem")
        # 确认卡数据源：包/远程 + 环境变量与请求头声明原样保留
        self.assertIn("packages", payload)
        self.assertEqual(
            payload["packages"][0]["environmentVariables"][0]["isSecret"], True
        )
        # 详情路径参数 URL 编码（reverse-DNS 含 /）
        self.assertTrue(
            any("io.github.acme%2Ffilesystem" in url for _m, url, _k in self.client.calls)
        )

    def test_fetch_pack_manifest_pack(self):
        pack = self.make_source().fetch_pack("io.github.acme/filesystem")
        self.assertEqual(pack.kind, "mcp")
        self.assertEqual(pack.manifest_path, "server.json")
        self.assertEqual([path for path, _t in pack.files], ["server.json"])

    def test_registered_in_build_sources(self):
        sources = build_sources(FakeCatalogSettings())
        self.assertIn("mcp-registry", sources)


if __name__ == "__main__":
    unittest.main()
