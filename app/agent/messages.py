"""
消息与历史：把图状态里的消息翻译成协议形状。

这里全是纯函数——不碰图、不碰 IO，所以能单独测。各函数的职责：

- `text_of`：content 可能是字符串，也可能是分段列表（流式与多模态两种形态）
- `brief`：日志用的一行摘要（工具参数与返回值可能是很长的字典或多行文本）
- `error_message` / `unanswered_calls`：失败回执的构造、悬空调用的判定
- `to_history_call` / `to_interrupt_data` / `title_of` / `build_history`：读路径 → 协议形状
- `to_diff`：工具 artifact（结构化差异）→ 协议形状，实时与历史两条路共用
- `usable_compaction`：这条压缩记录还能不能对着这份消息列表用
"""

import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from app.event.events import (
    CompactionInfo,
    HistoryMessage,
    HistoryToolCall,
    HistoryView,
    InterruptData,
    ToolDiff,
)

logger = logging.getLogger(__name__)

INTERRUPTED_MESSAGE = "这一轮被中断，未取得结果。"


def text_of(message: object) -> str:
    """兼容 content 为字符串与分段列表两种形态。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def brief(value: object, limit: int = 200) -> str:
    """日志用：压成一行并截断——工具参数与返回值可能是很长的字典或多行文本。"""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def error_message(call_id: str, text: str) -> ToolMessage:
    """
    给模型的失败回执。

    `status="error"` 是"失败"的唯一标记：前端卡片、历史恢复都读它。
    """
    return ToolMessage(content=text, tool_call_id=call_id, status="error")


def unanswered_calls(messages: list) -> list[dict]:
    """
    收集"没人回应的 tool_calls"。

    provider 对历史的要求是每个 tool_use 都有配对的 tool_result，所以这些调用会让**下一次
    请求直接 400**——修好之前，这个会话对模型来说是不可继续的。
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    return [
        call
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["id"] not in answered
    ]


def usable_compaction(compaction: dict, total: int) -> bool:
    """
    这条压缩记录还能不能用。

    锚点是消息列表里的下标：越界就**忽略压缩、发全量**并打 WARNING——退化方向必须是
    "多花 token"，而不是拿一条错位的历史去发请求。

    放这里是因为两个方向都要用它：`build_history`（画分隔线）与图的 `call_model`（拼提示词）。
    """
    index = compaction.get("from_index")
    return isinstance(index, int) and 1 <= index < total


def to_diff(artifact: object) -> ToolDiff | None:
    """
    把工具留下的 artifact 翻成协议形状（目前只有 edit 的结构化差异用它）。

    形状不对就当没有：这是**展示用**的数据，不能因为它让整轮对话失败，但也不能装作
    没发生——留一行 WARNING。别的工具（artifact 为 None）走到这里直接返回。
    """
    if not isinstance(artifact, dict) or "lines" not in artifact:
        return None
    try:
        return ToolDiff.model_validate(artifact)
    except ValidationError as exc:
        logger.warning("工具的 artifact 形状不对，忽略差异：%s", exc)
        return None


def to_history_call(call: dict, result: object) -> HistoryToolCall:
    """
    把一次调用与它的结果配成历史里的一张工具卡片。

    没有结果是正常情况：那一轮可能停在待确认上，或者流被中断了。
    """
    if result is None:
        return HistoryToolCall(
            id=call["id"],
            name=call["name"],
            args=call.get("args") or {},
            state="pending",
            error="未完成：可能停在待确认上",
        )
    text = text_of(result)
    failed = getattr(result, "status", None) == "error"
    return HistoryToolCall(
        id=call["id"],
        name=call["name"],
        args=call.get("args") or {},
        state="failed" if failed else "ok",
        result=None if failed else text,
        error=text if failed else None,
        diff=None if failed else to_diff(getattr(result, "artifact", None)),
    )


def to_interrupt_data(item: object) -> InterruptData:
    """把 LangGraph 的 interrupt 对象翻成协议形状（prompt + options）。"""
    payload = (
        item.value
        if isinstance(item.value, dict)
        else {"prompt": str(item.value)}
    )
    return InterruptData(
        id=item.id,
        prompt=payload.get("prompt", ""),
        options=list(payload.get("options") or []),
    )


def title_of(values: dict) -> str:
    """会话标题：取第一条用户消息。"""
    for message in values.get("messages", []):
        if isinstance(message, HumanMessage):
            return text_of(message)
    return ""


def build_history(values: dict) -> HistoryView:
    """
    从状态里重建会话历史。

    工具调用不是另存一份记录，而是**从消息本身推出来**：`AIMessage.tool_calls` 是调用，
    按 `tool_call_id` 配对的 `ToolMessage` 是结果——两边都在检查点里。
    模型先调工具、再给结论，所以卡片挂在后面那条有正文的 assistant 消息上。

    压缩只改"发给模型的视图"，所以这里的消息一条不少；额外给出压缩分界的位置，
    前端据此插一条分隔线——用户仍然看得到被摘要掉的原文。
    """
    messages = values.get("messages", [])
    results = {
        message.tool_call_id: message
        for message in messages
        if isinstance(message, ToolMessage)
    }
    compaction = values.get("compaction") or {}
    boundary = (
        compaction.get("from_index")
        if usable_compaction(compaction, len(messages))
        else None
    )

    out: list[HistoryMessage] = []
    pending_calls: list[HistoryToolCall] = []
    boundary_before = 0
    for index, message in enumerate(messages):
        if boundary is not None and index == boundary:
            # 走到锚点这条时，已经渲染出去的就是"分界之前"的部分
            boundary_before = len(out)
        if isinstance(message, HumanMessage):
            out.append(
                HistoryMessage(
                    role="user",
                    content=text_of(message),
                    id=getattr(message, "id", None),
                )
            )
            continue
        if not isinstance(message, AIMessage):
            # 工具消息不进正文：它已经以卡片形式挂在调用的那条 assistant 消息上了
            continue

        pending_calls.extend(
            to_history_call(call, results.get(call["id"]))
            for call in message.tool_calls
        )
        text = text_of(message)
        if text:
            out.append(
                HistoryMessage(
                    role="assistant",
                    content=text,
                    id=getattr(message, "id", None),
                    tool_calls=pending_calls,
                )
            )
            pending_calls = []

    if pending_calls:
        # 只有调用没有结论：停在待确认上，或那一轮被中断了。
        # 补一条空正文的消息，卡片才有地方挂。
        out.append(
            HistoryMessage(role="assistant", content="", tool_calls=pending_calls)
        )

    info = None
    if boundary is not None:
        info = CompactionInfo(
            before=boundary_before,
            summary=compaction.get("summary", ""),
            tokens_before=int(compaction.get("tokens_before") or 0),
            tokens_after=int(compaction.get("tokens_after") or 0),
            count=int(compaction.get("count") or 0),
            at=compaction.get("at", ""),
        )
    calls = sum(len(item.tool_calls) for item in out)
    logger.debug("重建历史：消息=%d 工具调用=%d", len(out), calls)
    return HistoryView(messages=out, compaction=info)
