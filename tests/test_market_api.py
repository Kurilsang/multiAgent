"""主服务在线目录代理与目录包安装测试：fake 爬取服务注入，目录安装路径零意外外网。"""

import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

from fastapi.testclient import TestClient

import app.server as server

from tests.test_server_stream import StubSettings


class FakeCatalogHttp:
    """爬取服务替身（server._catalog_client 注入点）；断言目录安装路径零意外外网。"""

    def __init__(self, routes=None):
        self.routes = list((routes or {}).items())
        self.calls: list[tuple] = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        for needle, result in self.routes:
            if needle in path:
                return result
        return FakeCatalogResponse({}, 404)


class FakeCatalogResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


PACK_OK = {
    "files": [
        {
            "path": "SKILL.md",
            "contents": (
                "---\nname: 市场技能\ndescription: 来自目录\n"
                "allowed-tools: Read, Grep\ntools: []\n---\n\n正文。\n"
            ),
        }
    ],
    "extra_files": ["scripts/run.js"],
    "origin": "owner/repo",
    "source": "skills-sh",
}

PACK_INVALID = {
    "files": [{"path": "SKILL.md", "contents": "没有 frontmatter"}],
    "extra_files": [],
    "source": "skills-sh",
}


class MarketApiTest(unittest.TestCase):
    def setUp(self):
        self._orig = (
            server.settings,
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
            server.mcp_manager,
            server._catalog_client,
        )
        server.settings = StubSettings()
        server.conversation.reset()
        self._tmp = tempfile.TemporaryDirectory()
        (
            server.tool_registry,
            server.skill_registry,
            server.skill_load_errors,
            server.agent_engine,
            server.mcp_manager,
        ) = server._build_wiring(Path(self._tmp.name))
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
            server._catalog_client,
        ) = self._orig
        self._tmp.cleanup()

    def test_market_proxies_pass_through(self):
        fake = FakeCatalogHttp(
            {
                "/internal/sources": FakeCatalogResponse({"sources": []}),
                "/internal/search": FakeCatalogResponse({"items": [1], "total": 1}),
                "/internal/detail": FakeCatalogResponse({"manifest_text": "x"}),
                "/internal/refresh": FakeCatalogResponse({"results": []}),
            }
        )
        server._catalog_client = lambda: fake
        self.assertEqual(self.client.get("/market/sources").json(), {"sources": [], "refreshing": False})
        self.assertEqual(self.client.get("/market/search", params={"q": "pdf"}).json()["total"], 1)
        self.assertEqual(
            self.client.get("/market/detail", params={"id": "a", "source": "s"}).json(),
            {"manifest_text": "x"},
        )

    def test_market_search_forwards_kind_filter(self):
        """统一市场按资产类型分栏检索：kind 参数原样转发爬取服务。"""
        fake = FakeCatalogHttp(
            {"/internal/search": FakeCatalogResponse({"items": [], "total": 0})}
        )
        server._catalog_client = lambda: fake
        self.client.get("/market/search", params={"kind": "mcp"})
        method, path, kwargs = fake.calls[0]
        self.assertEqual((method, path), ("GET", "/internal/search"))
        self.assertEqual(kwargs["params"]["kind"], "mcp")

    def test_disabled_when_base_url_empty(self):
        server.settings.catalog_base_url = ""
        resp = self.client.get("/market/sources")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("未启用", resp.json()["detail"])

    def test_catalog_unreachable_maps_to_502(self):
        class BrokenHttp:
            def request(self, method, path, **kwargs):
                raise ConnectionError("拒绝连接")

        server._catalog_client = lambda: BrokenHttp()
        resp = self.client.get("/market/sources")
        self.assertEqual(resp.status_code, 502)
        self.assertIn("暂不可用", resp.json()["detail"])

    def test_catalog_error_passthrough(self):
        fake = FakeCatalogHttp(
            {"/internal/detail": FakeCatalogResponse({"detail": "未配置的源: x"}, 404)}
        )
        server._catalog_client = lambda: fake
        resp = self.client.get("/market/detail", params={"id": "a", "source": "x"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("未配置的源", resp.json()["detail"])

    def test_install_from_catalog_drops_extras_and_unknown_fields(self):
        fake = FakeCatalogHttp({"/internal/pack": FakeCatalogResponse(PACK_OK)})
        server._catalog_client = lambda: fake
        resp = self.client.post(
            "/skills/install",
            json={"source": "catalog", "catalog_id": "owner/repo/市场技能", "source_platform": "skills-sh"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["results"][0]["status"], "installed")
        self.assertEqual(data["source"], "catalog:skills-sh")
        self.assertIn("已丢弃", data["warnings"][0])
        self.assertIn("scripts/run.js", data["warnings"][0])

        # 只落 SKILL.md 纯提示词：无脚本、未知 frontmatter 字段未进包
        names = [s["name"] for s in self.client.get("/skills").json()["skills"]]
        self.assertEqual(names, ["市场技能"])
        content = self.client.get("/skills/市场技能").json()["content"]
        self.assertNotIn("allowed-tools", content)
        self.assertIn("正文。", content)
        # 取包经爬取服务（零外网断言：fake 记录了唯一一次取包调用）
        self.assertEqual([call[1] for call in fake.calls], ["/internal/pack/owner/repo/市场技能"])

    def test_install_from_catalog_invalid_pack_reported(self):
        fake = FakeCatalogHttp({"/internal/pack": FakeCatalogResponse(PACK_INVALID)})
        server._catalog_client = lambda: fake
        resp = self.client.post(
            "/skills/install",
            json={"source": "catalog", "catalog_id": "a/b/c", "source_platform": "skills-sh"},
        )
        results = resp.json()["results"]
        self.assertEqual(results[0]["status"], "invalid")
        self.assertIn("缺少可用的 name", results[0]["detail"])

    def test_real_catalog_client_is_stdlib_only(self):
        """真实传输层可构造——回归锚：曾因未声明的 httpx 依赖在运行时炸。

        测试缝里 _catalog_client 被 fake 顶替，真实实现从未被执行；
        此测试真实构造它（标准库 urllib，零第三方 HTTP 依赖）。
        """
        client = server._catalog_client()
        self.assertTrue(hasattr(client, "request"))
        self.assertEqual(client._base, "http://catalog.test")

    def test_install_from_catalog_sanitizes_instead_of_rejecting(self):
        """用户实测案例：description 200 字（> 自家 100 上限）应清洗截断后装上。"""
        pack = {
            "files": [
                {
                    "path": "SKILL.md",
                    "contents": (
                        "---\nname: Next.js Development\ndescription: " + "描述" * 100 + "\n"
                        "allowed-tools: Read\ntools: []\n---\n\n正文。\n"
                    ),
                }
            ],
            "extra_files": [],
            "source": "skills-sh",
        }
        server._catalog_client = lambda: FakeCatalogHttp({"/internal/pack": FakeCatalogResponse(pack)})
        resp = self.client.post(
            "/skills/install",
            json={"source": "catalog", "catalog_id": "a/b/c", "source_platform": "skills-sh"},
        )
        results = resp.json()["results"]
        self.assertEqual(results[0]["status"], "installed")
        self.assertEqual(results[0]["name"], "Next-js-Development")
        entry = self.client.get("/skills/Next-js-Development").json()
        self.assertEqual(len(entry["description"]), 100)

    def test_install_requires_catalog_id(self):
        server._catalog_client = lambda: FakeCatalogHttp()
        resp = self.client.post("/skills/install", json={"source": "catalog"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("缺少目录条目", resp.json()["detail"])


class CatalogAutostartTest(unittest.TestCase):
    """本机爬取服务托管：省掉手动第二个终端，独立进程语义不变。"""

    def _settings(self, autostart: bool):
        settings = StubSettings()
        settings.catalog_autostart = autostart
        settings.catalog_base_url = "http://catalog.test"
        return settings

    def test_autostart_spawns_managed_child_when_unreachable(self):
        with unittest.mock.patch.object(server, "settings", self._settings(True)), \
                unittest.mock.patch.object(server, "_catalog_reachable", return_value=False), \
                unittest.mock.patch.object(server.subprocess, "Popen") as popen:
            server._ensure_catalog_service()
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0][1:], ["-m", "services.catalog"])

    def test_autostart_skipped_when_reachable_or_disabled_or_empty(self):
        with unittest.mock.patch.object(server, "settings", self._settings(True)), \
                unittest.mock.patch.object(server, "_catalog_reachable", return_value=True), \
                unittest.mock.patch.object(server.subprocess, "Popen") as popen:
            server._ensure_catalog_service()
        with unittest.mock.patch.object(server, "settings", self._settings(False)), \
                unittest.mock.patch.object(server, "_catalog_reachable", return_value=False), \
                unittest.mock.patch.object(server.subprocess, "Popen") as popen2:
            server._ensure_catalog_service()
        settings = self._settings(True)
        settings.catalog_base_url = ""
        with unittest.mock.patch.object(server, "settings", settings), \
                unittest.mock.patch.object(server.subprocess, "Popen") as popen3:
            server._ensure_catalog_service()
        popen.assert_not_called()
        popen2.assert_not_called()
        popen3.assert_not_called()

    def test_spawn_failure_degrades_gracefully(self):
        with unittest.mock.patch.object(server, "settings", self._settings(True)), \
                unittest.mock.patch.object(server, "_catalog_reachable", return_value=False), \
                unittest.mock.patch.object(server.subprocess, "Popen", side_effect=OSError("no")):
            server._ensure_catalog_service()  # 不抛异常：走 /market/* 中文降级


class MarketRefreshTest(unittest.TestCase):
    """目录刷新异步化：全量爬取分钟级，触发即返回；结果落源状态。

    曾经的坑：同步刷新超出代理 15s 超时即 502（mcp-registry 全量实测 200s+）。
    """

    def setUp(self):
        self._orig = (server.settings, server._catalog_client)
        server.settings = StubSettings()
        server._set_refresh_running(False)
        self._release = threading.Event()
        self._started = threading.Event()
        self.client = TestClient(server.app)

    def tearDown(self):
        self._release.set()  # 放行可能阻塞的后台线程，避免测试悬挂
        for _ in range(200):
            if not server._refresh_running:
                break
            time.sleep(0.01)
        (server.settings, server._catalog_client) = self._orig

    def _blocking_http(self):
        test = self

        class BlockingHttp:
            def __init__(self):
                self.calls: list[tuple] = []

            def request(self, method, path, **kwargs):
                self.calls.append((method, path, kwargs))
                if "/internal/refresh" in path:  # 只堵爬取，源状态查询立回
                    test._started.set()
                    test._release.wait(5)
                    return FakeCatalogResponse({"results": []})
                return FakeCatalogResponse({"sources": []})

        return BlockingHttp()

    def _wait_idle(self):
        for _ in range(200):
            if not server._refresh_running:
                return
            time.sleep(0.01)

    def test_refresh_triggers_background_crawl_with_long_timeout(self):
        fake = self._blocking_http()
        server._catalog_client = lambda: fake
        resp = self.client.post("/market/refresh", json={"max_pages": 7})
        self.assertEqual(resp.json(), {"status": "started"})
        self.assertTrue(self._started.wait(2))
        self._release.set()
        self._wait_idle()
        method, path, kwargs = fake.calls[0]
        self.assertEqual((method, path), ("POST", "/internal/refresh"))
        self.assertGreaterEqual(kwargs.get("timeout", 0), 60)  # 不再被 15s 代理超时腰斩
        self.assertEqual(kwargs.get("json"), {"max_pages": 7})  # 页数预算透传

    def test_refresh_dedupes_while_running(self):
        fake = self._blocking_http()
        server._catalog_client = lambda: fake
        self.assertEqual(self.client.post("/market/refresh", json={}).json(), {"status": "started"})
        self.assertTrue(self._started.wait(2))
        self.assertEqual(self.client.post("/market/refresh", json={}).json(), {"status": "running"})
        self._release.set()
        self._wait_idle()
        self.assertEqual(len(fake.calls), 1)  # 进行中不重复触发

    def test_sources_carries_refreshing_flag(self):
        fake = self._blocking_http()
        server._catalog_client = lambda: fake
        self.client.post("/market/refresh", json={})
        self.assertTrue(self._started.wait(2))
        self.assertTrue(self.client.get("/market/sources").json()["refreshing"])
        self._release.set()
        self._wait_idle()
        self.assertFalse(self.client.get("/market/sources").json()["refreshing"])

    def test_refresh_disabled_when_base_url_empty(self):
        server.settings.catalog_base_url = ""
        resp = self.client.post("/market/refresh", json={})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("未启用", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
