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
        entries = self.make_source().crawl()  # 默认全量：翻到 cursor 耗尽
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
        # 拼装后的命令模板（确认卡要展示的命令原文）
        self.assertEqual(payload["install_preview"]["transport"], "stdio")
        self.assertEqual(
            payload["install_preview"]["command"], ["npx", "-y", "@acme/mcp-fs@1.2.3"]
        )
        # 结构化密钥声明：${VAR} 占位名 + isRequired/isSecret/default/choices
        self.assertEqual(
            payload["install_preview"]["env"],
            [
                {
                    "name": "ACME_TOKEN",
                    "var": "ACME_TOKEN",
                    "isRequired": True,
                    "isSecret": True,
                    "default": "",
                    "choices": [],
                }
            ],
        )
        # 详情路径参数 URL 编码（reverse-DNS 含 /）
        self.assertTrue(
            any("io.github.acme%2Ffilesystem" in url for _m, url, _k in self.client.calls)
        )

    def test_detail_remote_preview_endpoint_and_header_declarations(self):
        """远程条目：endpoint + 请求头声明（占位名清洗大写，对位 app/mcp.py）。"""
        source = self.make_source(
            {
                "/versions/latest": FakeResponse(
                    load_catalog_fixture("mcp-registry-detail-remote.json")
                )
            }
        )
        detail = source.detail("io.github.acme/remote-db")
        preview = json.loads(detail.manifest_text)["install_preview"]
        self.assertEqual(preview["transport"], "streamable-http")
        self.assertEqual(preview["url"], "https://mcp.acme.dev/db/mcp")
        self.assertEqual(
            preview["headers"],
            [
                {
                    "name": "Authorization",
                    "var": "AUTHORIZATION",
                    "isRequired": True,
                    "isSecret": True,
                    "default": "Bearer changeme",
                    "choices": [],
                },
                {
                    "name": "X-API-Key",
                    "var": "X_API_KEY",
                    "isRequired": False,
                    "isSecret": True,
                    "default": "",
                    "choices": ["key-a", "key-b"],
                },
            ],
        )

    def test_preview_vars_match_connection_definition_placeholders(self):
        """跨边界同步锚：声明的 var 必须等于连接定义合成的 ${VAR} 占位名（同形各自维护，防漂移）。"""
        from app.mcp import parse_install_text

        source = self.make_source()
        manifest = json.loads(source.detail("io.github.acme/filesystem").manifest_text)
        candidates, _warnings = parse_install_text(
            json.dumps(manifest), source="catalog:mcp-registry"
        )
        name, server, error = candidates[0]
        self.assertEqual(error, "")
        for decl in manifest["install_preview"]["env"]:
            self.assertEqual(server.env[decl["var"]], "${" + decl["var"] + "}")

        remote = self.make_source(
            {
                "/versions/latest": FakeResponse(
                    load_catalog_fixture("mcp-registry-detail-remote.json")
                )
            }
        )
        manifest = json.loads(remote.detail("io.github.acme/remote-db").manifest_text)
        candidates, _warnings = parse_install_text(
            json.dumps(manifest), source="catalog:mcp-registry"
        )
        name, server, error = candidates[0]
        self.assertEqual(error, "")
        for decl in manifest["install_preview"]["headers"]:
            self.assertEqual(server.headers[decl["name"]], "${" + decl["var"] + "}")

    def test_detail_mcpb_only_marks_unsupported(self):
        """mcpb 单文件包：展示但标「暂不支持安装」（transport null + 原因）。"""
        payload = {
            "server": {
                "name": "com.acme/mcpb-only",
                "packages": [
                    {"registryType": "mcpb", "identifier": "x.mcpb", "version": "1.0"}
                ],
                "remotes": [],
            },
            "_meta": {
                "io.modelcontextprotocol.registry/official": {"status": "active"}
            },
        }
        source = self.make_source(
            {"/versions/latest": FakeResponse(json.dumps(payload))}
        )
        preview = json.loads(source.detail("com.acme/mcpb-only").manifest_text)[
            "install_preview"
        ]
        self.assertIsNone(preview["transport"])
        self.assertIn("mcpb", preview["reason"])

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
