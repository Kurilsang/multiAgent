"""会话持久化：主对话历史落 SQLite（SPEC-0005 / issue #56）。

存储边界（SPEC-0005 Q3=C）：会话历史是**可变实体**（改名/删除/按 id 取），
进 SQLite；任务轨迹继续每任务一份 append-only JSONL——审计日志是唯一权威
副本，复制进库会立刻产生「两份不一致信谁」的问题。

本模块自带的健壮性约定：
- 开 WAL：后台任务线程写事件与 HTTP 线程读历史是真实并发；
- `PRAGMA user_version` 版本化迁移，**库版本高于代码版本即 fail loud**；
- 多进程共享检测：运行时心跳行 + pid 存活判定，发现别的活进程占用同一
  数据目录就拒绝启动，而不是静默写坏库。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

# 会话库 schema 版本：新增迁移时 +1 并在 _MIGRATIONS 登记
SCHEMA_VERSION = 3
# 默认会话标识：不带 session_id 的请求落到这里（既有调用方式不破）
DEFAULT_SESSION_ID = "default"
# 会话名缺省取首条用户消息的前若干字符
SESSION_NAME_CHARS = 20
# 心跳新鲜窗口：超过这个间隔未跳动视为「上个进程已死」
HEARTBEAT_FRESH_SECONDS = 300.0

_HEADER_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    provider TEXT,
    model TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    reasoning TEXT,
    steps TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE TABLE IF NOT EXISTS runtime (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    pid INTEGER NOT NULL,
    heartbeat_at TEXT NOT NULL
);
"""

# v2：任务索引（只存索引，事件仍以审计 JSONL 为唯一权威副本）
_TASKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    log_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_session ON tasks(session_id, started_at);
"""

# v3：滚动摘要（SPEC-0006 T3，每会话一条，覆盖水位单调推进）
_SUMMARY_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_summaries (
    session_id TEXT PRIMARY KEY,
    covered_seq INTEGER NOT NULL,
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SessionStoreError(Exception):
    """会话库不可用（版本不匹配、被其他进程占用等），信息面向用户。"""


def _migrate_to_1(conn) -> None:
    """v1：会话 / 消息 / 运行时心跳表（首版结构）。"""
    conn.executescript(_HEADER_SCHEMA)


def _migrate_to_2(conn) -> None:
    """v2：任务索引表（刷新后按会话回放轨迹的查询面）。"""
    conn.executescript(_TASKS_SCHEMA)


def _migrate_to_3(conn) -> None:
    """v3：滚动摘要表（会话内记忆，SPEC-0006）。"""
    conn.executescript(_SUMMARY_SCHEMA)


# 版本 → 迁移函数（模块级：存进字典后不会被绑定成方法）
_MIGRATIONS = {1: _migrate_to_1, 2: _migrate_to_2, 3: _migrate_to_3}


def session_name_from(text: str) -> str:
    """会话名缺省取首条用户消息（截断 + 折叠空白）。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= SESSION_NAME_CHARS:
        return flat
    return flat[:SESSION_NAME_CHARS] + "…"


