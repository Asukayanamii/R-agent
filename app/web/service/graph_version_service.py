"""业务层：图版本审计——把一次版本切换组装成一行。

回答四个问题：**改前/改后**是什么（结构哈希 + 片段清单 + 挂载链）、**为什么**（reason）、
**长什么样**（mermaid 存档）、**什么时候**（created_at）。

写在合成路径上（`HarnessRuntime` 每次合成成功调一次）。所以这一层只做转换、不做可能失败的
业务判断：真出错也由调用方收成一条 WARNING，**审计不许带走装配**。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.common.models.entities import GraphVersionRecord
from app.harness.composer import render_mermaid
from app.web.dao.graph_version_dao import GraphVersionDao


class GraphVersionService:
    """图版本审计：`HarnessRuntime` 的 `on_version` 回调指向 `record`。"""

    def __init__(self, dao: GraphVersionDao) -> None:
        self.dao = dao

    async def record(self, version: Any, report: Any, reason: str) -> None:
        await self.dao.record(
            GraphVersionRecord(
                scope=getattr(report, 'scope', '') or '',
                version=version.id,
                structure_hash=version.structure_hash,
                reason=reason,
                base=report.base,
                protocol=report.protocol,
                participants=tuple(report.participants),
                mounts=tuple(report.to_dict()['mounts']),
                spec=version.spec,
                mermaid=render_mermaid(version.graph),
                warnings=tuple(report.warnings),
                created_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
            )
        )

    async def recent(self, scope: str = '', limit: int = 20) -> list[dict]:
        """最近几次切换（新版本在前）——`inspect_graph` 与排查用。"""
        return await self.dao.recent(scope, limit)
