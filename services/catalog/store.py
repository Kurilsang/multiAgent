"""目录存储：条目缓存 + 源状态 + 平台凭证仓。

搜索**只读缓存**（外网查询只发生在爬取/详情/取包时）；源状态含
last_refresh / last_error / stale（降级读缓存的判定依据）。
SqliteStore 持久化到 CATALOG_DB（凭证同库，日志严禁输出凭证内容）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Protocol

from .schema import AuditBadge, CatalogEntry

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
  source TEXT NOT NULL,
  id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  origin TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'skill',
  installs INTEGER NOT NULL DEFAULT 0,
  stars INTEGER NOT NULL DEFAULT 0,
  tags TEXT NOT NULL DEFAULT '[]',
  detail_url TEXT NOT NULL DEFAULT '',
  install_ref TEXT NOT NULL DEFAULT '',
  audits TEXT NOT NULL DEFAULT '[]',
  validated INTEGER NOT NULL DEFAULT 0,
  is_duplicate INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  status_message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS source_state (
  source TEXT PRIMARY KEY,
  last_refresh TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS creds (
  source TEXT PRIMARY KEY,
  data TEXT NOT NULL
);
"""

_DEFAULT_STATE = {"last_refresh": "", "last_error": "", "entry_count": 0}


class CatalogStore(Protocol):
    def replace_source(
        self, source: str, entries: list[CatalogEntry], refreshed_at: str
    ) -> None: ...

    def record_error(self, source: str, error: str) -> None: ...

    def search(
        self, q: str = "", source: str = "", page: int = 1, page_size: int = 20
    ) -> tuple[list[CatalogEntry], int]: ...

    def sources(self) -> list[dict]: ...

    def refreshed_at(self) -> str: ...


def _state_row(name: str, row: dict, count: int) -> dict:
    return {
        "source": name,
        "last_refresh": row["last_refresh"],
        "last_error": row["last_error"],
        "entry_count": count,
        "stale": bool(row["last_error"]) or not row["last_refresh"],
    }


class MemoryStore:
    """内存存储：按源分组保序（爬取顺序 = 榜单热度顺序）。测试与轻量场景用。"""

    def __init__(self):
        self._entries: dict[str, list[CatalogEntry]] = {}
        self._state: dict[str, dict] = {}
        self._refreshed_at = ""

    def replace_source(
        self, source: str, entries: list[CatalogEntry], refreshed_at: str
    ) -> None:
        self._entries[source] = list(entries)
        self._state[source] = {
            "last_refresh": refreshed_at,
            "last_error": "",
            "entry_count": len(entries),
        }
        self._refreshed_at = refreshed_at

    def record_error(self, source: str, error: str) -> None:
        state = self._state.setdefault(source, dict(_DEFAULT_STATE))
        state["last_error"] = error
        state["entry_count"] = len(self._entries.get(source, ()))

    def search(
        self, q: str = "", source: str = "", page: int = 1, page_size: int = 20
    ) -> tuple[list[CatalogEntry], int]:
        needle = q.strip().casefold()
        hits: list[CatalogEntry] = []
        for name, entries in self._entries.items():
            if source and name != source:
                continue
            for entry in entries:
                if needle and needle not in (
                    entry.name + "\n" + entry.description
                ).casefold():
                    continue
                hits.append(entry)
        start = (page - 1) * page_size
        return hits[start : start + page_size], len(hits)

    def sources(self) -> list[dict]:
        return [
            _state_row(name, self._state[name], self._state[name]["entry_count"])
            for name in sorted(self._state)
        ]

    def refreshed_at(self) -> str:
        return self._refreshed_at


