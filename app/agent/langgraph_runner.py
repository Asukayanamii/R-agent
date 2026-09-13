"""
基于 LangGraph 的 Agent 实现。

把 LangGraph 的 astream_events 翻译成本项目的统一事件协议，
上层路由与前端不感知 LangGraph 的存在。
"""

from collections.abc import AsyncIterator
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.types import Command, interrupt
from pydantic import BaseModel

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

SYSTEM_PROMPT = "你是一个简洁、准确的中文助手。需要时调用工具，不要编造工具返回的结果。"

TOOL_MAP = {tool.name: tool for tool in TOOLS}

REJECT_MESSAGE = "用户拒绝执行该操作。"


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


async def _run_tools(state: MessagesState):
    """
    逐个征求人工确认，然后只执行通过的调用。

    不用 ToolNode 的原因：它会执行 AIMessage 里的全部调用（含已被回绝的），
    并产生重复 tool_call_id 的 ToolMessage，审批拒绝因此形同虚设。
    这里统一承担审批与执行，单一执行路径。
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

    results = []
    for call in calls:
        if not approved.get(call["id"], True):
            results.append(
                ToolMessage(content=REJECT_MESSAGE, tool_call_id=call["id"])
            )
            continue

        tool = TOOL_MAP.get(call["name"])
        if tool is None:
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
            results.append(ToolMessage(content=str(output), tool_call_id=call["id"]))
        except Exception as exc:
            results.append(
                ToolMessage(
                    content=f"工具执行失败：{exc}",
                    tool_call_id=call["id"],
                    status="error",
                )
            )

    return {"messages": results}


def build_graph(checkpointer: BaseCheckpointSaver, model: BaseChatModel | None = None):
    """agent ↔ tools 循环，checkpointer 负责 thread_id 维度的多轮状态。"""
    model = model or ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        temperature=0,
        streaming=True,
    )
    model_with_tools = model.bind_tools(TOOLS)

    async def call_model(state: MessagesState):
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
        self, checkpointer: BaseCheckpointSaver, model: BaseChatModel | None = None
    ) -> None:
        self.graph = build_graph(checkpointer, model)

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    async def stream(self, thread_id: str, message: str) -> AsyncIterator[BaseModel]:
        inputs = {"messages": [HumanMessage(content=message)]}
        async for event in self._run(inputs, self._config(thread_id)):
            yield event

    async def resume(self, thread_id: str, value: str) -> AsyncIterator[BaseModel]:
        config = self._config(thread_id)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.interrupts:
            yield ErrorEvent(
                data=ErrorData(message="该会话没有待确认的操作，无需 resume")
            )
            return
        async for event in self._run(Command(resume=value), config):
            yield event

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        snapshot = await self.graph.aget_state(self._config(thread_id))
        result = []
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
        return result

    async def _run(self, inputs: object, config: dict) -> AsyncIterator[BaseModel]:
        usage = Usage()
        message_id = ""

        async for event in self.graph.astream_events(
            inputs, config=config, version="v2"
        ):
            kind = event["event"]

            if kind == "on_chat_model_stream":
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
                yield ToolStartEvent(
                    data=ToolStartData(
                        id=event["run_id"],
                        name=event.get("name", ""),
                        args=args if isinstance(args, dict) else {"input": args},
                    )
                )

            elif kind == "on_tool_end":
                output = event["data"].get("output")
                failed = getattr(output, "status", None) == "error"
                result = _text_of(output)
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
            payload = item.value if isinstance(item.value, dict) else {"prompt": str(item.value)}
            yield InterruptEvent(
                data=InterruptData(
                    id=item.id,
                    prompt=payload.get("prompt", ""),
                    options=list(payload.get("options") or []),
                )
            )

        yield MessageEndEvent(
            data=MessageEndData(message_id=message_id or uuid4().hex[:8], usage=usage)
        )
