"""SQLite 目录缓存、凭证仓与刷新器测试（临时库，零网络）。"""

import tempfile
import threading
import unittest
from pathlib import Path

from services.catalog.refresher import refresh_loop, refresh_now, start_scheduler
from services.catalog.store import MemoryStore, SqliteStore

from tests.fakes import FakeCatalogSource, make_catalog_entries


class SqliteStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "catalog.db"
        self.store = SqliteStore(self.path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_replace_search_persists_across_instances(self):
        self.store.replace_source("fake", make_catalog_entries(5), "2026-09-23T00:00:00Z")
        items, total = self.store.search(page=1, page_size=2)
        self.assertEqual((len(items), total), (2, 5))
        self.store.close()
        self.store = SqliteStore(self.path)  # 重启后目录仍可用
        _, total = self.store.search()
        self.assertEqual(total, 5)

    def test_search_filters_by_query_and_source(self):
        self.store.replace_source("a", make_catalog_entries(3, source="a"), "t1")
        self.store.replace_source("b", make_catalog_entries(2, source="b", prefix="other"), "t1")
        self.assertEqual(self.store.search(q="skill-2")[1], 1)
        self.assertEqual(self.store.search(source="b")[1], 2)
        self.assertEqual(self.store.search(q="zzz")[1], 0)

    def test_search_filters_by_kind(self):
        from services.catalog.schema import CatalogEntry

        self.store.replace_source("a", make_catalog_entries(2, source="a"), "t1")
        self.store.replace_source(
            "b",
            [
                CatalogEntry(
                    id="io.x/y", name="n", description="d", source="b", origin="o",
                    kind="mcp",
                )
            ],
            "t1",
        )
        self.assertEqual(self.store.search(kind="mcp")[1], 1)
        self.assertEqual(self.store.search(kind="mcp")[0][0].id, "io.x/y")
        self.assertEqual(self.store.search(kind="skill")[1], 2)
        self.assertEqual(self.store.search(kind="")[1], 3)

    def test_audit_tags_roundtrip(self):
        from services.catalog.schema import AuditBadge, CatalogEntry

        entry = CatalogEntry(
            id="a/b", name="n", description="d", source="a", origin="o",
            tags=("x", "y"),
            audits=(AuditBadge(provider="Snyk", status="pass", summary="ok", risk_level="LOW"),),
            validated=True,
        )
        self.store.replace_source("a", [entry], "t1")
        loaded = self.store.search()[0][0]
        self.assertEqual(loaded.tags, ("x", "y"))
        self.assertEqual(loaded.audits[0].provider, "Snyk")
        self.assertEqual(loaded.audits[0].risk_level, "LOW")
        self.assertTrue(loaded.validated)

    def test_kind_roundtrips_in_cache(self):
        from services.catalog.schema import CatalogEntry

        entry = CatalogEntry(
            id="io.x/y", name="n", description="d", source="a", origin="o", kind="mcp"
        )
        self.store.replace_source("a", [entry], "t1")
        self.assertEqual(self.store.search()[0][0].kind, "mcp")

    def test_legacy_db_without_kind_column_migrates(self):
        import sqlite3

        self.store.close()
        self.path.unlink()
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            "CREATE TABLE entries (source TEXT NOT NULL, id TEXT NOT NULL,"
            " name TEXT NOT NULL, description TEXT NOT NULL, origin TEXT NOT NULL,"
            " installs INTEGER NOT NULL DEFAULT 0, stars INTEGER NOT NULL DEFAULT 0,"
            " tags TEXT NOT NULL DEFAULT '[]', detail_url TEXT NOT NULL DEFAULT '',"
            " install_ref TEXT NOT NULL DEFAULT '', audits TEXT NOT NULL DEFAULT '[]',"
            " validated INTEGER NOT NULL DEFAULT 0,"
            " is_duplicate INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO entries (source, id, name, description, origin)"
            " VALUES ('a', 'a/b', 'n', 'd', 'o')"
        )
        conn.commit()
        conn.close()
        self.store = SqliteStore(self.path)  # 旧库（无 kind 列）自动迁移
        items, total = self.store.search()
        self.assertEqual(total, 1)
        self.assertEqual(items[0].kind, "skill")

    def test_status_roundtrips_in_cache(self):
        from services.catalog.schema import CatalogEntry

        entry = CatalogEntry(
            id="a/b", name="n", description="d", source="a", origin="o",
            status="deprecated", status_message="改用别的",
        )
        self.store.replace_source("a", [entry], "t1")
        loaded = self.store.search()[0][0]
        self.assertEqual((loaded.status, loaded.status_message), ("deprecated", "改用别的"))

    def test_record_error_keeps_cache_and_marks_stale(self):
        self.store.replace_source("fake", make_catalog_entries(5), "t1")
        self.store.record_error("fake", "平台不可达")
        state = self.store.sources()[0]
        self.assertTrue(state["stale"])
        self.assertEqual(state["entry_count"], 5)
        self.assertIn("平台不可达", state["last_error"])
        self.assertEqual(self.store.search()[1], 5)  # 降级读缓存

    def test_record_warning_visible_without_stale_and_cleared_by_replace(self):
        self.store.replace_source("fake", make_catalog_entries(2), "t1")
        self.store.record_warning("fake", "已截断：仅爬取前 200 页，仍有更多")
        state = self.store.sources()[0]
        self.assertIn("截断", state["warning"])
        self.assertFalse(state["stale"])  # 截断告警 ≠ 抓取失败
        self.store.replace_source("fake", make_catalog_entries(3), "t2")  # 完整爬取清告警
        self.assertEqual(self.store.sources()[0]["warning"], "")

    def test_successful_replace_resets_stale(self):
        self.store.replace_source("fake", make_catalog_entries(1), "t1")
        self.store.record_error("fake", "x")
        self.store.replace_source("fake", make_catalog_entries(2), "t2")
        state = self.store.sources()[0]
        self.assertFalse(state["stale"])
        self.assertEqual(state["entry_count"], 2)
        self.assertEqual(self.store.refreshed_at(), "t2")


