"""会话持久化（SPEC-0005 T2 / issue #56）：会话库、消息落库、健壮性。

用真实临时 SQLite（照 tests/test_catalog_store.py 的先例），不 mock 存储层：
断言的是外部行为——重启后历史还在、库版本不匹配 fail loud、多进程占用被拒。
"""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.sessions import (
    DEFAULT_SESSION_ID,
    SCHEMA_VERSION,
    SessionStore,
    SessionStoreError,
    session_name_from,
)


class SessionStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.db"
        self.store = SessionStore(self.path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    # ---- 消息 ----

    def test_messages_persist_across_reopen(self):
        """核心承诺：重开（= 重启服务）后历史还在。"""
        self.store.append("default", "user", "你好")
        self.store.append(
            "default", "assistant", "你好！", provider="glm", model="glm-4.6"
        )
        self.store.close()
        reopened = SessionStore(self.path)
        try:
            messages = reopened.messages("default")
        finally:
            reopened.close()
        self.assertEqual([m["content"] for m in messages], ["你好", "你好！"])
        self.assertEqual(messages[1]["provider"], "glm")
        self.assertEqual(messages[1]["model"], "glm-4.6")

    def test_message_sequence_is_monotonic(self):
        for index in range(5):
            self.store.append("default", "user", f"m{index}")
        seqs = [m["seq"] for m in self.store.messages("default")]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])

    def test_reasoning_and_steps_roundtrip(self):
        """思考链与工具轮次一并持久化（刷新后与实时页一致）。"""
        steps = [
            {"type": "action", "tool": "use_skill", "arguments": {"name": "x"}},
            {"type": "observation", "tool": "use_skill", "text": "已激活", "is_error": False},
        ]
        self.store.append(
            "default", "assistant", "答案", reasoning="我先想想", steps=steps
        )
        message = self.store.messages("default")[0]
        self.assertEqual(message["reasoning"], "我先想想")
        self.assertEqual(message["steps"], steps)

    def test_failed_turn_is_recorded_with_status(self):
        """失败留痕（不回滚）：状态可见，内容可读。"""
        self.store.append("default", "user", "会失败的问题")
        self.store.append("default", "assistant", "出错了：上游 500", status="failed")
        messages = self.store.messages("default")
        self.assertEqual(messages[0]["status"], "ok")
        self.assertEqual(messages[1]["status"], "failed")
        self.assertIn("上游 500", messages[1]["content"])

    def test_messages_are_scoped_per_session(self):
        self.store.create_session("会话A", session_id="a")
        self.store.create_session("会话B", session_id="b")
        self.store.append("a", "user", "问题A")
        self.store.append("b", "user", "问题B")
        self.assertEqual([m["content"] for m in self.store.messages("a")], ["问题A"])
        self.assertEqual([m["content"] for m in self.store.messages("b")], ["问题B"])

    def test_clear_messages_is_not_offered(self):
        """「清空」的语义是新建会话（SPEC-0005 Q6），不提供原地清空消息的 API。"""
        self.assertFalse(hasattr(self.store, "clear_messages"))

    # ---- 会话 ----

    def test_default_session_is_created_once(self):
        first = self.store.ensure_default_session()
        second = self.store.ensure_default_session()
        self.assertEqual(first, DEFAULT_SESSION_ID)
        self.assertEqual(second, DEFAULT_SESSION_ID)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_create_list_rename_delete(self):
        session_id = self.store.create_session("我的会话")
        self.assertEqual(self.store.get_session(session_id)["name"], "我的会话")
        self.store.rename_session(session_id, "改名了")
        self.assertEqual(self.store.get_session(session_id)["name"], "改名了")
        names = [s["id"] for s in self.store.list_sessions()]
        self.assertIn(session_id, names)
        self.store.delete_session(session_id)
        self.assertIsNone(self.store.get_session(session_id))

    def test_delete_is_hard_and_drops_messages(self):
        session_id = self.store.create_session("待删")
        self.store.append(session_id, "user", "内容")
        self.store.delete_session(session_id)
        self.assertEqual(self.store.messages(session_id), [])
        self.assertEqual(self.store.get_session(session_id), None)

    def test_delete_unknown_session_raises(self):
        with self.assertRaises(SessionStoreError):
            self.store.delete_session("nope")

    def test_rename_unknown_session_raises(self):
        with self.assertRaises(SessionStoreError):
            self.store.rename_session("nope", "x")

    def test_list_sessions_counts_messages(self):
        session_id = self.store.create_session("计数")
        self.store.append(session_id, "user", "a")
        self.store.append(session_id, "assistant", "b")
        entry = next(s for s in self.store.list_sessions() if s["id"] == session_id)
        self.assertEqual(entry["messages"], 2)

    # ---- 健壮性 ----

    def test_wal_mode_is_enabled(self):
        mode = self.store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_schema_version_is_stamped(self):
        self.assertEqual(self.store.schema_version, SCHEMA_VERSION)

    def test_newer_database_version_fails_loud(self):
        """库版本高于代码版本：明确报错，不静默写坏数据。"""
        self.store.close()
        conn = sqlite3.connect(str(self.path))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
        conn.commit()
        conn.close()
        with self.assertRaises(SessionStoreError) as ctx:
            SessionStore(self.path)
        self.assertIn("高于当前代码支持的版本", str(ctx.exception))

    def test_older_database_is_migrated_in_order(self):
        """旧版本库（user_version=0 但有表）能被迁移到当前版本。"""
        self.store.close()
        self.path.unlink()
        conn = sqlite3.connect(str(self.path))
        conn.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
        )
        conn.commit()
        conn.close()
        migrated = SessionStore(self.path)
        try:
            self.assertEqual(migrated.schema_version, SCHEMA_VERSION)
            # 迁移后可正常写入（messages 表已建）
            migrated.create_session("迁移后")
            self.assertEqual(len(migrated.list_sessions()), 1)
        finally:
            migrated.close()

    def test_second_process_on_same_directory_is_rejected(self):
        """多进程共享同一数据目录 = 拒绝启动（心跳新鲜 + pid 确实活着）。"""
        self.store.claim()
        self.store.heartbeat()
        # 起一个真实存活进程，冒充「另一个正在跑的服务」
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            conn = sqlite3.connect(str(self.path))
            conn.execute(
                "UPDATE runtime SET pid = ?, heartbeat_at = ? WHERE id = 1",
                (child.pid, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
            conn.close()
            other = SessionStore(self.path)
            try:
                with self.assertRaises(SessionStoreError) as ctx:
                    other.claim()
                self.assertIn("已被另一个进程占用", str(ctx.exception))
            finally:
                other.close()
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_dead_process_heartbeat_allows_takeover(self):
        """上个进程已死（pid 不存在）但心跳仍「新鲜」：必须允许接管。

        这是 e2e 实测出的真实场景：进程被强杀后心跳留在库里，
        若只看时间戳，正常重启会被自己拒绝。
        """
        self.store.claim()
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            "UPDATE runtime SET pid = ?, heartbeat_at = ? WHERE id = 1",
            (999999, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        conn.close()
        other = SessionStore(self.path)
        try:
            other.claim()  # 不抛错
            self.assertEqual(other.messages("default"), [])
        finally:
            other.close()

    def test_stale_heartbeat_allows_takeover(self):
        """心跳过期：允许接管，不误伤。"""
        self.store.claim()
        stale = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
        conn = sqlite3.connect(str(self.path))
        conn.execute(
            "UPDATE runtime SET pid = ?, heartbeat_at = ? WHERE id = 1", (999999, stale)
        )
        conn.commit()
        conn.close()
        other = SessionStore(self.path)
        try:
            other.claim()  # 不抛错
            self.assertEqual(other.messages("default"), [])
        finally:
            other.close()

    def test_missing_parent_directory_is_created(self):
        nested = Path(self._tmp.name) / "deep" / "nested" / "sessions.db"
        store = SessionStore(nested)
        try:
            store.ensure_default_session()
            self.assertTrue(nested.exists())
        finally:
            store.close()

    # ---- 任务索引（#58：刷新后回放轨迹的查询面） ----

    def test_task_index_records_and_lists(self):
        self.store.ensure_default_session()
        self.store.record_task("t1", "default", "任务一", log_path="/logs/t1.jsonl")
        self.store.record_task("t2", "default", "任务二", log_path="/logs/t2.jsonl")
        self.store.finish_task("t1", "completed")
        tasks = self.store.list_tasks("default")
        self.assertEqual([t["task_id"] for t in tasks], ["t1", "t2"])
        self.assertEqual(tasks[0]["status"], "completed")
        self.assertTrue(tasks[0]["finished_at"])
        self.assertEqual(tasks[1]["status"], "running")
        self.assertEqual(tasks[1]["log_path"], "/logs/t2.jsonl")

    def test_task_index_is_scoped_per_session(self):
        self.store.create_session("A", session_id="a")
        self.store.create_session("B", session_id="b")
        self.store.record_task("t1", "a", "A 的任务")
        self.store.record_task("t2", "b", "B 的任务")
        self.assertEqual([t["task_id"] for t in self.store.list_tasks("a")], ["t1"])
        self.assertEqual([t["task_id"] for t in self.store.list_tasks("b")], ["t2"])

    def test_task_index_survives_reopen(self):
        """索引落库：重启后仍能查到任务归属（回放的前提）。"""
        self.store.record_task("t1", "default", "任务", log_path="/logs/t1.jsonl")
        self.store.finish_task("t1", "partial")
        self.store.close()
        reopened = SessionStore(self.path)
        try:
            entry = reopened.get_task("t1")
        finally:
            reopened.close()
        self.assertEqual(entry["status"], "partial")
        self.assertEqual(entry["session_id"], "default")

    def test_v1_database_migrates_to_v2_gaining_task_index(self):
        """既有 v1 库（有会话与消息、无 tasks 表）能被迁移，数据不丢。"""
        self.store.close()
        legacy = Path(self._tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        # 手工造一个 v1 形态的库：只跑 v1 迁移并盖版本号
        conn.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,"
            " content TEXT NOT NULL, provider TEXT, model TEXT,"
            " status TEXT NOT NULL DEFAULT 'ok', reasoning TEXT, steps TEXT,"
            " created_at TEXT NOT NULL);"
            "CREATE TABLE runtime (id INTEGER PRIMARY KEY CHECK (id = 1),"
            " pid INTEGER NOT NULL, heartbeat_at TEXT NOT NULL);"
            "PRAGMA user_version = 1;"
        )
        conn.execute(
            "INSERT INTO sessions (id, name, created_at, updated_at) VALUES"
            " ('default', '旧会话', '2026-10-10T10:00:00', '2026-10-10T10:00:00')"
        )
        conn.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at)"
            " VALUES ('default', 1, 'user', '旧消息', '2026-10-10T10:00:00')"
        )
        conn.commit()
        conn.close()

        migrated = SessionStore(legacy)
        try:
            self.assertEqual(migrated.schema_version, SCHEMA_VERSION)
            # 旧数据完好
            self.assertEqual(
                [m["content"] for m in migrated.messages("default")], ["旧消息"]
            )
            # 新表可用
            migrated.record_task("t1", "default", "新任务")
            self.assertEqual(len(migrated.list_tasks("default")), 1)
        finally:
            migrated.close()


class SessionSummaryTest(unittest.TestCase):
    """滚动摘要存储（SPEC-0006 T3）：每会话一条，覆盖水位单调。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "sessions.db"
        self.store = SessionStore(self.path)

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_no_summary_initially(self):
        self.assertIsNone(self.store.get_summary("default"))

    def test_save_and_get_summary_roundtrip(self):
        self.store.save_summary("default", covered_seq=5, content="摘要内容")
        summary = self.store.get_summary("default")
        self.assertEqual(summary["covered_seq"], 5)
        self.assertEqual(summary["content"], "摘要内容")

    def test_summary_upserts_single_row(self):
        self.store.save_summary("default", covered_seq=5, content="旧")
        self.store.save_summary("default", covered_seq=12, content="新")
        summary = self.store.get_summary("default")
        self.assertEqual(summary["covered_seq"], 12)
        self.assertEqual(summary["content"], "新")

    def test_summary_persists_across_reopen(self):
        self.store.save_summary("default", covered_seq=7, content="持久化摘要")
        self.store.close()
        reopened = SessionStore(self.path)
        try:
            self.assertEqual(reopened.get_summary("default")["content"], "持久化摘要")
        finally:
            reopened.close()

    def test_v2_database_migrates_to_v3_gaining_summary_table(self):
        """既有 v2 库能被迁移，数据不丢、新表可用。"""
        self.store.close()
        legacy = Path(self._tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '',"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL);"
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " session_id TEXT NOT NULL, seq INTEGER NOT NULL, role TEXT NOT NULL,"
            " content TEXT NOT NULL, provider TEXT, model TEXT,"
            " status TEXT NOT NULL DEFAULT 'ok', reasoning TEXT, steps TEXT,"
            " created_at TEXT NOT NULL);"
            "CREATE TABLE runtime (id INTEGER PRIMARY KEY CHECK (id = 1),"
            " pid INTEGER NOT NULL, heartbeat_at TEXT NOT NULL);"
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
            " task TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,"
            " finished_at TEXT, log_path TEXT NOT NULL DEFAULT '');"
            "PRAGMA user_version = 2;"
        )
        conn.execute(
            "INSERT INTO sessions (id, name, created_at, updated_at) VALUES"
            " ('default', '旧会话', '2026-10-10T10:00:00', '2026-10-10T10:00:00')"
        )
        conn.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at)"
            " VALUES ('default', 1, 'user', '旧消息', '2026-10-10T10:00:00')"
        )
        conn.commit()
        conn.close()

        migrated = SessionStore(legacy)
        try:
            self.assertEqual(migrated.schema_version, SCHEMA_VERSION)
            self.assertEqual(
                [m["content"] for m in migrated.messages("default")], ["旧消息"]
            )
            migrated.save_summary("default", covered_seq=3, content="迁移后摘要")
            self.assertEqual(migrated.get_summary("default")["content"], "迁移后摘要")
        finally:
            migrated.close()


class SessionStorageConventionTest(unittest.TestCase):
    """会话库的落盘约定：数据目录不入库、默认路径指向 data/。"""

    def test_data_directory_is_gitignored(self):
        """会话库不落版本库：机械守住 .gitignore 的 data/ 条目。"""
        root = Path(__file__).resolve().parents[1]
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/", ignored)

    def test_default_session_db_path_points_into_data(self):
        """默认值指向 data/（读字段默认值，避开环境变量覆盖）。"""
        from app.config import Settings

        self.assertEqual(Settings.model_fields["session_db"].default, "data/sessions.db")


class SessionNameTest(unittest.TestCase):
    def test_name_from_first_message_is_truncated(self):
        self.assertEqual(session_name_from("短问题"), "短问题")

    def test_long_message_is_collapsed_and_truncated(self):
        name = session_name_from(
            "  这是一个   很长很长很长很长很长很长很长很长很长的问题  "
        )
        self.assertLessEqual(len(name), 21)
        self.assertTrue(name.endswith("…"))
        self.assertNotIn("  ", name)

    def test_empty_message_yields_empty_name(self):
        self.assertEqual(session_name_from(""), "")


if __name__ == "__main__":
    unittest.main()
