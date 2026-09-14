"""
数据访问层：workspace 与 thread_workspace 两张表。

工作区从"会话行上的一段字符串"升格成实体后就落在这里：路径怎么归一、怎么判重、
会话与工作区怎么绑定，都是这一层的读写细节。不含业务规则（时间戳由上层传进来），
也不 import 上层 DTO——只认 app.models.entities 里的领域实体。

两张表放在同一个 DAO 里：它们只在"建立一次绑定"和"读一次归属"时被一起用，
拆成两个类只会让同一个动作跨两个对象。
"""

import os
from collections.abc import Sequence
from pathlib import Path

import aiosqlite

from app.models.entities import WorkspaceRecord

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS workspace (
        path_key   TEXT PRIMARY KEY,
        path       TEXT NOT NULL,
        name       TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS thread_workspace (
        thread_id  TEXT PRIMARY KEY,
        path_key   TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT ''
    )
    """,
)


def _path_key(path: str) -> str:
    """
    归一化键：只用来判重，不拿来展示。

    Windows 下同一个目录有无数种写法（大小写、正反斜杠），不折叠就会裂成多个工作区；
    展示始终用 path，保留用户机器上的原始写法。POSIX 上 normcase 是恒等变换。
    """
    return os.path.normcase(path).replace("\\", "/")


def _name_of(path: str) -> str:
    """展示名，默认取目录名；盘符根这类取不到名字的退回整条路径。"""
    return Path(path).name or path


class WorkspaceDao:
    """工作区表与会话绑定表的数据访问对象。"""

    def __init__(self, path: str) -> None:
        self.path = path or ":memory:"
        self._db: aiosqlite.Connection | None = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "WorkspaceDao 尚未 open()"
        return self._db

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        await self._db.execute("PRAGMA busy_timeout = 5000")
        for statement in SCHEMA:
            await self._db.execute(statement)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def ensure(self, path: str, created_at: str) -> WorkspaceRecord:
        """
        取或建一个工作区，返回库里的那一行。

        大小写变体会命中同一个 path_key，所以同一目录只会有一行，路径以首次登记的写法为准。
        """
        canonical = Path(path).as_posix()
        key = _path_key(canonical)
        await self._conn.execute(
            "INSERT OR IGNORE INTO workspace (path_key, path, name, created_at) "
            "VALUES (?, ?, ?, ?)",
            (key, canonical, _name_of(canonical), created_at),
        )
        await self._conn.commit()

        cursor = await self._conn.execute(
            "SELECT path_key, path, name, created_at FROM workspace WHERE path_key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        assert row is not None, f"工作区刚写入却查不到：{key}"
        return WorkspaceRecord(
            path=row[1], path_key=row[0], name=row[2], created_at=row[3]
        )

    async def bind(self, thread_id: str, path: str, updated_at: str) -> str:
        """把会话绑到工作区，返回库里的规范路径。重复绑定以最后一次为准。"""
        record = await self.ensure(path, updated_at)
        await self._conn.execute(
            """
            INSERT INTO thread_workspace (thread_id, path_key, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                path_key   = excluded.path_key,
                updated_at = excluded.updated_at
            """,
            (thread_id, record.path_key, updated_at),
        )
        await self._conn.commit()
        return record.path

    async def bind_if_absent(self, thread_id: str, path: str, updated_at: str) -> None:
        """只在没绑过时写——迁移历史数据用，绝不覆盖已有归属。"""
        record = await self.ensure(path, updated_at)
        await self._conn.execute(
            "INSERT OR IGNORE INTO thread_workspace (thread_id, path_key, updated_at) "
            "VALUES (?, ?, ?)",
            (thread_id, record.path_key, updated_at),
        )
        await self._conn.commit()

    async def bind_many(
        self, thread_ids: Sequence[str], path: str, updated_at: str
    ) -> None:
        """把一批还没归属的会话绑到同一个工作区。已绑好的不动（迁移回填用）。"""
        if not thread_ids:
            return
        record = await self.ensure(path, updated_at)
        await self._conn.executemany(
            "INSERT OR IGNORE INTO thread_workspace (thread_id, path_key, updated_at) "
            "VALUES (?, ?, ?)",
            [(thread_id, record.path_key, updated_at) for thread_id in thread_ids],
        )
        await self._conn.commit()

    async def unbind(self, thread_id: str) -> None:
        """删掉会话的绑定。不存在的静默通过——DELETE 应当是幂等的。"""
        await self._conn.execute(
            "DELETE FROM thread_workspace WHERE thread_id = ?", (thread_id,)
        )
        await self._conn.commit()

    async def unbind_many(self, thread_ids: Sequence[str]) -> int:
        """批量删除绑定行，返回删掉的行数。"""
        if not thread_ids:
            return 0
        marks = ",".join("?" * len(thread_ids))
        cursor = await self._conn.execute(
            f"DELETE FROM thread_workspace WHERE thread_id IN ({marks})",
            tuple(thread_ids),
        )
        await self._conn.commit()
        return cursor.rowcount

    async def for_path(self, path: str) -> WorkspaceRecord | None:
        """按路径查工作区（比的是归一化键）。没登记过返回 None。"""
        cursor = await self._conn.execute(
            "SELECT path_key, path, name, created_at FROM workspace WHERE path_key = ?",
            (_path_key(Path(path).as_posix()),),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row is None:
            return None
        return WorkspaceRecord(
            path=row[1], path_key=row[0], name=row[2], created_at=row[3]
        )

    async def for_thread(self, thread_id: str) -> str:
        """会话绑定的工作区（规范路径）；没绑过返回空串。"""
        cursor = await self._conn.execute(
            "SELECT w.path FROM thread_workspace t JOIN workspace w "
            "ON w.path_key = t.path_key WHERE t.thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        return row[0] if row else ""

    async def paths_for(self, thread_ids: Sequence[str]) -> dict[str, WorkspaceRecord]:
        """批量取多个会话的工作区，供会话列表一次拼装（避免 N+1）。"""
        if not thread_ids:
            return {}
        marks = ",".join("?" * len(thread_ids))
        cursor = await self._conn.execute(
            "SELECT t.thread_id, w.path_key, w.path, w.name, w.created_at "
            "FROM thread_workspace t JOIN workspace w ON w.path_key = t.path_key "
            f"WHERE t.thread_id IN ({marks})",
            tuple(thread_ids),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            row[0]: WorkspaceRecord(
                path=row[2], path_key=row[1], name=row[3], created_at=row[4]
            )
            for row in rows
        }