class SqliteStore:
    """SQLite 持久存储：重启后目录仍可用；单连接 + 锁（刷新线程与请求线程互斥）。"""

    def __init__(self, db_path: Path):
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        for column_sql in (
            "ALTER TABLE entries ADD COLUMN kind TEXT NOT NULL DEFAULT 'skill'",
            "ALTER TABLE entries ADD COLUMN status TEXT NOT NULL DEFAULT 'active'",
            "ALTER TABLE entries ADD COLUMN status_message TEXT NOT NULL DEFAULT ''",
        ):
            try:  # 旧库迁移：缺列补齐
                self._conn.execute(column_sql)
            except sqlite3.OperationalError:
                pass
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def replace_source(
        self, source: str, entries: list[CatalogEntry], refreshed_at: str
    ) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM entries WHERE source = ?", (source,))
            self._conn.executemany(
                "INSERT INTO entries (source, id, name, description, origin, kind,"
                " installs, stars, tags, detail_url, install_ref, audits, validated,"
                " is_duplicate, status, status_message) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [self._row(source, entry) for entry in entries],
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO source_state (source, last_refresh, last_error)"
                " VALUES (?, ?, '')",
                (source, refreshed_at),
            )
            self._conn.commit()

    def record_error(self, source: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO source_state (source, last_refresh, last_error)"
                " VALUES (?, COALESCE((SELECT last_refresh FROM source_state WHERE source = ?), ''), ?)",
                (source, source, error),
            )
            self._conn.commit()

    def search(
        self, q: str = "", source: str = "", page: int = 1, page_size: int = 20
    ) -> tuple[list[CatalogEntry], int]:
        where = []
        params: list = []
        if source:
            where.append("source = ?")
            params.append(source)
        if q.strip():
            where.append("(LOWER(name) LIKE ? OR LOWER(description) LIKE ?)")
            needle = f"%{q.strip().casefold()}%"
            params.extend([needle, needle])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM entries{clause}", params
            ).fetchone()[0]
            rows = self._conn.execute(
                f"SELECT source, id, name, description, origin, kind, installs, stars,"
                f" tags, detail_url, install_ref, audits, validated, is_duplicate,"
                f" status, status_message"
                f" FROM entries{clause} ORDER BY rowid LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return [self._entry(row) for row in rows], total

    def sources(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, last_refresh, last_error FROM source_state"
            ).fetchall()
            counts = dict(self._conn.execute(
                "SELECT source, COUNT(*) FROM entries GROUP BY source"
            ).fetchall())
        return [
            _state_row(
                name,
                {"last_refresh": last_refresh, "last_error": last_error},
                counts.get(name, 0),
            )
            for name, last_refresh, last_error in sorted(rows)
        ]

    def refreshed_at(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(last_refresh) FROM source_state"
            ).fetchone()
        return row[0] or ""

    def creds(self, source: str) -> "SqliteCredsStore":
        return SqliteCredsStore(self, source)

    # ---- 行映射 ----

    @staticmethod
    def _row(source: str, entry: CatalogEntry) -> tuple:
        return (
            source,
            entry.id,
            entry.name,
            entry.description,
            entry.origin,
            entry.kind,
            entry.installs,
            entry.stars,
            json.dumps(list(entry.tags), ensure_ascii=False),
            entry.detail_url,
            entry.install_ref,
            json.dumps([badge.to_dict() for badge in entry.audits], ensure_ascii=False),
            1 if entry.validated else 0,
            1 if entry.is_duplicate else 0,
            entry.status,
            entry.status_message,
        )

    @staticmethod
    def _entry(row: tuple) -> CatalogEntry:
        (
            source,
            entry_id,
            name,
            description,
            origin,
            kind,
            installs,
            stars,
            tags,
            detail_url,
            install_ref,
            audits,
            validated,
            is_duplicate,
            status,
            status_message,
        ) = row
        return CatalogEntry(
            id=entry_id,
            name=name,
            description=description,
            source=source,
            origin=origin,
            kind=kind,
            installs=installs,
            stars=stars,
            tags=tuple(json.loads(tags)),
            detail_url=detail_url,
            install_ref=install_ref,
            audits=tuple(AuditBadge.from_raw(item) for item in json.loads(audits)),
            validated=bool(validated),
            is_duplicate=bool(is_duplicate),
            status=status,
            status_message=status_message,
        )


class SqliteCredsStore:
    """单源平台凭证仓（dict 风格 get/update，供适配器注入）。

    ⚠ 凭证是密钥：只进 SQLite，日志/报错严禁输出内容。
    """

    def __init__(self, store: SqliteStore, source: str):
        self._store = store
        self._source = source

    def get(self, key: str, default=None):
        return self._data().get(key, default)

    def update(self, mapping: dict) -> None:
        data = self._data()
        data.update({k: str(v) for k, v in mapping.items()})
        with self._store._lock:
            self._store._conn.execute(
                "INSERT OR REPLACE INTO creds (source, data) VALUES (?, ?)",
                (self._source, json.dumps(data, ensure_ascii=False)),
            )
            self._store._conn.commit()

    def _data(self) -> dict:
        with self._store._lock:
            row = self._store._conn.execute(
                "SELECT data FROM creds WHERE source = ?", (self._source,)
            ).fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row[0])
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
