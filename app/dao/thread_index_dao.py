"""
数据访问层：thread_index 表。

这一层只做表的读写，不含任何业务规则（时间戳怎么取、pending 怎么判定都不在这里），
也不 import 上层的 DTO——它只认 app.models.entities 里的领域实体。
"""

import sqlite3

import aiosqlite

from app.models.entities import ThreadRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_index (
    thread_id  TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    pending    INTEGER NOT NULL DEFAULT 0,
    workspace  TEXT NOT NULL DEFAULT ''
)
"""

MIGRATIONS = ("ALTER TABLE thread_index ADD COLUMN workspace TEXT NOT NULL DEFAULT ''",)


class ThreadIndexDao:
    """会话索引表的数据访问对象。"""

    def __init__(self, path: str) -> None:
        self.path = path or ":memory:"
        self._db: aiosqlite.Connection | None = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "ThreadIndexDao 尚未 open()"
        return self._db

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute(SCHEMA)
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """
        给已存在的表补列。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，所以新增字段必须
        显式 ALTER。重复执行会报 duplicate column，那是预期内的，忽略掉。
        """
        for statement in MIGRATIONS:
            try:
                await self._db.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def upsert(self, record: ThreadRecord) -> None:
        """标题与工作区一旦有值就不再用空串覆盖，避免无关字段被清掉。"""
        await self._conn.execute(
            """
            INSERT INTO thread_index (thread_id, title, updated_at, pending, workspace)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                title      = CASE WHEN excluded.title <> '' THEN excluded.title ELSE title END,
                updated_at = excluded.updated_at,
                pending    = excluded.pending,
                workspace  = CASE WHEN excluded.workspace <> '' THEN excluded.workspace ELSE workspace END
            """,
            (
                record.thread_id,
                record.title,
                record.updated_at,
                int(record.pending),
                record.workspace,
            ),
        )
        await self._conn.commit()

    async def set_workspace(self, thread_id: str, workspace: str) -> None:
        await self._conn.execute(
            """
            INSERT INTO thread_index (thread_id, title, updated_at, pending, workspace)
            VALUES (?, '', '', 0, ?)
            ON CONFLICT(thread_id) DO UPDATE SET workspace = excluded.workspace
            """,
            (thread_id, workspace),
        )
        await self._conn.commit()

    async def get_workspace(self, thread_id: str) -> str:
        cursor = await self._conn.execute(
            "SELECT workspace FROM thread_index WHERE thread_id = ?", (thread_id,)
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else ""

    async def list(self, limit: int = 50) -> list[ThreadRecord]:
        cursor = await self._conn.execute(
            "SELECT thread_id, title, updated_at, pending, workspace FROM thread_index "
            "ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            ThreadRecord(
                thread_id=row[0],
                title=row[1],
                updated_at=row[2],
                pending=bool(row[3]),
                workspace=row[4],
            )
            for row in rows
        ]

    async def count(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) FROM thread_index")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row else 0
