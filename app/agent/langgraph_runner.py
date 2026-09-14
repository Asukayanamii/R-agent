"""
基于 LangGraph 的 Agent 实现。

把 LangGraph 的 astream_events 翻译成本项目的统一事件协议，
上层路由与前端不感知 LangGraph 的存在。
"""

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from time import monotonic
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from app.agent.runtime import current_workspace
from app.agent.tools import APPROVAL_REQUIRED, TOOLS
from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
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

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "你是一个在用户指定工作区里干活的编程助手。"
    "bash 与文件类工具的工作目录都是当前工作区根目录；"
    "工作区之外的位置需要用户授权才能访问。"
    "需要查看代码、跑测试或执行命令时用工具，不要编造工具返回的内容。"
    "回答用中文，简洁准确。"
)

TOOL_MAP = {tool.name: tool for tool in TOOLS}

REJECT_MESSAGE = "用户拒绝执行该操作。"


def _brief(value: object, limit: int = 200) -> str:
    """日志用：压成一行并截断——工具参数与返回值可能是很长的字典或多行文本。"""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _text_of(message: object) -> str:
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


def _workspace_token(config: dict):
    """
    把本次运行的工作区放进 ContextVar，供沙箱判定信任边界。

    RunnableConfig 是 LangGraph 给"每次调用的参数"准备的位置，不自造 state 字段。
    """
    workspace = (config.get("configurable") or {}).get("workspace")
    if not workspace:
        return None
    return current_workspace.set(Path(workspace))


async def _run_tools(state: MessagesState):
    """
    逐个征求人工确认，然后只执行通过的调用。

    不用 ToolNode 的原因：它会执行 AIMessage 里的全部调用（含已被回绝的），
    并产生重复 tool_call_id 的 ToolMessage，审批拒绝因此形同虚设。
    这里统一承担审批与执行，单一执行路径。

    注意 `except GraphBubbleUp: raise` 不能删。interrupt() 抛的就是它，
    被 except Exception 接住的话，工具内部的沙箱授权询问会退化成一条
    "工具执行失败"——静默失效，而且很难查。
    """
    config = get_config()
    calls = state["messages"][-1].tool_calls

    approved: dict[str, bool] = {}
    for call in calls:
        if call["name"] in APPROVAL_REQUIRED:
            decision = interrupt(
                {
                    "prompt": f"请求调用 {call['name']}，参数 {call['args']}",
                    "options": ["确认", "取消"],
                }
            )
            approved[call["id"]] = decision == "确认"
            logger.info("工具审批 name=%s 决定=%s", call["name"], decision)

    results = []
    token = _workspace_token(config)
    try:
        for call in calls:
            if not approved.get(call["id"], True):
                logger.warning("工具被拒绝 name=%s", call["name"])
                results.append(
                    ToolMessage(content=REJECT_MESSAGE, tool_call_id=call["id"])
                )
                continue

            tool = TOOL_MAP.get(call["name"])
            if tool is None:
                logger.warning("模型要调未知工具 name=%s", call["name"])
                results.append(
                    ToolMessage(
                        content=f"未知工具：{call['name']}",
                        tool_call_id=call["id"],
                        status="error",
                    )
                )
                continue

            try:
                output = await tool.ainvoke(call["args"], config=config)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                # 工具自己报错不算致命：写成 ToolMessage 让模型看到并解释，日志留一条
                logger.warning("工具执行失败 name=%s：%s", call["name"], exc)
                results.append(
                    ToolMessage(
                        content=f"工具执行失败：{exc}",
                        tool_call_id=call["id"],
                        status="error",
                    )
                )
            else:
                results.append(
                    ToolMessage(content=str(output), tool_call_id=call["id"])
                )
    finally:
        if token is not None:
            current_workspace.reset(token)

    return {"messages": results}


def build_graph(checkpointer: BaseCheckpointSaver, model: BaseChatModel | None = None):
    """
    agent ↔ tools 循环，checkpointer 负责 thread_id 维度的多轮状态。

    `tools_condition` 按最后一条 AI 消息里有没有 tool_calls 分流：有就去 tools 执行，
    没有就结束（END）。工具结果以 ToolMessage 回到消息列表，再从 agent 走一遍。
    """
    model = model or ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        temperature=0,
        streaming=True,  # 关掉流式就没有 text_delta，前端只能等整段回复
    )
    model_with_tools = model.bind_tools(TOOLS)

    async def call_model(state: MessagesState):
        # 节点本身无状态：每轮都把 system prompt 重新拼在历史前面，历史来自 checkpointer
        response = await model_with_tools.ainvoke(
            [{"role": "system", "content": SYSTEM_PROMPT}, *state["messages"]]
        )
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", _run_tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges(
        "agent", tools_condition, {"tools": "tools", END: END}
    )
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)


