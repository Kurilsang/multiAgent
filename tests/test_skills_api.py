"""技能管理 HTTP API 测试：临时技能目录 + TestClient，不发真实请求。"""

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import app.server as server

from tests.test_server_stream import StubSettings


class SkillsApiTest(unittest.TestCase):
    def setUp(self):
        self._orig = (
            server.settings,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
        )
        server.settings = StubSettings()
        server.conversation.reset()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
        ) = server._build_wiring(self.root)
        server.skill_registry.create("时间报告", "日期推算", "先取时间", ("calculator",))
        self.client = TestClient(server.app)

    def tearDown(self):
        server.conversation.reset()
        (
            server.settings,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
        ) = self._orig
        self._tmp.cleanup()

    def test_list_and_get_skill(self):
        resp = self.client.get("/skills")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["skills"][0]["name"], "时间报告")
        self.assertEqual(payload["skills"][0]["tools"], ["calculator"])
        self.assertTrue(payload["skills"][0]["enabled"])
        self.assertEqual(payload["load_errors"], [])

        resp = self.client.get("/skills/时间报告")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("name: 时间报告", resp.json()["content"])

    def test_unknown_skill_returns_404(self):
        resp = self.client.get("/skills/nope")
        self.assertEqual(resp.status_code, 404)
        self.assertIn("未找到技能", resp.json()["detail"])

    def test_enable_disable_delete(self):
        resp = self.client.post("/skills/时间报告/disable")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["skill"]["enabled"])

        resp = self.client.get("/skills")
        self.assertFalse(resp.json()["skills"][0]["enabled"])

        resp = self.client.post("/skills/时间报告/enable")
        self.assertTrue(resp.json()["skill"]["enabled"])

        resp = self.client.delete("/skills/时间报告")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get("/skills").json()["skills"], [])
        self.assertEqual(self.client.delete("/skills/时间报告").status_code, 404)


if __name__ == "__main__":
    unittest.main()
