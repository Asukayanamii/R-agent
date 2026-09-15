from collections.abc import AsyncIterator
from uuid import uuid4

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.container import get_service
from app.event.events import (
    BrowseResponse,
    CompactionInfo,
    HistoryResponse,
    ThreadData,
    ThreadEvent,
    ThreadListResponse,
    WorkspaceInfo,
    WorkspaceResponse,
)
from app.event.stream import SSE_HEADERS, SSE_MEDIA_TYPE, sse_stream
from app.exceptions import InvalidInput
from app.result.result import Result

router = APIRouter(prefix="/chat", tags=["chat"])

EVENT_DOC = """
每帧 data 形如 {"type": ..., "data": {...}}。

- thread：会话握手，携带 thread_id，必为第一帧
- text_delta：增量文本，累加即得完整回复
- tool_start / tool_end：工具调用开始与结束，通过 id 配对
- interrupt：需要人工确认，前端渲染确认 UI 后调 POST /chat/resume
- compact：上下文已压缩。消息一条没删，只是"发给模型的那份"从这里开始变短了
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


class WorkspaceRequest(BaseModel):
    thread_id: str = Field(..., description="会话 ID")
    path: str = Field(
        ..., min_length=1, description="工作区目录。绝对路径，或相对应用目录的路径"
    )


class CompactRequest(BaseModel):
    thread_id: str = Field(..., description="会话 ID")


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


@router.post(
    "/compact",
    summary="主动压缩上下文",
    description=(
        "把会话中段摘要掉，等价 Claude Code 的 `/compact`。\n\n"
        "与自动压缩共用同一套逻辑，区别是**跳过阈值与冷却**——用户点了就是要压。"
        "消息一条不删：变的只是「发给模型的那份」，历史照旧完整。\n\n"
        "没得压（会话还短、中段不足两条，或压完并不更省）时返回失败并说明原因，"
        "而不是静默成功。`COMPACT_ENABLED=0` 只关自动压缩，这个接口照常可用"
        "（与 Pi 的取舍一致）。"
    ),
)
async def chat_compact(payload: CompactRequest) -> Result[CompactionInfo]:
    info = await get_service().compact(payload.thread_id)
    if info is None:
        return Result.fail(
            message="这次没有可压的内容：中段太短（不足两条），或者压完并不比现在更省"
        )
    return Result.success(data=info)


@router.get(
    "/history",
    summary="读取会话历史",
    description="从 checkpointer 读取指定会话的消息，供前端恢复既有对话。会话不存在时返回空列表。",
)
async def chat_history(
    thread_id: str = Query(..., description="会话 ID"),
) -> Result[HistoryResponse]:
    service = get_service()
    view = await service.history(thread_id)
    pending = await service.pending_interrupts(thread_id)
    return Result.success(
        data=HistoryResponse(
            thread_id=thread_id,
            messages=view.messages,
            pending=pending,
            compaction=view.compaction,
        )
    )


@router.put(
    "/workspace",
    summary="把会话绑定到工作区",
    description=(
        "工作区是沙箱的信任边界：工具能自由访问工作区内的路径，越界会弹确认卡片。"
        "同一工作区下的所有对话共享同一份沙箱授权。\n\n"
        "**只有用户能改它**——agent 若能改自己的边界，边界就不存在了。"
        "前端只在会话诞生时调用它（新对话先选目录、发第一条消息前绑定），"
        "不会用它把既有会话搬来搬去——归属一旦定下，只有删除会话才会清掉。"
    ),
)
async def chat_set_workspace(payload: WorkspaceRequest) -> Result[WorkspaceResponse]:
    try:
        resolved = await get_service().set_workspace(payload.thread_id, payload.path)
    except InvalidInput as exc:
        return Result.fail(message=str(exc))
    return Result.success(
        data=WorkspaceResponse(thread_id=payload.thread_id, workspace=resolved)
    )


@router.get(
    "/browse",
    summary="列出目录（供挑选工作区）",
    description=(
        "**刻意不受沙箱约束**——沙箱限制的是 agent，不是用户；"
        "用户本来就能在自己机器上任意选目录。\n\n"
        "代价是它成为一个可列举任意目录的接口。本地单用户部署没问题，"
        "若要把 API 暴露出去，必须先加鉴权或去掉它。"
    ),
)
async def chat_browse(
    path: str = Query("", description="要列出的目录；留空则返回用户目录与各盘符"),
) -> Result[BrowseResponse]:
    try:
        data = get_service().browse(path)
    except InvalidInput as exc:
        return Result.fail(message=str(exc))
    return Result.success(data=data)


@router.get(
    "/threads",
    summary="列出既有会话",
    description=(
        "从 checkpointer 推导会话列表，按最近更新倒序。"
        "这是会话列表的唯一来源：localStorage 按 origin 隔离，"
        "而桌面端每次启动端口不同，靠前端自持清单必然丢。\n\n"
        "顺带给出 `default_workspace`：没选过工作区时新会话落在应用所在目录，"
        "它和用户自己挑的工作区是同一类东西，前端不需要为它单开一个分组。"
    ),
)
async def chat_threads(
    limit: int = Query(50, ge=1, le=200, description="最多返回多少条"),
) -> Result[ThreadListResponse]:
    service = get_service()
    threads = await service.threads(limit)
    default = await service.default_workspace()
    return Result.success(
        data=ThreadListResponse(
            threads=threads,
            default_workspace=WorkspaceInfo(path=default.path, name=default.name),
        )
    )


@router.delete(
    "/threads/{thread_id}",
    summary="删除会话",
    description=(
        "删除该会话的检查点与索引行，**不可恢复**。不存在的会话静默通过。\n\n"
        "该会话在沙箱里获得过的授权**不会**跟着删——授权按工作区归属，"
        "同工作区的其他会话还在用。"
    ),
)
async def delete_thread(thread_id: str) -> Result[None]:
    await get_service().delete_thread(thread_id)
    return Result.success(message="已删除")
