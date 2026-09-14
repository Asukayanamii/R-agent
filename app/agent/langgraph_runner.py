"""
基于 LangGraph 的 Agent 实现。

把 LangGraph 的 astream_events 翻译成本项目的统一事件协议，
上层路由与前端不感知 LangGraph 的存在。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from time import monotonic
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from langchain_core.tools import ToolException
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
from app.exceptions import ModelUnavailable
from app.event.events import (
    ErrorData,
    ErrorEvent,
    HistoryMessage,
    HistoryToolCall,
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

INTERRUPTED_MESSAGE = "这一轮被中断，未取得结果。"

NO_MODEL_REASON = (
    "未配置 LLM_API_KEY，无法对话。"
    "在项目根目录的 .env 里填上 key（可参考 .env.example），重启应用后即可使用。"
)


class _UnavailableModel(BaseChatModel):
    """
    没配 `LLM_API_KEY` 时的占位模型：一调用就抛，不假装能回答。

    为什么只换模型、不换整个 runner：历史、待确认项、删除这些**读路径**都存在检查点里，
    跟模型没关系。换个空实现的 runner 会把它们一起弄丢——打开旧会话一片空白。
    """

    @property
    def _llm_type(self) -> str:
        return "unavailable"

    def bind_tools(self, tools: object, **kwargs: object) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> ChatResult:
        raise ModelUnavailable(NO_MODEL_REASON)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> ChatResult:
        raise ModelUnavailable(NO_MODEL_REASON)


def _brief(value: object, limit: int = 200) -> str:
    """日志用：压成一行并截断——工具参数与返回值可能是很长的字典或多行文本。"""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _unanswered_calls(messages: list) -> list[dict]:
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


def _error_message(call_id: str, text: str) -> ToolMessage:
    """
    给模型的失败回执。

    `status="error"` 是"失败"的唯一标记：前端卡片、历史恢复都读它。
    """
    return ToolMessage(content=text, tool_call_id=call_id, status="error")


def _to_history_call(call: dict, result: object) -> HistoryToolCall:
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
    text = _text_of(result)
    failed = getattr(result, "status", None) == "error"
    return HistoryToolCall(
        id=call["id"],
        name=call["name"],
        args=call.get("args") or {},
        state="failed" if failed else "ok",
        result=None if failed else text,
        error=text if failed else None,
    )


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
    逐个征求人工确认，然后并行执行通过的调用。

    不用 ToolNode 的原因：它会执行 AIMessage 里的全部调用（含已被回绝的），
    并产生重复 tool_call_id 的 ToolMessage，审批拒绝因此形同虚设。
    这里统一承担审批与执行，单一执行路径。

    审批仍然一个一个来：interrupt 的恢复值是按发生顺序回填的，一次只弹一张卡
    才谈得上"回答的是哪一个"。执行阶段则并行，见 _run_calls。
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

    token = _workspace_token(config)
    try:
        results = await _run_calls(calls, config, approved)
    finally:
        if token is not None:
            current_workspace.reset(token)

    return {"messages": results}


async def _invoke_call(
    call: dict, config: dict, approved: dict[str, bool]
) -> ToolMessage:
    """
    跑一次调用，把"没做成"收成带 error 标记的回执。

    除了中断，什么异常都在这里结束：模型照旧收到那句话，而失败由 status 标出来。
    """
    if not approved.get(call["id"], True):
        logger.warning("工具被拒绝 name=%s", call["name"])
        return ToolMessage(content=REJECT_MESSAGE, tool_call_id=call["id"])

    tool = TOOL_MAP.get(call["name"])
    if tool is None:
        logger.warning("模型要调未知工具 name=%s", call["name"])
        return _error_message(call["id"], f"未知工具：{call['name']}")

    try:
        output = await tool.ainvoke(call["args"], config=config)
    except GraphBubbleUp:
        # 这句不能删，也不能挪到 except Exception 后面：GraphBubbleUp 继承 Exception，
        # interrupt() 抛的就是它。被当成工具失败的话，工具内部的沙箱授权询问会静默
        # 退化成一条"工具执行失败"，很难查。
        raise
    except ToolException as exc:
        # 工具自己判定"没做成"（文件不存在、沙箱拒绝、命令非零退出……）。
        # 这句话模型照旧收到，但打上 error 标记，界面与历史才显示"失败"。
        # 不用 LangChain 的 handle_tool_error：它要靠运行时能取到 tool_call_id
        # 才会把 status 包进 ToolMessage，而这里是手写的执行路径。
        logger.warning("工具失败 name=%s：%s", call["name"], exc)
        return _error_message(call["id"], str(exc))
    except Exception as exc:
        # 其余异常（参数校验失败、工具内部 bug）：同样是失败，标出是异常
        logger.warning("工具异常 name=%s：%s", call["name"], exc)
        return _error_message(call["id"], f"工具执行失败：{exc}")
    return ToolMessage(content=str(output), tool_call_id=call["id"])


async def _run_calls(
    calls: list[dict], config: dict, approved: dict[str, bool]
) -> list[ToolMessage]:
    """
    同一轮的多个调用并行执行。

    并发的含义要看清：工具都是 async 的，重叠的是它们的**等待**（子进程、文件 IO），
    同步段照样只有一份——这是事件循环上的并发，不是把工具丢进线程池。

    第一个中断（或异常）冒头就取消其余调用：节点马上会被中断掀翻、整轮重跑，
    留着它们跑完只会产生没人认领的副作用（命令还在后台改工作区）。被取消的一方
    不会留下"失败"卡片——它没有结果，前端在收到确认卡片时把运行中的卡片收成未完成。

    不用 TaskGroup：它会把异常包进异常组，LangGraph 就认不出那个中断了。
    """
    happened: list[BaseException] = []

    async def run(call: dict) -> ToolMessage:
        try:
            return await _invoke_call(call, config, approved)
        except BaseException as exc:
            # 记"实际发生"的先后，_run_calls 结尾要用（为什么见那里）
            happened.append(exc)
            raise

    tasks = [asyncio.create_task(run(call)) for call in calls]
    try:
        _, running = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except BaseException:
        # 本轮自己被取消（用户停止、连接断开）：连子任务一起收干净，别留下还在跑的进程
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    for task in running:
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)

    results: list[ToolMessage] = []
    for task in tasks:
        if task.cancelled():
            continue
        # 每个任务的异常都要取一次：不取 asyncio 会在回收时报 "never retrieved"
        if task.exception() is None:
            results.append(task.result())

    if happened:
        # 抛**最早发生**的那个，不能按调用顺序挑：LangGraph 给一个 task 里的多次
        # interrupt 编号，就是按发生先后，恢复值也按这个编号回填。抛错一个，
        # 用户对这张卡片的回答就会落到另一个调用手里（实测见 LESSONS）。
        raise happened[0]
    return results


def _build_model() -> BaseChatModel:
    """没配 key 就用占位模型：读路径照常，只有真去调模型时才报错。"""
    if not LLM_API_KEY:
        return _UnavailableModel()
    return ChatOpenAI(
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        temperature=0,
        streaming=True,  # 关掉流式就没有 text_delta，前端只能等整段回复
    )


def build_graph(checkpointer: BaseCheckpointSaver, model: BaseChatModel | None = None):
    """
    agent ↔ tools 循环，checkpointer 负责 thread_id 维度的多轮状态。

    `tools_condition` 按最后一条 AI 消息里有没有 tool_calls 分流：有就去 tools 执行，
    没有就结束（END）。工具结果以 ToolMessage 回到消息列表，再从 agent 走一遍。
    """
    model = model or _build_model()
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
        """
        从状态里重建会话历史。

        工具调用不是另存一份记录，而是**从消息本身推出来**：`AIMessage.tool_calls` 是调用，
        按 `tool_call_id` 配对的 `ToolMessage` 是结果——两边都在检查点里。
        模型先调工具、再给结论，所以卡片挂在后面那条有正文的 assistant 消息上。
        """
        snapshot = await self.graph.aget_state(self._config(thread_id))
        messages = snapshot.values.get("messages", [])
        results = {
            message.tool_call_id: message
            for message in messages
            if isinstance(message, ToolMessage)
        }

        out: list[HistoryMessage] = []
        pending_calls: list[HistoryToolCall] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                out.append(
                    HistoryMessage(
                        role="user",
                        content=_text_of(message),
                        id=getattr(message, "id", None),
                    )
                )
                continue
            if not isinstance(message, AIMessage):
                # 工具消息不进正文：它已经以卡片形式挂在调用的那条 assistant 消息上了
                continue

            pending_calls.extend(
                _to_history_call(call, results.get(call["id"]))
                for call in message.tool_calls
            )
            text = _text_of(message)
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

        calls = sum(len(item.tool_calls) for item in out)
        logger.debug("读取历史 thread=%s 消息=%d 工具调用=%d", thread_id, len(out), calls)
        return out

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
        return bool(_unanswered_calls(snapshot.values.get("messages", [])))

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

        calls = _unanswered_calls(snapshot.values.get("messages", []))
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
                            id=event["run_id"], ok=False, error=_brief(str(error))
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
