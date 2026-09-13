from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.container import get_service
from app.event.events import (
    HistoryMessage,
    InterruptData,
    ThreadData,
    ThreadEvent,
    ThreadSummary,
)
from app.event.stream import SSE_HEADERS, SSE_MEDIA_TYPE, sse_stream
from app.result.result import Result

router = APIRouter(prefix="/chat", tags=["chat"])

EVENT_DOC = """
每帧 data 形如 {"type": ..., "data": {...}}。

- thread：会话握手，携带 thread_id，必为第一帧
- text_delta：增量文本，累加即得完整回复
- tool_start / tool_end：工具调用开始与结束，通过 id 配对
- interrupt：需要人工确认，前端渲染确认 UI 后调 POST /chat/resume
- message_end：本条消息结束，携带 message_id 与 usage
- error：出错，code 与 Result 语义一致
- done：流结束，必为最后一帧

未知 type 请静默忽略，以便后端新增事件时旧前端不受影响。
"""

STREAM_DOC = f"""
发起一轮对话。{EVENT_DOC}
"""

RESUME_DOC = f"""
带着用户的选择，从上次中断处继续执行。

`thread_id` 与 value 均无待确认操作时返回 error 事件。{EVENT_DOC}
"""


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户输入")
    thread_id: str | None = Field(
        None, description="会话 ID，不传则新建并通过首帧 thread 事件返回"
    )


class ResumeRequest(BaseModel):
    thread_id: str = Field(..., description="会话 ID，来自 interrupt 所在的同一次会话")
    value: str = Field(..., min_length=1, description="用户对 interrupt 的选择")


class HistoryResponse(BaseModel):
    thread_id: str
    messages: list[HistoryMessage]
    pending: list[InterruptData] = Field(
        default_factory=list,
        description="待确认项。前端据此在历史末尾补渲染确认卡片，否则卡住的会话进去无处可点",
    )


class ThreadListResponse(BaseModel):
    threads: list[ThreadSummary]


def _sse(thread_id: str, events: AsyncIterator[BaseModel]) -> StreamingResponse:
    """两种入口共用同一套流外壳：先发 thread 握手，再透传数据面事件。"""

    async def frames() -> AsyncIterator[BaseModel]:
        yield ThreadEvent(data=ThreadData(thread_id=thread_id))
        async for event in events:
            yield event

    return StreamingResponse(
        sse_stream(frames()), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post(
    "/stream",
    summary="流式对话（SSE）",
    description=STREAM_DOC,
    responses={200: {"content": {SSE_MEDIA_TYPE: {}}, "description": "SSE 事件流"}},
)
async def chat_stream(payload: ChatRequest) -> StreamingResponse:
    thread_id = payload.thread_id or uuid4().hex
    return _sse(
        thread_id,
        get_service().stream(thread_id=thread_id, message=payload.message),
    )


@router.post(
    "/resume",
    summary="继续被中断的对话（SSE）",
    description=RESUME_DOC,
    responses={200: {"content": {SSE_MEDIA_TYPE: {}}, "description": "SSE 事件流"}},
)
async def chat_resume(payload: ResumeRequest) -> StreamingResponse:
    return _sse(
        payload.thread_id,
        get_service().resume(thread_id=payload.thread_id, value=payload.value),
    )


@router.get(
    "/history",
    summary="读取会话历史",
    description="从 checkpointer 读取指定会话的消息，供前端恢复既有对话。会话不存在时返回空列表。",
)
async def chat_history(
    thread_id: str = Query(..., description="会话 ID"),
) -> Result[HistoryResponse]:
    service = get_service()
    messages = await service.history(thread_id)
    pending = await service.pending_interrupts(thread_id)
    return Result.success(
        data=HistoryResponse(thread_id=thread_id, messages=messages, pending=pending)
    )


@router.get(
    "/threads",
    summary="列出既有会话",
    description=(
        "从 checkpointer 推导会话列表，按最近更新倒序。"
        "这是会话列表的唯一来源：localStorage 按 origin 隔离，"
        "而桌面端每次启动端口不同，靠前端自持清单必然丢。"
    ),
)
async def chat_threads(
    limit: int = Query(50, ge=1, le=200, description="最多返回多少条"),
) -> Result[ThreadListResponse]:
    threads = await get_service().threads(limit)
    return Result.success(data=ThreadListResponse(threads=threads))
