"""
SSE 传输层。

把事件对象编码成 text/event-stream 帧，并统一收口 error / done 两个
控制面事件，保证任何情况下前端都能收到明确的结束标记。
"""

import asyncio
from collections.abc import AsyncIterator

from pydantic import BaseModel

from app.event.events import DoneData, DoneEvent, ErrorData, ErrorEvent

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

SSE_MEDIA_TYPE = "text/event-stream"


def encode_sse(event: BaseModel, seq: int) -> str:
    """
    编码单帧。

    id 字段为自增序号，供前端断线重连时检测是否丢帧。
    """
    return f"id: {seq}\nevent: message\ndata: {event.model_dump_json()}\n\n"


async def sse_stream(events: AsyncIterator[BaseModel]) -> AsyncIterator[str]:
    """
    包装业务事件流，追加结束标记。

    正常结束时补 done；抛异常时补 error + done。客户端断开触发的
    CancelledError 直接向上抛，不产生多余帧。
    """
    seq = 0
    try:
        async for event in events:
            seq += 1
            yield encode_sse(event, seq)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        seq += 1
        yield encode_sse(ErrorEvent(data=ErrorData(message=str(exc))), seq)

    seq += 1
    yield encode_sse(DoneEvent(data=DoneData()), seq)