def _pid_alive(pid: int) -> bool:
    """该 pid 是否仍是活进程（纯 stdlib、跨平台）。

    只靠心跳时间戳判断占用是不够的：进程被强杀后心跳会「新鲜」地留在库里，
    正常重启反而被自己拒绝。这里用系统事实判定：进程死了就允许接管。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                # STILL_ACTIVE == 259：进程还活着（含我们自己的 pid）
                return exit_code.value == 259
            return True
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX：信号 0 只做存在性/权限检查
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不属于当前用户
    return True


class SessionStore:
    """会话与消息的 SQLite 存储：单连接 + 锁（与既有 SQLite 先例同形状）。"""

    def __init__(self, path: Path | str, *, now: datetime | None = None):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._now = now
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        except sqlite3.Error as exc:
            raise SessionStoreError(f"会话库无法打开：{exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            # WAL：读不被写阻塞（后台任务写事件、HTTP 线程读历史）
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._migrate()
        except SessionStoreError:
            self._conn.close()  # 版本不匹配等：别把句柄留在半开状态
            raise
        except sqlite3.Error as exc:
            self._conn.close()
            raise SessionStoreError(f"会话库初始化失败：{exc}") from exc

    # ---- schema ----

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise SessionStoreError(
                    f"会话库版本（{version}）高于当前代码支持的版本（{SCHEMA_VERSION}）："
                    "请升级代码，或改用其他 SESSION_DB 路径，避免把数据写坏"
                )
            for target in range(version + 1, SCHEMA_VERSION + 1):
                _MIGRATIONS[target](self._conn)
                self._conn.execute(f"PRAGMA user_version = {target}")
            self._conn.commit()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # ---- 多进程共享检测（心跳行 + pid 比对） ----

    def claim(self) -> None:
        """认领数据目录：发现别的**活进程**占用同一库时拒绝启动。

        判定 = 心跳新鲜 **且** 该 pid 仍是活进程。只看心跳会把「被强杀后
        残留的新鲜心跳」误判成占用，导致正常重启被自己拒绝（e2e 实测过）。
        """
        pid = os.getpid()
        with self._lock:
            row = self._conn.execute(
                "SELECT pid, heartbeat_at FROM runtime WHERE id = 1"
            ).fetchone()
            if row is not None and row["pid"] != pid:
                age = self._seconds_since(row["heartbeat_at"])
                fresh = age is not None and age < HEARTBEAT_FRESH_SECONDS
                if fresh and _pid_alive(int(row["pid"])):
                    raise SessionStoreError(
                        f"会话库已被另一个进程占用（pid={row['pid']}，"
                        f"{age:.0f} 秒前仍在心跳）：本服务不支持多进程共享同一数据目录，"
                        "请先停掉那个进程，或用 SESSION_DB 指向另一个文件"
                    )
            self._conn.execute(
                "INSERT INTO runtime (id, pid, heartbeat_at) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, "
                "heartbeat_at = excluded.heartbeat_at",
                (pid, self._stamp()),
            )
            self._conn.commit()

    def heartbeat(self) -> None:
        """刷新心跳（每 30 秒由后台线程调用，让占用检测真正有意义）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE runtime SET heartbeat_at = ? WHERE id = 1 AND pid = ?",
                (self._stamp(), os.getpid()),
            )
            self._conn.commit()

    def start_heartbeat(self, interval: float = 30.0) -> None:
        if self._heartbeat_thread is not None:
            return

        def loop() -> None:
            while not self._heartbeat_stop.wait(interval):
                try:
                    self.heartbeat()
                except sqlite3.Error:
                    return  # 库已不可写：静默退出，不影响主流程

        self._heartbeat_thread = threading.Thread(
            target=loop, daemon=True, name="session-heartbeat"
        )
        self._heartbeat_thread.start()

    def _seconds_since(self, stamp: str) -> float | None:
        try:
            then = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return None
        return ((self._now or datetime.now()) - then).total_seconds()

    def _stamp(self) -> str:
        return (self._now or datetime.now()).isoformat(timespec="seconds")

    # ---- 会话 ----

    def ensure_default_session(self) -> str:
        """默认会话（不带 session_id 的请求落点）：不存在则建。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM sessions WHERE id = ?", (DEFAULT_SESSION_ID,)
            ).fetchone()
            if row is None:
                stamp = self._stamp()
                self._conn.execute(
                    "INSERT INTO sessions (id, name, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (DEFAULT_SESSION_ID, "默认会话", stamp, stamp),
                )
                self._conn.commit()
            return DEFAULT_SESSION_ID

    def create_session(self, name: str = "", session_id: str | None = None) -> str:
        """新建会话；返回会话标识。"""
        import uuid

        sid = session_id or uuid.uuid4().hex[:8]
        stamp = self._stamp()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (id, name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, name.strip(), stamp, stamp),
            )
            self._conn.commit()
        return sid

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, name, created_at, updated_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_sessions(self) -> list[dict]:
        """会话列表：最近更新在前，附消息条数。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.name, s.created_at, s.updated_at, "
                "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS messages "
                "FROM sessions s ORDER BY s.updated_at DESC, s.id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def rename_session(self, session_id: str, name: str) -> None:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE sessions SET name = ?, updated_at = ? WHERE id = ?",
                (name.strip(), self._stamp(), session_id),
            )
            self._conn.commit()
        if cursor.rowcount == 0:
            raise SessionStoreError(f"未找到会话: {session_id}")

    def delete_session(self, session_id: str) -> None:
        """硬删会话及其消息（单用户，软删只增状态）。"""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE id = ?", (session_id,)
            )
            self._conn.execute(
                "DELETE FROM messages WHERE session_id = ?", (session_id,)
            )
            self._conn.commit()
        if cursor.rowcount == 0:
            raise SessionStoreError(f"未找到会话: {session_id}")

    # ---- 消息 ----

    def append(
        self,
        session_id: str,
        role: str,
        content: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str = "ok",
        reasoning: str | None = None,
        steps: list[dict] | None = None,
    ) -> dict:
        """追加一条消息（用户消息在调用模型之前就落库，崩溃也不丢）。

        status 表达该轮终态（ok / failed / partial）；reasoning 存思考链，
        steps 存聊天通道的工具轮次（思考/行动/观察）——刷新后与实时页一致。
        """
        stamp = self._stamp()
        steps_text = json.dumps(steps, ensure_ascii=False) if steps else None
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS last FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            seq = int(row["last"]) + 1
            self._conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, provider, "
                "model, status, reasoning, steps, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    role,
                    content,
                    provider,
                    model,
                    status,
                    reasoning,
                    steps_text,
                    stamp,
                ),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id)
            )
            self._conn.commit()
        return {
            "seq": seq,
            "role": role,
            "content": content,
            "provider": provider,
            "model": model,
            "status": status,
            "reasoning": reasoning or "",
            "steps": steps or [],
            "created_at": stamp,
        }

    def messages(self, session_id: str) -> list[dict]:
        """按序读回会话消息（展示全量；入模截断由 Conversation 负责）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, role, content, provider, model, status, reasoning, "
                "steps, created_at FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            item["reasoning"] = item.get("reasoning") or ""
            raw = item.pop("steps", None)
            try:
                item["steps"] = json.loads(raw) if raw else []
            except ValueError:
                item["steps"] = []
            out.append(item)
        return out

    # ---- 滚动摘要（SPEC-0006 T3）：每会话一条，覆盖水位单调推进 ----

    def get_summary(self, session_id: str) -> dict | None:
        """读回会话的滚动摘要；无摘要返回 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT covered_seq, content FROM session_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return {"covered_seq": row["covered_seq"], "content": row["content"]}

    def save_summary(self, session_id: str, *, covered_seq: int, content: str) -> None:
        """写入/覆盖会话滚动摘要（单行 upsert）。"""
        stamp = self._stamp()
        with self._lock:
            self._conn.execute(
                "INSERT INTO session_summaries (session_id, covered_seq, content, updated_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(session_id) DO UPDATE SET"
                " covered_seq = excluded.covered_seq,"
                " content = excluded.content,"
                " updated_at = excluded.updated_at",
                (session_id, covered_seq, content, stamp),
            )
            self._conn.commit()

    # ---- 任务索引（事件仍以审计 JSONL 为唯一权威副本） ----

    def record_task(
        self,
        task_id: str,
        session_id: str,
        task: str,
        status: str = "running",
        log_path: str = "",
    ) -> None:
        """登记一次任务（启动时调用；终态由 finish_task 补）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (task_id, session_id, task, status, started_at,"
                " log_path) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET session_id = excluded.session_id,"
                " task = excluded.task, status = excluded.status,"
                " log_path = excluded.log_path",
                (task_id, session_id, task, status, self._stamp(), log_path),
            )
            self._conn.commit()

    def finish_task(self, task_id: str, status: str) -> None:
        """记录任务终态（含 failed / partial；中断的任务留在 running 由 UI 判定）。"""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = ?, finished_at = ? WHERE task_id = ?",
                (status, self._stamp(), task_id),
            )
            self._conn.commit()

    def list_tasks(self, session_id: str) -> list[dict]:
        """某会话的任务索引（时间序）：刷新后据此回放轨迹。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id, session_id, task, status, started_at, finished_at,"
                " log_path FROM tasks WHERE session_id = ? ORDER BY started_at, rowid",
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_task(self, task_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, session_id, task, status, started_at, finished_at,"
                " log_path FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def close(self) -> None:
        self._heartbeat_stop.set()
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
