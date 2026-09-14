"""
数据访问层：thread_index 表。

这一层只做表的读写，不含任何业务规则（时间戳怎么取、pending 怎么判定都不在这里），
也不 import 上层的 DTO——它只认 app.models.entities 里的领域实体。

工作区已迁出本表，见 workspace_dao；这里只留了"把历史列迁走再删掉"的两个方法。
"""

from collections.abc import Sequence

import aiosqlite

from app.models.entities import ThreadRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS thread_index (
    thread_id  TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    pending    INTEGER NOT NULL DEFAULT 0
)
"""


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
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def upsert(self, record: ThreadRecord) -> None:
        """标题一旦有值就不再用空串覆盖，避免无关字段被清掉。"""
        await self._conn.execute(
            """
            INSERT INTO thread_index (thread_id, title, updated_at, pending)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                title      = CASE WHEN excluded.title <> '' THEN excluded.title ELSE title END,
                updated_at = excluded.updated_at,
                pending    = excluded.pending
            """,
            (
                record.thread_id,
                record.title,
                record.updated_at,
                int(record.pending),
            ),
        )
        await self._conn.commit()

    async def list_threads(self, limit: int = 50) -> list[ThreadRecord]:
        cursor = await self._conn.execute(
            "SELECT thread_id, title, updated_at, pending FROM thread_index "
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
            )
            for row in rows
        ]

    async def delete(self, thread_id: str) -> None:
        """删除索引行。不存在的会话静默通过——DELETE 应当是幂等的。"""
        await self._conn.execute(
            "DELETE FROM thread_index WHERE thread_id = ?", (thread_id,)
        )
        await self._conn.commit()

    async def delete_many(self, thread_ids: Sequence[str]) -> int:
        """批量删除索引行，返回删掉的行数。"""
        if not thread_ids:
            return 0
        marks = ",".join("?" * len(thread_ids))
        cursor = await self._conn.execute(
            f"DELETE FROM thread_index WHERE thread_id IN ({marks})",
            tuple(thread_ids),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def count(self) -> int:
        cursor = await self._conn.execute("SELECT COUNT(*) FROM thread_index")
        row = await cursor.fetchone()
        await cursor.close()
        return int(row[0]) if row else 0

    async def thread_ids(self) -> list[str]:
        """全部会话 ID，供启动迁移比对归属。"""
        cursor = await self._conn.execute("SELECT thread_id FROM thread_index")
        rows = await cursor.fetchall()
        await cursor.close()
        return [row[0] for row in rows]

    async def rows_with_legacy_workspace(self) -> list[tuple[str, str]]:
        """还留着工作区字符串的历史行，供启动时一次性迁入工作区表。"""
        if not await self._has_legacy_workspace():
            return []
        cursor = await self._conn.execute(
            "SELECT thread_id, workspace FROM thread_index WHERE workspace <> ''"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [(row[0], row[1]) for row in rows]

    async def drop_legacy_workspace(self) -> bool:
        """
        删掉已迁出的工作区列，返回是否真的删了。

        **不能塞进 open() 当普通迁移**：它会跑在迁移动作之前，把还没读出来的归属一起带走。
        什么时候删由业务层定（先迁后删），这里只负责"列在就删，不在就当没事"。
        """
        if not await self._has_legacy_workspace():
            return False
        await self._conn.execute("ALTER TABLE thread_index DROP COLUMN workspace")
        await self._conn.commit()
        return True

    async def _has_legacy_workspace(self) -> bool:
        """老库才有那一列；新建的库、删过一轮的库都没有。"""
        cursor = await self._conn.execute("PRAGMA table_info(thread_index)")
        rows = await cursor.fetchall()
        await cursor.close()
        return any(row[1] == "workspace" for row in rows)

    async def placeholder_ids(self) -> list[str]:
        """
        只被旧版 set_workspace 物化过、从没跑过对话的空行。

        判据是标题与时间都是空串：`_record` 每轮都会带上时间，写不出这种行。
        """
        cursor = await self._conn.execute(
            "SELECT thread_id FROM thread_index WHERE title = '' AND updated_at = ''"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [row[0] for row in rows]
