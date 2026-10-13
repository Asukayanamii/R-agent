"""
数据访问层：graph_version 表——图版本的审计账。

每次版本切换留一行：结构哈希、参与片段、挂载链、片段清单（`spec`）与 mermaid 存档，
外加一个 `reason`（启动 / 结构变化 / 熔断）。**组装那一行是业务层的事**，这里只管读写
与 JSON 列的打包解包。

**只增不删**：审计的价值全在历史里。一行约 1~2KB，要清理自己动手。
"""

import json
import logging

import aiosqlite

from app.common.models.entities import GraphVersionRecord

logger = logging.getLogger(__name__)

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS graph_version (
        scope          TEXT NOT NULL DEFAULT '',
        version        INTEGER NOT NULL,
        structure_hash TEXT NOT NULL,
        reason         TEXT NOT NULL DEFAULT '',
        base           TEXT NOT NULL DEFAULT '',
        protocol       TEXT,
        participants   TEXT NOT NULL DEFAULT '[]',
        mounts         TEXT NOT NULL DEFAULT '[]',
        spec           TEXT NOT NULL DEFAULT '{}',
        mermaid        TEXT NOT NULL DEFAULT '',
        warnings       TEXT NOT NULL DEFAULT '[]',
        created_at     TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (scope, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS graph_version_created ON graph_version (created_at)",
)

_COLUMNS = (
    "scope, version, structure_hash, reason, base, protocol,"
    " participants, mounts, spec, mermaid, warnings, created_at"
)


class GraphVersionDao:
    """图版本审计表的数据访问对象。"""

    def __init__(self, path: str) -> None:
        self.path = path or ":memory:"
        self._db: aiosqlite.Connection | None = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "GraphVersionDao 尚未 open()"
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

    async def record(self, item: GraphVersionRecord) -> None:
        """同 (scope, version) 重复写入按覆盖处理：重合成总是产生新 id，覆盖只用于补写。"""
        await self._conn.execute(
            f"INSERT OR REPLACE INTO graph_version ({_COLUMNS})"  # noqa: S608 - 列名是模块常量
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.scope,
                item.version,
                item.structure_hash,
                item.reason,
                item.base,
                item.protocol,
                json.dumps(list(item.participants), ensure_ascii=False),
                json.dumps(list(item.mounts), ensure_ascii=False),
                json.dumps(item.spec, ensure_ascii=False),
                item.mermaid,
                json.dumps(list(item.warnings), ensure_ascii=False),
                item.created_at,
            ),
        )
        await self._conn.commit()
        logger.debug(
            "审计写入 scope=%s v%d %s（%s）",
            item.scope or '全局',
            item.version,
            item.structure_hash[:12],
            item.reason,
        )

    async def recent(self, scope: str = "", limit: int = 20) -> list[dict]:
        """最近几次切换（新版本在前），JSON 列解回结构。"""
        cursor = await self._conn.execute(
            f"SELECT {_COLUMNS} FROM graph_version WHERE scope = ?"  # noqa: S608
            " ORDER BY version DESC LIMIT ?",
            (scope, limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [
            {
                'scope': row[0],
                'version': row[1],
                'structure_hash': row[2],
                'reason': row[3],
                'base': row[4],
                'protocol': row[5],
                'participants': json.loads(row[6] or '[]'),
                'mounts': json.loads(row[7] or '[]'),
                'spec': json.loads(row[8] or '{}'),
                'mermaid': row[9],
                'warnings': json.loads(row[10] or '[]'),
                'created_at': row[11],
            }
            for row in rows
        ]
