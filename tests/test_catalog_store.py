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
