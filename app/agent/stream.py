"""
事件翻译：把 LangGraph 的 `astream_events` 翻成本项目的统一事件协议。

上层路由与前端不感知 LangGraph 的存在，转义表都在这一个文件里：

| LangGraph 事件 | 项目事件 |
| --- | --- |
| `on_chat_model_stream` | `text_delta` |
| `on_tool_start` / `on_tool_end` | `tool_start` / `tool_end` |
| compact 节点跑完（`on_chain_end`） | `compact` |

一轮里模型可能被调用多次（每次工具返回后都要再问一次），所以 usage 要**累加**，
message_id 取最后一次的。中断不在事件流里（见 LESSONS），跑完从状态快照读。
"""

import logging
from collections.abc import AsyncIterator
from time import monotonic
from uuid import uuid4

from langgraph.errors import GraphBubbleUp, GraphRecursionError
from pydantic import BaseModel

from app.agent.compaction.policy import TAG as COMPACT_TAG
from app.agent.compaction.runtime import COMPACT_NODE
from app.agent.graph import log_step_limit
from app.agent.messages import brief, text_of, to_diff, to_interrupt_data
from app.event.events import (
    CompactData,
    CompactEvent,
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

logger = logging.getLogger(__name__)


async def translate_events(
    graph, *, thread_id: str, config: dict, inputs: object
) -> AsyncIterator[BaseModel]:
    """
    跑一次图，边跑边把事件翻译成协议形状。

    摘要调用（tags 里带 `compact`，见 compaction/policy.py）不算"模型的回答"：它的文本
    要挡在前端之外，usage 照常计入本轮——那笔钱是真实花掉的。

    撞上 superstep 上限时补一条日志再原样上抛（错误文案仍是 LangGraph 的原文）。
    """
    usage = Usage()
    message_id = ""
    started = monotonic()
    tool_started: dict[str, float] = {}

    try:
        async for event in graph.astream_events(inputs, config=config, version="v2"):
            kind = event["event"]
            tagged_compact = COMPACT_TAG in (event.get("tags") or [])

            if kind == "on_chat_model_stream":
                if tagged_compact:
                    continue
                # 模型边生成边推 chunk，增量直接透传，前端累加即得完整回复
                text = text_of(event["data"].get("chunk"))
                if text:
                    yield TextDeltaEvent(data=TextDeltaData(text=text))

            elif kind == "on_chat_model_end":
                output = event["data"].get("output")
                meta = getattr(output, "usage_metadata", None) or {}
                usage = Usage(
                    input_tokens=usage.input_tokens + meta.get("input_tokens", 0),
                    output_tokens=usage.output_tokens + meta.get("output_tokens", 0),
                )
                if tagged_compact:
                    logger.debug(
                        "摘要调用 tokens=%d/%d",
                        meta.get("input_tokens", 0),
                        meta.get("output_tokens", 0),
                    )
                    continue
                message_id = getattr(output, "id", "") or message_id

            elif kind == "on_chain_end" and event.get("name") == COMPACT_NODE:
                # 节点返回的就是状态更新：这一轮真压了才有 compaction 字段（没压是空更新）。
                # 报文事件在这里发，位置正好在模型回复之前——前端先看到分隔线，再看回答。
                update = event["data"].get("output")
                record = update.get("compaction") if isinstance(update, dict) else None
                if record:
                    yield CompactEvent(
                        data=CompactData(
                            tokens_before=int(record.get("tokens_before") or 0),
                            tokens_after=int(record.get("tokens_after") or 0),
                            summary=record.get("summary", ""),
                            count=int(record.get("count") or 0),
                        )
                    )

            elif kind == "on_tool_start":
                args = event["data"].get("input") or {}
                tool_args = args if isinstance(args, dict) else {"input": args}
                name = event.get("name", "")
                tool_started[event["run_id"]] = monotonic()
                logger.info(
                    "工具开始 name=%s thread=%s 参数=%s",
                    name,
                    thread_id,
                    brief(tool_args),
                )
                yield ToolStartEvent(
                    data=ToolStartData(id=event["run_id"], name=name, args=tool_args)
                )

            elif kind == "on_tool_end":
                output = event["data"].get("output")
                failed = getattr(output, "status", None) == "error"
                result = text_of(output)
                elapsed = monotonic() - tool_started.pop(event["run_id"], monotonic())
                logger.info(
                    "工具结束 name=%s thread=%s ok=%s 用时=%.2fs 结果=%s",
                    event.get("name", ""),
                    thread_id,
                    not failed,
                    elapsed,
                    brief(result),
                )
                yield ToolEndEvent(
                    data=ToolEndData(
                        id=event["run_id"],
                        ok=not failed,
                        result=None if failed else result,
                        error=result if failed else None,
                        diff=None if failed else to_diff(getattr(output, "artifact", None)),
                    )
                )

            elif kind == "on_tool_error":
                # 工具抛异常时 LangGraph 只发 error、不发 end（langgraph#6018），
                # 所以这里补上 tool_end，否则前端那张卡片会一直停在"运行中"。
                # 中断也从这条路上来（interrupt 抛的是 GraphBubbleUp），那不是失败：
                # 卡片由确认卡片接手，前端收到 interrupt 时会把运行中的卡片标成未完成。
                error = event["data"].get("error")
                tool_started.pop(event["run_id"], None)
                if isinstance(error, GraphBubbleUp):
                    logger.debug("工具停在中断上 name=%s", event.get("name", ""))
                else:
                    logger.debug("工具抛异常 name=%s：%s", event.get("name", ""), error)
                    yield ToolEndEvent(
                        data=ToolEndData(
                            id=event["run_id"], ok=False, error=brief(str(error))
                        )
                    )
    except GraphRecursionError as exc:
        log_step_limit(thread_id, exc)
        raise

    # 中断不出现在 astream_events 里，只能跑完从状态快照读。
    snapshot = await graph.aget_state(config)
    for item in snapshot.interrupts:
        data = to_interrupt_data(item)
        logger.info("等待人工确认 thread=%s：%s", thread_id, brief(data.prompt))
        yield InterruptEvent(data=data)

    logger.debug(
        "本轮跑完 thread=%s 用时=%.2fs tokens=%d/%d",
        thread_id,
        monotonic() - started,
        usage.input_tokens,
        usage.output_tokens,
    )
    yield MessageEndEvent(
        data=MessageEndData(message_id=message_id or uuid4().hex[:8], usage=usage)
    )
