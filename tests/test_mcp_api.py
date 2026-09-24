"""MCP 管理 HTTP API 测试：fake client factory 注入 + 临时目录，零真实进程/网络。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app.server as server

from tests.fakes import FakeLLMClient, FakeMcpSession, make_mcp_tool, text_round, tool_round
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


class McpApiTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
