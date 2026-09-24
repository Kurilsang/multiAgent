"""MCP 管理 HTTP API 测试：fake client factory 注入 + 临时目录，零真实进程/网络。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app.server as server

from tests.fakes import (
    FakeLLMClient,
    FakeMcpSession,
    load_catalog_fixture,
    make_mcp_tool,
    text_round,
    tool_round,
)
from tests.test_market_api import FakeCatalogHttp, FakeCatalogResponse
from tests.test_server_stream import StubSettings


def good_entry(**overrides):
    entry = {
        "name": "io.github.acme/filesystem",
        "transport": "stdio",
        "command": [sys.executable, "-m", "fixture_mcp_server"],
        "env": {"ACME_TOKEN": "${MCP_ACME_TOKEN}"},
    }
    entry.update(overrides)
    return entry


class McpApiHarness(unittest.TestCase):
    def setUp(self):
        self._orig = (
            server.settings,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
            server.mcp_manager,
        )
        server.settings = StubSettings()
        server.conversation.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config = self.root / "mcp" / "servers.json"
        self.config.parent.mkdir()
        self.session = FakeMcpSession(tools=[make_mcp_tool()])
        self.requested = []

        def factory(server_def):
            self.requested.append(server_def)
            return self.session

        self.factory = factory
        self.client = TestClient(server.app)

    def tearDown(self):
        server.conversation.reset()
        (
            server.settings,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
            server.mcp_manager,
        ) = self._orig
        self._tmp.cleanup()

    def write_config(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        self.config.write_text(text, encoding="utf-8")

    def wire(self):
        (
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
            server.mcp_manager,
        ) = server._build_wiring(
            self.root, mcp_config=self.config, mcp_client_factory=self.factory
        )

class McpApiTest(McpApiHarness):
    def test_lists_servers_with_tools_and_status(self):
        self.write_config({"servers": [good_entry(env={"ACME_TOKEN": "${MCP_ACME_TOKEN}"})]})
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"MCP_ACME_TOKEN": "s3cret"}):
            self.wire()
        resp = self.client.get("/mcp")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["load_errors"], [])
        item = payload["servers"][0]
        self.assertEqual(item["name"], "io.github.acme/filesystem")
        self.assertEqual(item["status"], "connected")
        self.assertEqual(item["tools"], ["mcp__io-github-acme-filesystem__read_file"])
        self.assertEqual(item["env_keys"], ["ACME_TOKEN"])
        self.assertNotIn("s3cret", resp.text)  # 密钥值脱敏
        self.assertIn("mcp__io-github-acme-filesystem__read_file", server.tool_registry.names())
        # 建连拿到的是解析后的占位符值
        self.assertEqual(self.requested[0].env["ACME_TOKEN"], "s3cret")

    def test_invalid_entry_and_unresolved_placeholder_surface_as_load_errors(self):
        self.write_config(
            {
                "servers": [
                    good_entry(),
                    {"name": "a/broken", "transport": "stdio"},
                    good_entry(name="io.github.acme/needs-env"),
                ]
            }
        )
        self.wire()
        payload = self.client.get("/mcp").json()
        errors = "\n".join(payload["load_errors"])
        self.assertIn("无效", errors)  # 解析期拒绝的条目
        self.assertIn("MCP_ACME_TOKEN", errors)  # 占位符未解析的条目
        statuses = {item["name"]: item["status"] for item in payload["servers"]}
        self.assertEqual(statuses["io.github.acme/filesystem"], "error")
        self.assertEqual(statuses["io.github.acme/needs-env"], "error")

    def test_invalid_json_reported(self):
        self.write_config("{ not json")
        self.wire()
        payload = self.client.get("/mcp").json()
        self.assertIn("JSON", payload["load_errors"][0])

    def test_missing_config_gives_empty_world(self):
        self.wire()  # 未写配置文件 = 空配置（不算错误）
        payload = self.client.get("/mcp").json()
        self.assertEqual(payload, {"servers": [], "load_errors": []})

    def test_management_endpoints_flow(self):
        self.write_config({"servers": [good_entry(env={})]})
        self.wire()
        name = "io.github.acme/filesystem"

        resp = self.client.post("/mcp/disable", json={"name": name})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["server"]["status"], "disabled")
        self.assertNotIn("mcp__io-github-acme-filesystem__read_file", server.tool_registry.names())

        resp = self.client.post("/mcp/enable", json={"name": name})
        self.assertEqual(resp.json()["server"]["status"], "connected")
        self.assertIn("mcp__io-github-acme-filesystem__read_file", server.tool_registry.names())

        self.write_config({"servers": []})
        resp = self.client.post("/mcp/reload")
        data = resp.json()
        self.assertEqual(data["servers"], [])
        self.assertEqual(data["load_errors"], [])

        resp = self.client.post("/mcp/remove", json={"name": name})
        self.assertEqual(resp.status_code, 404)  # 已不在清单

    def test_chat_gate_allows_enabled_mcp_tools(self):
        self.write_config({"servers": [good_entry(env={})]})
        self.wire()
        fake = FakeLLMClient(
            [
                tool_round("mcp__io-github-acme-filesystem__read_file", {"path": "a"}),
                text_round("完成"),
            ]
        )
        server.llm = fake
        resp = self.client.post("/chat/stream", json={"message": "读一下"})
        observations = [
            json.loads(line[len("data: "):])
            for line in resp.iter_lines()
            if line.startswith("data: ")
        ]
        observation = next(f for f in observations if f.get("type") == "observation")
        self.assertFalse(observation["is_error"])  # 闸门放行（非「未激活/未注册」）
        self.assertEqual(observation["text"], "ok")


class McpInstallTest(McpApiHarness):
    """安装来源：目录条目合成连接定义、本地 server.json/mcp.json 导入、mcpb 拒绝。"""

    def setUp(self):
        super().setUp()
        self.dotenv = Path(self._tmp.name) / ".env"
        self._orig_dotenv = server._DOTENV_PATH
        server._DOTENV_PATH = self.dotenv
        self.catalog = FakeCatalogHttp()
        self._orig_catalog = server._catalog_client
        server._catalog_client = lambda: self.catalog  # 工厂注入（缝的形状是可调用）

    def tearDown(self):
        server._catalog_client = self._orig_catalog
        server._DOTENV_PATH = self._orig_dotenv
        super().tearDown()

    def server_json(self, **overrides):
        payload = json.loads(load_catalog_fixture("mcp-registry-detail.json"))
        payload["server"].update(overrides)
        return json.dumps(payload["server"], ensure_ascii=False)

    def install_body(self, **overrides):
        body = {
            "source": "catalog",
            "catalog_id": "io.github.acme/filesystem",
            "source_platform": "mcp-registry",
            "env_values": {"ACME_TOKEN": "s3cret"},
        }
        body.update(overrides)
        return body

    def test_install_from_catalog_composes_command_template(self):
        self.catalog.routes.append(
            (
                "/internal/detail",
                FakeCatalogResponse(
                    {"manifest_path": "server.json", "manifest_text": self.server_json()}
                ),
            )
        )
        self.write_config({"servers": []})
        self.wire()
        resp = self.client.post("/mcp/install", json=self.install_body())
        data = resp.json()
        self.assertEqual(data["results"][0]["status"], "installed")

        listing = self.client.get("/mcp").json()
        item = listing["servers"][0]
        self.assertEqual(item["command"], ["npx", "-y", "@acme/mcp-fs@1.2.3"])
        self.assertEqual(item["transport"], "stdio")
        self.assertEqual(item["source"], "catalog:mcp-registry")
        self.assertEqual(item["status"], "connected")  # 装了即用
        self.assertTrue(item["tools"])
        # 密钥：值进 .env、条目留占位、响应脱敏
        self.assertIn("ACME_TOKEN=s3cret", self.dotenv.read_text(encoding="utf-8"))
        self.assertEqual(item["env_keys"], ["ACME_TOKEN"])
        self.assertNotIn("s3cret", self.client.get("/mcp").text)
        # 零意外外网：仅取详情一次
        self.assertEqual(
            [(method, path) for method, path, _k in self.catalog.calls],
            [("GET", "/internal/detail")],
        )
        # 同名再装 → skipped
        resp = self.client.post("/mcp/install", json=self.install_body())
        self.assertEqual(resp.json()["results"][0]["status"], "skipped")

    def test_install_mcpb_only_rejected_with_warning(self):
        self.catalog.routes.append(
            (
                "/internal/detail",
                FakeCatalogResponse(
                    {
                        "manifest_path": "server.json",
                        "manifest_text": self.server_json(
                            packages=[
                                {
                                    "registryType": "mcpb",
                                    "identifier": "https://github.com/x/y/releases/a.mcpb",
                                    "version": "1.0.0",
                                    "fileSha256": "ab",
                                }
                            ],
                            remotes=[],
                        ),
                    }
                ),
            )
        )
        self.write_config({"servers": []})
        self.wire()
        data = self.client.post("/mcp/install", json=self.install_body()).json()
        self.assertEqual(data["results"][0]["status"], "invalid")
        self.assertIn("mcpb", data["results"][0]["detail"])
        self.assertEqual(self.client.get("/mcp").json()["servers"], [])

    def test_local_import_mcp_json(self):
        file_path = Path(self._tmp.name) / "mcp.json"
        file_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "a/local": {
                            "command": sys.executable,
                            "args": ["-m", "x"],
                            "env": {"K": "${V}"},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.write_config({"servers": []})
        self.wire()
        data = self.client.post(
            "/mcp/install", json={"source": "local", "path": str(file_path)}
        ).json()
        self.assertEqual(data["results"][0]["status"], "installed")
        item = self.client.get("/mcp").json()["servers"][0]
        self.assertEqual(item["command"], [sys.executable, "-m", "x"])
        self.assertEqual(item["source"], f"import:{file_path}")

    def test_local_import_server_json_remote(self):
        file_path = Path(self._tmp.name) / "server.json"
        file_path.write_text(
            self.server_json(
                name="io.github.acme/remote-db",
                packages=[],
                remotes=[
                    {
                        "type": "streamable-http",
                        "url": "https://mcp.acme.dev/db/mcp",
                        "headers": [
                            {
                                "name": "Authorization",
                                "isRequired": True,
                                "isSecret": True,
                            }
                        ],
                    }
                ],
            ),
            encoding="utf-8",
        )
        self.write_config({"servers": []})
        self.wire()
        data = self.client.post(
            "/mcp/install", json={"source": "local", "path": str(file_path)}
        ).json()
        self.assertEqual(data["results"][0]["status"], "installed")
        item = self.client.get("/mcp").json()["servers"][0]
        self.assertEqual(item["transport"], "streamable-http")
        self.assertEqual(item["header_keys"], ["Authorization"])


if __name__ == "__main__":
    unittest.main()
