"""
Agent 执行器接口。

本层只负责"跑一次图、产出事件"，以及读取图自身的状态。
会话索引、列表拼装等属于业务层与数据访问层，不在这里。
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel

from app.event.events import (
    ErrorData,
    ErrorEvent,
    HistoryMessage,
    InterruptData,
    InterruptEvent,
    MessageEndData,
    MessageEndEvent,
    TextDeltaData,
    TextDeltaEvent,
    ToolEndData,
    ToolEndEvent,
    ToolStartData,
    ToolStartEvent,
    Usage,
)
from app.models.entities import ThreadRecord


class AgentRunner(Protocol):
    """Agent 执行器：把一次用户输入变成一串数据面事件。"""

    def stream(self, thread_id: str, message: str) -> AsyncIterator[BaseModel]:
        """
        处理一条用户消息，产出 text_delta / tool_start / tool_end / interrupt / message_end。

        thread / error / done 属于控制面事件，由 SSE 层统一收口，实现类不要产出。
        """
        ...

    def resume(self, thread_id: str, value: str) -> AsyncIterator[BaseModel]:
        """
        从 interrupt 处继续执行，产出后续的数据面事件。

        没有待确认操作时应产出一个 error 事件，而不是静默结束。
        """
        ...

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        """读取既有会话的消息，供前端恢复。不存在的会话返回空列表。"""
        ...

    async def is_interrupted(self, thread_id: str) -> bool:
        """该会话是否停在待人工确认处。业务层据此维护索引里的 pending 标记。"""
        ...

    async def scan_threads(self, limit: int = 200) -> list[ThreadRecord]:
        """
        从存储里枚举会话快照，供业务层回填索引。

        这是慢路径（每个会话要单独查一次状态），只应在索引重建时调用。
        """
        ...


class StubRunner:
    """
    开发期替身：不依赖 LangGraph 与大模型，用于先行验证事件协议与前端渲染。

    消息以 /hitl 开头时会停在 interrupt 上，等待 resume，行为与真实实现一致。
    """

    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self._pending: dict[str, str] = {}
        self._history: dict[str, list[HistoryMessage]] = {}

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        return list(self._history.get(thread_id, []))

    async def is_interrupted(self, thread_id: str) -> bool:
        return thread_id in self._pending

    async def scan_threads(self, limit: int = 200) -> list[ThreadRecord]:
        items = list(self._history.items())[-limit:]
        return [
            ThreadRecord(
                thread_id=thread_id,
                title=messages[0].content if messages else "",
                pending=thread_id in self._pending,
            )
            for thread_id, messages in reversed(items)
        ]

    def _record(self, thread_id: str, role: str, content: str) -> None:
        self._history.setdefault(thread_id, []).append(
            HistoryMessage(role=role, content=content)
        )

    async def stream(self, thread_id: str, message: str) -> AsyncIterator[BaseModel]:
        self._record(thread_id, "user", message)

        for text in ("收到：", message, "\n\n"):
            yield TextDeltaEvent(data=TextDeltaData(text=text))
            await asyncio.sleep(self.delay)

        if message.startswith("/hitl"):
            prompt = f"确认要执行「{message}」吗？"
            self._pending[thread_id] = prompt
            yield InterruptEvent(
                data=InterruptData(
                    id=uuid4().hex[:8], prompt=prompt, options=["确认", "取消"]
                )
            )
            yield MessageEndEvent(data=MessageEndData(message_id=uuid4().hex[:8]))
            return

        call_id = uuid4().hex[:8]
        yield ToolStartEvent(
            data=ToolStartData(id=call_id, name="echo", args={"text": message})
        )
        await asyncio.sleep(self.delay)
        yield ToolEndEvent(data=ToolEndData(id=call_id, ok=True, result=message))

        for text in ("echo 工具已返回。", f"当前会话：{thread_id}"):
            yield TextDeltaEvent(data=TextDeltaData(text=text))
            await asyncio.sleep(self.delay)

        yield MessageEndEvent(
            data=MessageEndData(
                message_id=uuid4().hex[:8],
                usage=Usage(input_tokens=len(message), output_tokens=len(message)),
            )
        )
        self._record(thread_id, "assistant", f"echo 工具已返回：{message}")

    async def resume(self, thread_id: str, value: str) -> AsyncIterator[BaseModel]:
        if self._pending.pop(thread_id, None) is None:
            yield ErrorEvent(
                data=ErrorData(message="该会话没有待确认的操作，无需 resume")
            )
            return

        for text in (f"已收到选择：{value}。", "\n\n"):
            yield TextDeltaEvent(data=TextDeltaData(text=text))
            await asyncio.sleep(self.delay)

        yield MessageEndEvent(data=MessageEndData(message_id=uuid4().hex[:8]))
        self._record(thread_id, "assistant", f"已收到选择：{value}")
