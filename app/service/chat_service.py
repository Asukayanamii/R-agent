"""
业务层：编排 Agent 执行与会话索引维护。

自己不碰 SQL（dao 的职责），也不碰图与 checkpointer（agent 的职责），
只负责"一轮对话之后要记录什么"这类业务规则。
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone

from pydantic import BaseModel

from app.agent.runner import AgentRunner
from app.dao.thread_index_dao import ThreadIndexDao
from app.event.events import HistoryMessage, ThreadSummary
from app.models.entities import ThreadRecord


class ChatService:
    def __init__(self, runner: AgentRunner, index: ThreadIndexDao) -> None:
        self._runner = runner
        self._index = index

    async def stream(self, thread_id: str, message: str) -> AsyncIterator[BaseModel]:
        async for event in self._runner.stream(thread_id=thread_id, message=message):
            yield event
        await self._record(thread_id, title=message)

    async def resume(self, thread_id: str, value: str) -> AsyncIterator[BaseModel]:
        async for event in self._runner.resume(thread_id=thread_id, value=value):
            yield event
        await self._record(thread_id, title="")

    async def _record(self, thread_id: str, title: str) -> None:
        """
        一轮对话结束后刷新索引。

        pending 直接问 agent 要，而不是从事件流里推断是否出现过 interrupt——
        后者在"会话已中断却发来新消息"这类路径上会得出错误结论。
        """
        await self._index.upsert(
            ThreadRecord(
                thread_id=thread_id,
                title=title,
                updated_at=datetime.now(timezone.utc).isoformat(),
                pending=await self._runner.is_interrupted(thread_id),
            )
        )

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        return await self._runner.history(thread_id)

    async def threads(self, limit: int = 50) -> list[ThreadSummary]:
        records = await self._index.list(limit)
        return [
            ThreadSummary(
                thread_id=record.thread_id,
                title=record.title or record.thread_id[:12],
                updated_at=record.updated_at,
                pending=record.pending,
            )
            for record in records
        ]

    async def ensure_index(self) -> int:
        """索引为空而存储里有会话时回填一次，用于首次启用索引或索引被删。"""
        if await self._index.count() > 0:
            return 0
        records = await self._runner.scan_threads()
        for record in records:
            await self._index.upsert(record)
        return len(records)