class LangGraphRunner:
    """把 LangGraph 事件流翻译成统一事件协议。"""

    def __init__(
        self,
        checkpointer: BaseCheckpointSaver,
        model: BaseChatModel | None = None,
    ) -> None:
        self.checkpointer = checkpointer
        self.graph = build_graph(checkpointer, model)

    @staticmethod
    def _config(thread_id: str, workspace: str | None = None) -> dict:
        configurable: dict = {"thread_id": thread_id}
        if workspace:
            configurable["workspace"] = workspace
        return {"configurable": configurable}

    async def stream(
        self, thread_id: str, message: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
        inputs = {"messages": [HumanMessage(content=message)]}
        async for event in self._run(thread_id, workspace, inputs):
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
        async for event in self._run(thread_id, workspace, Command(resume=value)):
            yield event

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        result: list[HistoryMessage] = []
        # 只回人类与模型的文本：工具消息不进历史，前端恢复的是对话本身
        for message in snapshot.values.get("messages", []):
            if isinstance(message, HumanMessage):
                role = "user"
            elif isinstance(message, AIMessage):
                role = "assistant"
            else:
                continue
            text = _text_of(message)
            if not text:
                continue
            result.append(
                HistoryMessage(
                    role=role, content=text, id=getattr(message, "id", None)
                )
            )
        logger.debug("读取历史 thread=%s 条数=%d", thread_id, len(result))
        return result

    async def delete_thread(self, thread_id: str) -> None:
        """
        删掉该会话的检查点。

        用 checkpointer 自带的 `adelete_thread`，它会同时清 `checkpoints` 与 `writes`
        两张表（实测确认）。自己写 SQL 很容易漏掉 writes。
        """
        await self.checkpointer.adelete_thread(thread_id)
        logger.debug("已删除检查点 thread=%s", thread_id)

    @staticmethod
    def _title_of(snapshot: object) -> str:
        for message in getattr(snapshot, "values", {}).get("messages", []):
            if isinstance(message, HumanMessage):
                return _text_of(message)
        return ""

    @staticmethod
    def _to_interrupt_data(item: object) -> InterruptData:
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

    async def is_interrupted(self, thread_id: str) -> bool:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return bool(snapshot.interrupts)

    async def pending_interrupts(self, thread_id: str) -> list[InterruptData]:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        return [self._to_interrupt_data(item) for item in snapshot.interrupts]

    async def has_dangling_tool_calls(self, thread_id: str) -> bool:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        messages = snapshot.values.get("messages", [])
        answered = {
            m.tool_call_id for m in messages if isinstance(m, ToolMessage)
        }
        return any(
            call["id"] not in answered
            for message in messages
            if isinstance(message, AIMessage)
            for call in message.tool_calls
        )

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
                    title=self._title_of(snapshot),
                    updated_at=timestamp,
                    pending=bool(snapshot.interrupts),
                )
            )
        logger.debug("扫描完成：%d 条记录", len(result))
        return result

    async def _run(
        self, thread_id: str, workspace: str | None, inputs: object
    ) -> AsyncIterator[BaseModel]:
        """
        跑一次图，把 LangGraph 事件翻译成本项目的协议事件。

        翻译表：`on_chat_model_stream` → text_delta、`on_tool_start/end` → tool_start/end。
        一轮里模型可能被调用多次（每次工具返回后都要再问一次），所以 usage 要**累加**，
        message_id 取最后一次的。中断不在事件流里（见 LESSONS），跑完从状态快照读。
        """
        config = self._config(thread_id, workspace)
        usage = Usage()
        message_id = ""
        started = monotonic()
        tool_started: dict[str, float] = {}

        async for event in self.graph.astream_events(
            inputs, config=config, version="v2"
        ):
            kind = event["event"]

            if kind == "on_chat_model_stream":
                # 模型边生成边推 chunk，增量直接透传，前端累加即得完整回复
                text = _text_of(event["data"].get("chunk"))
                if text:
                    yield TextDeltaEvent(data=TextDeltaData(text=text))

            elif kind == "on_chat_model_end":
                output = event["data"].get("output")
                meta = getattr(output, "usage_metadata", None) or {}
                usage = Usage(
                    input_tokens=usage.input_tokens + meta.get("input_tokens", 0),
                    output_tokens=usage.output_tokens + meta.get("output_tokens", 0),
                )
                message_id = getattr(output, "id", "") or message_id

            elif kind == "on_tool_start":
                args = event["data"].get("input") or {}
                tool_args = args if isinstance(args, dict) else {"input": args}
                name = event.get("name", "")
                tool_started[event["run_id"]] = monotonic()
                logger.info(
                    "工具开始 name=%s thread=%s 参数=%s",
                    name,
                    thread_id,
                    _brief(tool_args),
                )
                yield ToolStartEvent(
                    data=ToolStartData(id=event["run_id"], name=name, args=tool_args)
                )

            elif kind == "on_tool_end":
                output = event["data"].get("output")
                failed = getattr(output, "status", None) == "error"
                result = _text_of(output)
                elapsed = monotonic() - tool_started.pop(event["run_id"], monotonic())
                logger.info(
                    "工具结束 name=%s thread=%s ok=%s 用时=%.2fs 结果=%s",
                    event.get("name", ""),
                    thread_id,
                    not failed,
                    elapsed,
                    _brief(result),
                )
                yield ToolEndEvent(
                    data=ToolEndData(
                        id=event["run_id"],
                        ok=not failed,
                        result=None if failed else result,
                        error=result if failed else None,
                    )
                )

        # 中断不出现在 astream_events 里，只能跑完从状态快照读。
        snapshot = await self.graph.aget_state(config)
        for item in snapshot.interrupts:
            data = self._to_interrupt_data(item)
            logger.info("等待人工确认 thread=%s：%s", thread_id, _brief(data.prompt))
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