class MemoryStoreSearchTest(unittest.TestCase):
    def test_search_filters_by_kind(self):
        from services.catalog.schema import CatalogEntry

        store = MemoryStore()
        store.replace_source("a", make_catalog_entries(2, source="a"), "t1")
        store.replace_source(
            "b",
            [
                CatalogEntry(
                    id="io.x/y", name="n", description="d", source="b", origin="o",
                    kind="mcp",
                )
            ],
            "t1",
        )
        self.assertEqual(store.search(kind="mcp")[1], 1)
        self.assertEqual(store.search(kind="skill")[1], 2)
        self.assertEqual(store.search(kind="")[1], 3)


class CredsStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SqliteStore(Path(self._tmp.name) / "catalog.db")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_creds_roundtrip_persists(self):
        creds = self.store.creds("lobehub")
        self.assertIsNone(creds.get("client_id"))
        creds.update({"client_id": "cid", "client_secret": "sec"})
        self.assertEqual(self.store.creds("lobehub").get("client_id"), "cid")
        self.store.close()
        self.store = SqliteStore(Path(self._tmp.name) / "catalog.db")
        self.assertEqual(self.store.creds("lobehub").get("client_secret"), "sec")


class RefresherTest(unittest.TestCase):
    def test_refresh_now_reports_per_source(self):
        store = MemoryStore()
        ok = FakeCatalogSource(name="a", entries=make_catalog_entries(3, source="a"))
        bad = FakeCatalogSource(name="b", error=RuntimeError("平台炸了"))
        results = refresh_now({"a": ok, "b": bad}, store)
        statuses = {item["source"]: item["status"] for item in results}
        self.assertEqual(statuses, {"a": "ok", "b": "error"})
        state = {item["source"]: item for item in store.sources()}
        self.assertEqual(state["a"]["entry_count"], 3)
        self.assertTrue(state["b"]["stale"])

    def test_refresh_passes_page_budget_and_records_truncation_warning(self):
        store = MemoryStore()
        source = FakeCatalogSource(
            name="a",
            entries=make_catalog_entries(2, source="a"),
            warnings=["已截断：仅爬取前 3 页，仍有更多"],
        )
        results = refresh_now({"a": source}, store, max_pages=9)
        self.assertEqual(source.crawl_max_pages, [9])  # 主动预算透传给适配器
        self.assertEqual(results[0]["warnings"], ["已截断：仅爬取前 3 页，仍有更多"])
        self.assertIn("截断", store.sources()[0]["warning"])  # 告警落源状态
        clean = FakeCatalogSource(name="b", entries=[])
        results = refresh_now({"b": clean}, store)
        self.assertEqual(clean.crawl_max_pages, [3])  # 未传预算走适配器默认
        self.assertEqual(results[0]["warnings"], [])

    def test_refresh_skips_when_running_and_dedupes_warnings(self):
        store = MemoryStore()
        release = threading.Event()
        started = threading.Event()

        class BlockingSource(FakeCatalogSource):
            def crawl(self, max_pages: int = 3):
                started.set()
                release.wait(5)
                return super().crawl(max_pages=max_pages)

        slow = BlockingSource(
            name="a",
            entries=make_catalog_entries(1, source="a"),
            warnings=["已截断", "已截断"],  # 并发爬取曾把告警叠加两遍
        )
        thread = threading.Thread(target=refresh_now, args=({"a": slow}, store))
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            skipped = refresh_now({"a": slow}, store)  # 并发触发：跳过，不重复爬
            self.assertEqual(skipped[0]["status"], "refreshing")
        finally:
            release.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        results = refresh_now({"a": slow}, store)
        self.assertEqual(results[0]["warnings"], ["已截断"])  # 重复告警去重
        self.assertEqual(store.sources()[0]["warning"], "已截断")

    def test_refresh_now_unknown_source_reported(self):
        results = refresh_now({}, MemoryStore(), ["nope"])
        self.assertEqual(results[0]["status"], "unknown_source")

    def test_refresh_loop_cycles_then_stops(self):
        store = MemoryStore()
        source = FakeCatalogSource(name="a", entries=make_catalog_entries(2, source="a"))
        stop = threading.Event()
        cycles: list = []

        def on_cycle(results):
            cycles.append(results)
            stop.set()

        thread = threading.Thread(
            target=refresh_loop,
            kwargs={
                "sources": {"a": source},
                "store": store,
                "interval_seconds": 0.01,
                "stop_event": stop,
                "on_cycle": on_cycle,
            },
        )
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(len(cycles), 1)
        self.assertEqual(store.search()[1], 2)

    def test_start_scheduler_refreshes_immediately(self):
        store = MemoryStore()
        source = FakeCatalogSource(name="a", entries=make_catalog_entries(1, source="a"))
        thread, stop = start_scheduler({"a": source}, store, interval_seconds=60)
        try:
            event = threading.Event()
            for _ in range(100):
                if store.search()[1] > 0:
                    break
                event.wait(0.02)
            self.assertEqual(store.search()[1], 1)
        finally:
            stop.set()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
