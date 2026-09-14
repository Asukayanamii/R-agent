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

    def stream(
        self, thread_id: str, message: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
        """
        处理一条用户消息，产出 text_delta / tool_start / tool_end / interrupt / message_end。

        workspace 是本次运行的信任边界，工具据此判定越界；省略则退回应用所在目录。
        thread / error / done 属于控制面事件，由 SSE 层统一收口，实现类不要产出。
        """
        ...

    def resume(
        self, thread_id: str, value: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
        """
        从 interrupt 处继续执行，产出后续的数据面事件。

        没有待确认操作时应产出一个 error 事件，而不是静默结束。
        """
        ...

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        """读取既有会话的消息，供前端恢复。不存在的会话返回空列表。"""
        ...

    async def delete_thread(self, thread_id: str) -> None:
        """
        删除该会话在存储里的全部状态，不可恢复。

        只删索引行是不够的：checkpointer 里还留着检查点，下一次索引重建会把会话
        重新枚举回来。所以索引层的删除必须配合这个。不存在的会话静默通过。
        """
        ...

    async def is_interrupted(self, thread_id: str) -> bool:
        """该会话是否停在待人工确认处。"""
        ...

    async def pending_interrupts(self, thread_id: str) -> list[InterruptData]:
        """
        取出该会话当前挂着的待确认项。

        用于在用户绕过确认、直接发新消息时把确认卡片重新推给前端——
        用户面对的是界面，没法自己去调 /chat/resume。
        """
        ...

    async def has_dangling_tool_calls(self, thread_id: str) -> bool:
        """
        该会话的历史里是否有"没人回应的 tool_calls"。

        这是 provider 拒绝请求的直接原因：
        "An assistant message with 'tool_calls' must be followed by tool messages
        responding to each 'tool_call_id'."
        会话停在 interrupt 上时就是这样；此时若发新消息，interrupt 会被丢弃，
        这条悬空调用就永久留在历史里。
        """
        ...

    async def repair_after_abort(self, thread_id: str) -> int:
        """
        一轮被中断（用户点了停止、连接断了、进程被关）之后，把状态修回"可继续"。

        中断可能停在"模型已经要求调工具、工具还没回结果"的位置，那种悬空调用对 provider
        而言是非法历史，下一次发消息会被拒。这里给缺结果的调用补一条"已中断"的回复，
        返回补了几条。

        **停在待确认（interrupt）上的会话不能动**——那是合法状态，用户点确认就能继续。
        """
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
        self._pending: dict[str, InterruptData] = {}
        self._history: dict[str, list[HistoryMessage]] = {}

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        return list(self._history.get(thread_id, []))

    async def delete_thread(self, thread_id: str) -> None:
        self._pending.pop(thread_id, None)
        self._history.pop(thread_id, None)

    async def is_interrupted(self, thread_id: str) -> bool:
        return thread_id in self._pending

    async def pending_interrupts(self, thread_id: str) -> list[InterruptData]:
        pending = self._pending.get(thread_id)
        return [pending] if pending is not None else []

    async def has_dangling_tool_calls(self, thread_id: str) -> bool:
        return False

    async def repair_after_abort(self, thread_id: str) -> int:
        """内存实现没有"悬空调用"这回事：中断即忘。"""
        return 0

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

    async def stream(
        self, thread_id: str, message: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
        self._record(thread_id, "user", message)

        for text in ("收到：", message, "\n\n"):
            yield TextDeltaEvent(data=TextDeltaData(text=text))
            await asyncio.sleep(self.delay)

        if message.startswith("/hitl"):
            data = InterruptData(
                id=uuid4().hex[:8],
                prompt=f"确认要执行「{message}」吗？",
                options=["确认", "取消"],
            )
            self._pending[thread_id] = data
            yield InterruptEvent(data=data)
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

    async def resume(
        self, thread_id: str, value: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
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
