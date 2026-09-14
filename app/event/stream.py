"""
SSE 传输层。

把事件对象编码成 text/event-stream 帧，并统一收口 error / done 两个
控制面事件，保证任何情况下前端都能收到明确的结束标记。
"""

import asyncio
import logging
from collections.abc import AsyncIterator

from pydantic import BaseModel

from app.event.events import DoneData, DoneEvent, ErrorData, ErrorEvent

logger = logging.getLogger(__name__)

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

    正常结束时补 done；抛异常时补 error + done。**这是流式这半边的唯一异常出口**：
    上游（模型服务、图执行）抛什么都在这里落地，进程不受影响，前端也总能收到明确的结束标记。
    帧里放异常原文，不做翻译——面向程序员的项目，原文比客套话有用。

    客户端断开触发的 CancelledError 直接向上抛，不产生多余帧。
    """
    seq = 0
    try:
        async for event in events:
            seq += 1
            yield encode_sse(event, seq)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("对话流异常")  # 堆栈留在服务端，帧里给原文
        seq += 1
        # 空 message 的异常（如裸 raise ValueError()）至少让用户看到类型
        message = str(exc) or type(exc).__name__
        yield encode_sse(ErrorEvent(data=ErrorData(message=message)), seq)

    seq += 1
    yield encode_sse(DoneEvent(data=DoneData()), seq)
