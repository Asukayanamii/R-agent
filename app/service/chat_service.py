"""
业务层：编排 Agent 执行与会话索引维护。

自己不碰 SQL（dao 的职责），也不碰图与 checkpointer（agent 的职责），
只负责"一轮对话之后要记录什么"这类业务规则。
"""

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

from pydantic import BaseModel

from app.agent.runner import AgentRunner
from app.config import PROJECT_ROOT
from app.dao.thread_index_dao import ThreadIndexDao
from app.exceptions import InvalidInput
from app.event.events import (
    ErrorData,
    ErrorEvent,
    HistoryMessage,
    InterruptData,
    InterruptEvent,
    MessageEndData,
    MessageEndEvent,
    ThreadSummary,
)
from app.models.entities import ThreadRecord


class ChatService:
    def __init__(self, runner: AgentRunner, index: ThreadIndexDao) -> None:
        self._runner = runner
        self._index = index

    async def _stuck_reason(self, thread_id: str) -> str | None:
        """会话是否卡着（用于索引里的 pending 标记）。"""
        if await self._runner.is_interrupted(thread_id):
            return "待确认"
        if await self._runner.has_dangling_tool_calls(thread_id):
            return "有悬空工具调用"
        return None

    async def stream(self, thread_id: str, message: str) -> AsyncIterator[BaseModel]:
        pending = await self._runner.pending_interrupts(thread_id)
        if pending:
            # 用户绕过了确认、直接又发了一条消息。这里不能只回一句"请先调用
            # /chat/resume"——他面对的是界面，没法自己构造请求，只会卡住。
            # 把待确认项重新推回去，前端会再渲染一张卡片，点一下即可。
            for item in pending:
                yield InterruptEvent(
                    data=item.model_copy(
                        update={"prompt": f"上一条消息未处理，请先确认：{item.prompt}"}
                    )
                )
            yield MessageEndEvent(data=MessageEndData(message_id=uuid4().hex[:8]))
            return

        if await self._runner.has_dangling_tool_calls(thread_id):
            yield ErrorEvent(
                data=ErrorData(
                    message="该会话有未完成的工具调用，无法继续，请点击「新对话」开始"
                )
            )
            return

        async for event in self._runner.stream(
            thread_id=thread_id,
            message=message,
            workspace=await self._workspace(thread_id),
        ):
            yield event
        await self._record(thread_id, title=message)

    async def resume(self, thread_id: str, value: str) -> AsyncIterator[BaseModel]:
        async for event in self._runner.resume(
            thread_id=thread_id,
            value=value,
            workspace=await self._workspace(thread_id),
        ):
            yield event
        await self._record(thread_id, title="")

    async def _workspace(self, thread_id: str) -> str | None:
        """该会话的工作区。没设过则返回 None，由沙箱退回应用所在目录。"""
        return await self._index.get_workspace(thread_id) or None

    def browse(self, path: str) -> dict:
        """
        列出目录，供前端挑选工作区。

        **这一步刻意不受沙箱约束**：沙箱限制的是 agent，不是用户。
        用户本来就能在自己机器上任意选目录，拦它没有意义。

        代价是它成为一个可以列举任意目录的接口。本地单用户部署没问题，
        但要是把 API 暴露出去，这个接口必须先加鉴权或去掉。
        """
        if not path.strip():
            return {"path": "", "parent": None, "dirs": self._start_points()}

        target = Path(path.strip()).expanduser()
        try:
            target = target.resolve()
        except OSError as exc:
            raise InvalidInput(f"无法解析路径：{exc}") from exc

        if not target.is_dir():
            raise InvalidInput(f"不是目录：{target.as_posix()}")

        entries = []
        try:
            for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if item.is_dir():
                        entries.append({"name": item.name, "path": item.as_posix()})
                except OSError:
                    continue
        except PermissionError as exc:
            raise InvalidInput(f"没有权限读取：{target.as_posix()}") from exc

        parent = target.parent
        return {
            "path": target.as_posix(),
            "parent": None if parent == target else parent.as_posix(),
            "dirs": entries,
        }

    @staticmethod
    def _start_points() -> list[dict]:
        """没给路径时给出的起点：用户目录 + 各盘符（Windows）。"""
        points = [{"name": f"~ {Path.home().name}", "path": Path.home().as_posix()}]
        if sys.platform == "win32":
            for letter in "CDEFGH":
                drive = Path(f"{letter}:/")
                if drive.exists():
                    points.append({"name": drive.as_posix(), "path": drive.as_posix()})
        else:
            points.append({"name": "/", "path": "/"})
        return points

    async def set_workspace(self, thread_id: str, path: str) -> str:
        """
        设置会话的工作区。

        工作区就是沙箱的信任边界，**只能由用户指定**——agent 若能改自己的边界，
        边界就不存在了。所以这个方法只应该被用户触发的接口调用。
        """
        target = Path(path.strip() or ".").expanduser()
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target = target.resolve()

        if not target.exists():
            raise InvalidInput(f"路径不存在：{target.as_posix()}")
        if not target.is_dir():
            raise InvalidInput(f"不是目录：{target.as_posix()}")

        await self._index.set_workspace(thread_id, target.as_posix())
        return target.as_posix()

    async def _record(self, thread_id: str, title: str) -> None:
        """
        一轮对话结束后刷新索引。

        pending 取"是否还卡着"：正常中断时它等同于 is_interrupted，但也能覆盖
        "中断态被新消息破坏、留下悬空 tool_calls"的坏死会话——那种会话同样需要
        用户注意，只是不能再 resume，只能新建。
        """
        await self._index.upsert(
            ThreadRecord(
                thread_id=thread_id,
                title=title,
                updated_at=datetime.now(timezone.utc).isoformat(),
                pending=await self._stuck_reason(thread_id) is not None,
            )
        )

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        return await self._runner.history(thread_id)

    async def pending_interrupts(self, thread_id: str) -> list[InterruptData]:
        """
        该会话挂着的待确认项。

        前端打开历史会话时要拿到它才能渲染确认卡片——否则一个卡住的会话
        虽然侧边栏有红点，用户进去却无处可点。
        """
        return await self._runner.pending_interrupts(thread_id)

    async def threads(self, limit: int = 50) -> list[ThreadSummary]:
        records = await self._index.list(limit)
        return [
            ThreadSummary(
                thread_id=record.thread_id,
                title=record.title or record.thread_id[:12],
                updated_at=record.updated_at,
                pending=record.pending,
                workspace=record.workspace,
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
