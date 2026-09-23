"""爬取服务 HTTP 面测试：fake 源 + 内存存储，不发真实请求。"""

import unittest

from fastapi.testclient import TestClient

from services.catalog.app import create_app
from services.catalog.config import CatalogSettings
from services.catalog.schema import CatalogError, SkillDetail, SkillPack
from services.catalog.store import MemoryStore

from tests.fakes import (
    FakeCatalogSource,
    load_catalog_fixture,
    make_catalog_entries,
)


class CatalogAppTest(unittest.TestCase):
    def setUp(self):
        self.source = FakeCatalogSource(entries=make_catalog_entries(5, source="fake"))
        self.store = MemoryStore()
        self.client = TestClient(create_app({"fake": self.source}, self.store))

    def test_health_reports_app_identity(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.json(), {"status": "ok", "app": "multiagent-catalog"})

    def test_refresh_then_search_paginates(self):
        resp = self.client.post("/internal/refresh", json={})
        self.assertEqual(resp.json()["results"], [{"source": "fake", "status": "ok", "count": 5}])
        self.assertEqual(self.source.crawl_calls, 1)

        resp = self.client.get("/internal/search", params={"page": 1, "page_size": 2})
        data = resp.json()
        self.assertEqual(data["total"], 5)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["id"], "fake/skill-1")
        self.assertTrue(data["refreshed_at"])

        resp = self.client.get("/internal/search", params={"page": 3, "page_size": 2})
        self.assertEqual(len(resp.json()["items"]), 1)

    def test_search_filters_by_query_and_source(self):
        self.client.post("/internal/refresh", json={})
        resp = self.client.get("/internal/search", params={"q": "skill-3"})
        self.assertEqual(resp.json()["total"], 1)
        resp = self.client.get("/internal/search", params={"source": "other"})
        self.assertEqual(resp.json()["total"], 0)

    def test_refresh_error_keeps_cache_and_marks_stale(self):
        self.client.post("/internal/refresh", json={})
        self.source.error = CatalogError("平台不可达", status=502)
        resp = self.client.post("/internal/refresh", json={})
        self.assertEqual(resp.json()["results"][0]["status"], "error")

        state = {item["source"]: item for item in self.client.get("/internal/sources").json()["sources"]}
        self.assertTrue(state["fake"]["stale"])
        self.assertIn("平台不可达", state["fake"]["last_error"])
        # 降级读缓存：旧条目仍在
        self.assertEqual(self.client.get("/internal/search").json()["total"], 5)

    def test_sources_lists_configured_but_never_refreshed(self):
        resp = self.client.get("/internal/sources")
        item = resp.json()["sources"][0]
        self.assertEqual(item["source"], "fake")
        self.assertTrue(item["stale"])

    def test_refresh_unknown_source_reported(self):
        resp = self.client.post("/internal/refresh", json={"source": "nope"})
        self.assertEqual(resp.json()["results"][0]["status"], "unknown_source")

    def test_detail_and_pack_passthrough(self):
        entry = make_catalog_entries(1)[0]
        self.source.detail_result = SkillDetail(entry=entry, skill_md=load_catalog_fixture("fake-skill.md"))
        self.source.pack_result = SkillPack(
            files=(("SKILL.md", "---\nname: x\n---\n正文"),),
            extra_files=("run.js",),
            origin="owner/repo",
            source="fake",
        )
        resp = self.client.get("/internal/detail", params={"id": "fake/skill-1", "source": "fake"})
        self.assertIn("name: fake", resp.json()["skill_md"])
        resp = self.client.get("/internal/pack/fake/skill-1", params={"source": "fake"})
        data = resp.json()
        self.assertEqual(data["files"][0]["path"], "SKILL.md")
        self.assertEqual(data["extra_files"], ["run.js"])

    def test_unknown_source_returns_404(self):
        resp = self.client.get("/internal/detail", params={"id": "x", "source": "nope"})
        self.assertEqual(resp.status_code, 404)

    def test_source_error_mapped_to_status(self):
        self.source.error = CatalogError("没找到技能", status=404)
        resp = self.client.get("/internal/pack/a/b", params={"source": "fake"})
        self.assertEqual(resp.status_code, 404)
        self.assertIn("没找到技能", resp.json()["detail"])


class IsolationTest(unittest.TestCase):
    """/internal/* 默认仅本机回环——隔离边界的机械核验。"""

    def test_default_host_is_loopback(self):
        self.assertEqual(CatalogSettings.from_env().host, "127.0.0.1")

    def test_env_overrides(self):
        import os
        from unittest import mock

        with mock.patch.dict(
            os.environ,
            {"CATALOG_HOST": "0.0.0.0", "CATALOG_PORT": "9000", "CATALOG_SOURCES": "skills-sh"},
        ):
            settings = CatalogSettings.from_env()
        self.assertEqual((settings.host, settings.port, settings.sources), ("0.0.0.0", 9000, ("skills-sh",)))


if __name__ == "__main__":
    unittest.main()
