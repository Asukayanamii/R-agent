"""
Agent 执行器（LangGraph 实现）。

**这个文件只是门面**：把 `AgentRunner` 协议的每个方法接到具体模块上——图与节点在 `graph`、
事件翻译在 `stream`、历史重建在 `messages`、模型在 `models`、压缩在 `compaction/`、
工具执行在 `tool_calls`。这里不写业务逻辑，只负责串起来。

上层路由与前端不感知 LangGraph 的存在。
"""

import logging
from collections.abc import AsyncIterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command
from pydantic import BaseModel

from app.agent.compaction.runtime import COMPACT_NODE, compact_once
from app.agent.graph import RECURSION_LIMIT, build_graph
from app.agent.messages import (
    INTERRUPTED_MESSAGE,
    build_history,
    title_of,
    to_interrupt_data,
    unanswered_calls,
)
from app.agent.models import build_summary_model
from app.agent.stream import translate_events
from app.event.events import (
    CompactionInfo,
    ErrorData,
    ErrorEvent,
    HistoryView,
    InterruptData,
)
from app.models.entities import ThreadRecord

logger = logging.getLogger(__name__)


class LangGraphRunner:
    """把 LangGraph 事件流翻译成统一事件协议。"""

    def __init__(
        self,
        checkpointer: BaseCheckpointSaver,
        model: BaseChatModel | None = None,
        summary_model: BaseChatModel | None = None,
    ) -> None:
        self.checkpointer = checkpointer
        # 摘要模型也留着：用户主动压缩（compact）走的是图外那条路，得能拿到它
        self.summary_model = summary_model or build_summary_model()
        self.graph = build_graph(checkpointer, model, self.summary_model)

    @staticmethod
    def _config(thread_id: str, workspace: str | None = None) -> dict:
        configurable: dict = {"thread_id": thread_id}
        if workspace:
            configurable["workspace"] = workspace
        # 图的 superstep 上限：LangGraph 默认 25 步，而一个工具回合要花 3 步
        # （tools → compact → agent），默认值只够 7 个回合。取值理由见 graph.RECURSION_LIMIT。
        return {"configurable": configurable, "recursion_limit": RECURSION_LIMIT}

    async def stream(
        self, thread_id: str, message: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
        inputs = {"messages": [HumanMessage(content=message)]}
        async for event in translate_events(
            self.graph,
            thread_id=thread_id,
            config=self._config(thread_id, workspace),
            inputs=inputs,
        ):
            yield event

    async def resume(
        self, thread_id: str, value: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
        config = self._config(thread_id, workspace)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.interrupts:
            yield ErrorEvent(
                data=ErrorData(message="该会话没有待确认的操作，无需 resume")
            )
            return
        async for event in translate_events(
            self.graph,
            thread_id=thread_id,
            config=config,
            inputs=Command(resume=value),
        ):
            yield event

    async def compact(self, thread_id: str) -> CompactionInfo | None:
        """
        用户主动压缩一次（等价 Claude Code 的 `/compact`）。

        与自动压缩共用 `compact_once`，区别只有 force：跳过阈值、开关与冷却——用户点了
        就是要压。压完就结束，不顺带调主模型（回答留给下一次提问），省一次没必要的开销。

        状态写回用 `as_node=COMPACT_NODE`：图的"下一步"因此是 agent，与自动压缩跑完时一致。
        返回的 `before` 由 `history()` 现算，保证与重开会话时的分隔线位置同一套规则。

        没得压（会话还短、中段不足两条）返回 None，由上层说明原因，不静默通过。
        """
        config = self._config(thread_id)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            return None

        update = await compact_once(
            snapshot.values,
            summary_model=self.summary_model,
            thread_id=thread_id,
            force=True,
        )
        if not update:
            return None

        await self.graph.aupdate_state(config, update, as_node=COMPACT_NODE)
        record = update["compaction"]
        view = await self.history(thread_id)
        return CompactionInfo(
            before=view.compaction.before if view.compaction else 0,
            summary=record["summary"],
            tokens_before=int(record["tokens_before"]),
            tokens_after=int(record["tokens_after"]),
            count=int(record["count"]),
            at=record["at"],
        )

    async def history(self, thread_id: str) -> HistoryView:
        """
        读取既有会话的消息与压缩分界（重建逻辑在 messages.build_history）。

        压缩只改"发给模型的视图"，所以这里的消息一条不少——用户看得到被摘要掉的原文。
        """
        snapshot = await self.graph.aget_state(self._config(thread_id))
        view = build_history(snapshot.values)
        logger.debug("读取历史 thread=%s 消息=%d", thread_id, len(view.messages))
        return view

    async def delete_thread(self, thread_id: str) -> None:
        """
        删掉该会话的检查点。

        用 checkpointer 自带的 `adelete_thread`，它会同时清 `checkpoints` 与 `writes`
        两张表（实测确认）。自己写 SQL 很容易漏掉 writes。
        """
        await self.checkpointer.adelete_thread(thread_id)
        logger.debug("已删除检查点 thread=%s", thread_id)

    async def is_interrupted(self, thread_id: str) -> bool:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return bool(snapshot.interrupts)

    async def pending_interrupts(self, thread_id: str) -> list[InterruptData]:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return [to_interrupt_data(item) for item in snapshot.interrupts]

    async def has_dangling_tool_calls(self, thread_id: str) -> bool:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return bool(unanswered_calls(snapshot.values.get("messages", [])))

    async def repair_after_abort(self, thread_id: str) -> int:
        """
        一轮被中断后把状态修回"可继续"。

        用 `aupdate_state(..., as_node="tools")` 补上"已中断"的 ToolMessage，等于告诉
        LangGraph 这些调用有结果了：消息链重新合法，下一次发消息照常从 agent 走。

        停在待确认（interrupt）上的会话**不动**——那是合法状态，点确认就能继续。
        """
        config = self._config(thread_id)
        snapshot = await self.graph.aget_state(config)
        if snapshot.interrupts:
            return 0

        calls = unanswered_calls(snapshot.values.get("messages", []))
        if not calls:
            return 0

        await self.graph.aupdate_state(
            config,
            {
                "messages": [
                    ToolMessage(
                        content=INTERRUPTED_MESSAGE,
                        tool_call_id=call["id"],
                        status="error",
                    )
                    for call in calls
                ]
            },
            as_node="tools",
        )
        logger.info("补齐被中断的调用 thread=%s 条数=%d", thread_id, len(calls))
        return len(calls)

    async def scan_threads(self, limit: int = 200) -> list[ThreadRecord]:
        """
        从 checkpointer 枚举会话，成本是 N+1 次查询，只供索引重建使用。

        checkpointer 没有"列出全部 thread_id"的正式接口，alist(None) 是可行入口；
        其返回顺序即最近更新在前，因此同一 thread_id 首次出现时拿到的就是最新时间戳。
        """
        latest: dict[str, str] = {}
        async for checkpoint in self.checkpointer.alist(None):
            thread_id = checkpoint.config["configurable"]["thread_id"]
            if thread_id not in latest:
                latest[thread_id] = checkpoint.checkpoint.get("ts", "")
                if len(latest) >= limit:
                    break
        logger.debug("扫描到 %d 个会话，逐个取状态中", len(latest))

        result = []
        for thread_id, timestamp in latest.items():
            snapshot = await self.graph.aget_state(self._config(thread_id))
            result.append(
                ThreadRecord(
                    thread_id=thread_id,
                    title=title_of(snapshot.values),
                    updated_at=timestamp,
                    pending=bool(snapshot.interrupts),
                )
            )
        logger.debug("扫描完成：%d 条记录", len(result))
        return result
