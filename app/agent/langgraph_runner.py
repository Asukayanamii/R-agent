"""
基于 LangGraph 的 Agent 实现。

把 LangGraph 的 astream_events 翻译成本项目的统一事件协议，
上层路由与前端不感知 LangGraph 的存在。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
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
from langgraph.errors import GraphBubbleUp, GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.types import Command, interrupt
from pydantic import BaseModel

from app.agent.context import (
    TAG as COMPACT_TAG,
    estimate,
    estimate_view,
    in_cooldown,
    message_tokens,
    note_failure,
    note_success,
    plan_cut,
    rough_tokens,
    serialize,
    summary_block_tokens,
    summary_input_chars,
    summary_max_tokens,
    summary_request,
    trim_middle,
    with_summary,
)
from app.agent.context import text_of as _text_of
from app.agent.runtime import current_workspace
from app.agent.tools import APPROVAL_REQUIRED, TOOLS
from app.config import (
    COMPACT_AT,
    COMPACT_ENABLED,
    COMPACT_KEEP_TOKENS,
    COMPACT_MODEL,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_CONTEXT_WINDOW,
    LLM_MODEL,
)
from app.exceptions import ModelUnavailable
from app.event.events import (
    CompactData,
    CompactEvent,
    CompactionInfo,
    ErrorData,
    ErrorEvent,
    HistoryMessage,
    HistoryToolCall,
    HistoryView,
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
    "回答用中文，简洁准确。\n"
    "\n"
    "工具选择：文件操作的活儿优先用专用工具，它们带行号、分页与截断保护，输出也更规整——\n"
    "- 读文件用 read，别用 cat / head / tail / sed -n\n"
    "- 按内容搜用 grep，按名字找用 find，看目录用 ls；别拿 bash 里的同名命令代替，"
    "更别把几条拼成一行（多件事的输出会混在一起，也没法分页）\n"
    "- 新建或整体覆盖用 write，改动片段用 edit（精确替换）；别用 echo >/tee/sed -i\n"
    "- bash 留给它真正擅长的：跑测试与构建、git、装依赖、进程与服务，"
    "以及需要管道或多步组合的活儿\n"
)

TOOL_MAP = {tool.name: tool for tool in TOOLS}

REJECT_MESSAGE = "用户拒绝执行该操作。"

INTERRUPTED_MESSAGE = "这一轮被中断，未取得结果。"

COMPACT_NODE = "compact"
"""压缩节点的名字。`_run` 靠 on_chain_end 里的这个节点名认出"本轮写了压缩记录"，
写成常量，改名时两边一起改——不然压缩事件会静默消失。"""

RECURSION_LIMIT = 10_000
"""单轮图的 superstep 上限（LangGraph 的 recursion_limit）。

LangGraph 逼着必须给一个有限值（默认只有 25），但这个值不该成为"复杂任务跑一半挂掉"的原因：

- 我们的图一个工具回合要花 3 步（`tools → compact → agent`），25 步只够 7 个回合；
- 主流 coding agent 基本不设上限（Pi 的 agent loop 是 `while (true)`，只在模型不再调工具/
  出错/用户取消时退出；Codex CLI 同理；Cline 把 max requests 直接删了）；
- LangGraph 自己新版默认已是 10007，`create_agent` 用 9999。

所以取 10000（约 3300 个工具回合），当成"真出现死循环时最后兜底"，而不是日常约束。
**真正的停止手段是用户点停止**（与手动停止走同一条收尾路径）。
"""

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


def _log_step_limit(thread_id: str, exc: Exception) -> None:
    """
    撞上 superstep 上限时补一条日志。

    这个上限高到正常任务碰不到（见 RECURSION_LIMIT），真撞上基本就是死循环：
    错误本身照旧原样上抛，日志里说清"多少步、可能是循环"，别让人对着一个数字发愣。
    """
    logger.warning(
        "本轮撞上图的 superstep 上限 thread=%s：%s（上限 %d，"
        "正常任务碰不到——多半是工具反复失败把模型困在循环里）",
        thread_id,
        exc,
        RECURSION_LIMIT,
    )


class AgentState(MessagesState):
    """
    图状态 = 消息 + 压缩记录 + 用量锚点。

    `compaction` 是"发给模型的视图"与"库里存的原文"之间唯一的分界；
    **消息列表本身永远不删**——历史恢复、工具卡片、悬空调用修复都靠它。
    """

    compaction: dict | None
    """{summary, from_index, tokens_before, tokens_after, count, at}；没压过时没有这个键。"""

    usage_anchor: dict | None
    """{tokens, upto}：上次请求 provider 实报的 input tokens + 那时覆盖到第几条消息。"""


def _usable_compaction(compaction: dict, total: int) -> bool:
    """
    这条压缩记录还能不能用。

    锚点是消息列表里的下标：越界就**忽略压缩、发全量**并打 WARNING——退化方向必须是
    "多花 token"，而不是拿一条错位的历史去发请求。
    """
    index = compaction.get("from_index")
    return isinstance(index, int) and 1 <= index < total


class _SummaryModel(ChatOpenAI):
    """
    摘要模型：把输出上限用 `max_tokens` 发出去。

    摘要的长度必须由外部卡住（接口参数），不能只写进提示词里——模型可以不理，
    而且那句"别太长"本身也占注意力。

    这版 langchain 会在 `ChatOpenAI._get_request_payload` 里把 `max_tokens` 改名成
    `max_completion_tokens`（跟着 OpenAI 的新命名走）。本项目面向的是**任意 OpenAI 兼容
    端点**，DeepSeek 这类只认 `max_tokens`：改名等于上限没设上，老端点还可能直接 400。
    所以这里把名字改回兼容的那一个（实测见 LESSONS）。
    """

    def _get_request_payload(
        self, input_: object, *, stop: list[str] | None = None, **kwargs: object
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload


def _build_model(name: str | None = None, max_tokens: int | None = None) -> BaseChatModel:
    """没配 key 就用占位模型：读路径照常，只有真去调模型时才报错。

    `max_tokens` 只有摘要模型用：卡住摘要长度，别让一次压缩写回比原文还长的东西。
    """
    if not LLM_API_KEY:
        return _UnavailableModel()
    model_class = _SummaryModel if max_tokens else ChatOpenAI
    return model_class(
        model=name or LLM_MODEL,
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        temperature=0,
        max_tokens=max_tokens,
        streaming=True,  # 关掉流式就没有 text_delta，前端只能等整段回复
    )


def _build_summary_model() -> BaseChatModel:
    """
    摘要模型单独建一个实例：它要带 `max_tokens` 卡住输出长度（见 `_SummaryModel`），
    主模型不该带这个限制。`COMPACT_MODEL` 留空就是同一个模型，只是换了个实例。
    """
    return _build_model(COMPACT_MODEL or None, summary_max_tokens(LLM_CONTEXT_WINDOW))


def _compaction_floor(state: dict) -> int:
    """
    上次压缩的切点：本次只摘要它之后的内容。

    已经摘要过的部分仍留在消息列表里（我们不删消息），但**不能再送进摘要器**——否则同一段
    内容会被反复摘要，摘要被越改越长。实测踩到过：两次切点相同的压缩把视图从 439 推到 463，
    等于白花一次调用还更占上下文。Pi 的规则也是从上次的保留边界开始。
    """
    compaction = state.get("compaction") or {}
    index = compaction.get("from_index")
    messages = state.get("messages") or []
    if isinstance(index, int) and 1 <= index < len(messages):
        return index
    return 1


async def _compact_once(
    state: dict, *, summary_model: BaseChatModel, thread_id: str, force: bool = False
) -> dict:
    """
    跑一次压缩判定与摘要，返回状态更新；没压就返回空字典。

    图里的 compact 节点与"用户主动压缩"都走这里，判定只写一遍。
    `force=True`（手动触发）跳过阈值、开关与冷却：用户点了就是要压，跟自动那套闸门无关。

    估算、切点、序列化、提示词都在 app/agent/context.py，这里只管时序、日志与状态写入。
    """
    if not force and not COMPACT_ENABLED:
        return {}

    messages = state["messages"]
    trigger = int(LLM_CONTEXT_WINDOW * COMPACT_AT)
    before = estimate(messages, state.get("usage_anchor"))
    if not state.get("usage_anchor"):
        # 没有锚点时粗估的是"消息"，而 system 提示词也是要发出去的：不补上它，
        # 后面的净收益校验就是拿"不含 system 的旧视图"比"含 system 的新视图"，
        # 短会话会被永远判成"没收益"（实测踩到过）。
        before += rough_tokens(SYSTEM_PROMPT)
    if not force and before < trigger:
        logger.debug("未到压缩线 thread=%s 估算=%d 触发线=%d", thread_id, before, trigger)
        return {}
    if not force and in_cooldown(thread_id):
        logger.debug("压缩在冷却中，跳过 thread=%s", thread_id)
        return {}

    floor = _compaction_floor(state)
    cut = plan_cut(messages, COMPACT_KEEP_TOKENS, floor=floor)
    if cut is None:
        logger.debug("没有值得摘要的中段 thread=%s 共%d条", thread_id, len(messages))
        return {}

    previous = (state.get("compaction") or {}).get("summary", "")
    body = trim_middle(
        serialize(messages[floor:cut]), summary_input_chars(LLM_CONTEXT_WINDOW)
    )
    started = monotonic()
    try:
        response = await summary_model.ainvoke(
            summary_request(previous, body),
            config={"tags": [COMPACT_TAG]},
        )
    except Exception as exc:
        # 压不动不是会话的错：本轮照旧发全量，退避之后再试
        note_failure(thread_id)
        logger.warning("摘要失败，本轮不压缩 thread=%s：%s", thread_id, exc)
        return {}

    summary = _text_of(response).strip()
    # 净收益校验用同口径比较：被替换掉的中段 vs 顶替它的摘要块。拿"provider 实报的前值"
    # 去比"粗估的后值"会因两边误差方向不同而误判（实测：CJK 内容下会把划算的压缩判成不划算）。
    replaced = sum(message_tokens(item) for item in messages[floor:cut])
    if not summary or summary_block_tokens(summary) >= replaced:
        note_failure(thread_id)
        logger.warning(
            "摘要没带来净收益，不写入 thread=%s 中段=%d 摘要块=%d",
            thread_id,
            replaced,
            summary_block_tokens(summary),
        )
        return {}

    kept = [messages[0], *messages[cut:]]
    after = estimate_view(SYSTEM_PROMPT, summary, kept)

    note_success(thread_id)
    count = (state.get("compaction") or {}).get("count", 0) + 1
    logger.info(
        "上下文已压缩 thread=%s 第%d次（%s）估算 %d → %d tokens，"
        "保留首条与第 %d 条起（共 %d 条），用时 %.2fs",
        thread_id,
        count,
        "手动" if force else "自动",
        before,
        after,
        cut,
        len(messages),
        monotonic() - started,
    )
    return {
        "compaction": {
            "summary": summary,
            "from_index": cut,
            "tokens_before": before,
            "tokens_after": after,
            "count": count,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        # 锚点按新视图重置：否则这一轮要是被中断、resume 回来重跑 compact，
        # 会拿着压缩前的旧数字又压一次（同一份内容压两遍，白花钱）。
        "usage_anchor": {"tokens": after, "upto": len(messages)},
    }


def build_graph(
    checkpointer: BaseCheckpointSaver,
    model: BaseChatModel | None = None,
    summary_model: BaseChatModel | None = None,
):
    """
    agent ↔ tools 循环 + 每次进 agent 前的压缩检查，checkpointer 负责 thread_id 维度的状态。

    `tools_condition` 按最后一条 AI 消息里有没有 tool_calls 分流：有就去 tools 执行，
    没有就结束（END）。工具结果以 ToolMessage 回到消息列表，再从 agent 走一遍。

    `compact` 有两条入边（START 与 tools）：**每次模型调用之前**都查一次水位。只接在 tools
    后面的话，不调工具的轮次永远不被审查，纯聊天会话会一路涨到超窗。resume 从被中断的那个
    节点继续、不重跑 START，所以同一份历史不会被压两次。
    """
    model = model or _build_model()
    model_with_tools = model.bind_tools(TOOLS)
    summary_model = summary_model or _build_summary_model()

    async def compact_context(state: AgentState) -> dict:
        """超水位就把中段摘要掉；不超（或压不动）就原样放行，返回空更新。"""
        thread_id = (get_config().get("configurable") or {}).get("thread_id") or "-"
        return await _compact_once(
            state, summary_model=summary_model, thread_id=thread_id
        )

    async def call_model(state: AgentState):
        """
        节点本身无状态：每轮都把 system prompt 重新拼在历史前面，历史来自 checkpointer。

        压缩过的会话发的是"摘要 + 首条用户消息 + 切点起的原文"（只压视图，原文还在检查点里）。
        `usage_anchor` 记的是这次请求 provider 实报的 input tokens 与"覆盖到第几条**状态里的**
        消息"——之后追加多少条，下一轮就只粗估那几条。
        """
        messages = state["messages"]
        compaction = state.get("compaction") or {}
        prompt = SYSTEM_PROMPT
        if _usable_compaction(compaction, len(messages)):
            prompt = with_summary(SYSTEM_PROMPT, compaction["summary"])
            messages = [messages[0], *messages[compaction["from_index"] :]]
        elif compaction:
            logger.warning(
                "压缩锚点越界，本次按全量发送 from_index=%s 共%d条",
                compaction.get("from_index"),
                len(messages),
            )

        response = await model_with_tools.ainvoke(
            [{"role": "system", "content": prompt}, *messages]
        )
        meta = getattr(response, "usage_metadata", None) or {}
        if not meta:
            return {"messages": [response]}
        return {
            "messages": [response],
            "usage_anchor": {
                "tokens": meta.get("input_tokens", 0),
                "upto": len(state["messages"]),
            },
        }

    builder = StateGraph(AgentState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", _run_tools)
    builder.add_node(COMPACT_NODE, compact_context)
    builder.add_edge(START, COMPACT_NODE)
    builder.add_edge(COMPACT_NODE, "agent")
    builder.add_conditional_edges(
        "agent", tools_condition, {"tools": "tools", END: END}
    )
    builder.add_edge("tools", COMPACT_NODE)
    return builder.compile(checkpointer=checkpointer)


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
        self.summary_model = summary_model or _build_summary_model()
        self.graph = build_graph(checkpointer, model, self.summary_model)

    @staticmethod
    def _config(thread_id: str, workspace: str | None = None) -> dict:
        configurable: dict = {"thread_id": thread_id}
        if workspace:
            configurable["workspace"] = workspace
        # 图的 superstep 上限：LangGraph 默认 25 步，而一个工具回合要花 3 步
        # （tools → compact → agent），默认值只够 7 个回合。取值理由见 RECURSION_LIMIT。
        return {"configurable": configurable, "recursion_limit": RECURSION_LIMIT}

    async def stream(
        self, thread_id: str, message: str, workspace: str | None = None
    ) -> AsyncIterator[BaseModel]:
        inputs = {"messages": [HumanMessage(content=message)]}
        try:
            async for event in self._run(thread_id, workspace, inputs):
                yield event
        except GraphRecursionError as exc:
            _log_step_limit(thread_id, exc)
            raise

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
        try:
            async for event in self._run(thread_id, workspace, Command(resume=value)):
                yield event
        except GraphRecursionError as exc:
            _log_step_limit(thread_id, exc)
            raise

    async def compact(self, thread_id: str) -> CompactionInfo | None:
        """
        用户主动压缩一次（等价 Claude Code 的 `/compact`）。

        与自动压缩共用 `_compact_once`，区别只有 force：跳过阈值、开关与冷却——用户点了
        就是要压。压完就结束，不顺带调主模型（回答留给下一次提问），省一次没必要的开销。

        状态写回用 `as_node=COMPACT_NODE`：图的"下一步"因此是 agent，与自动压缩跑完时一致。
        返回的 `before` 由 `history()` 现算，保证与重开会话时的分隔线位置同一套规则。

        没得压（会话还短、中段不足两条）返回 None，由上层说明原因，不静默通过。
        """
        config = self._config(thread_id)
        snapshot = await self.graph.aget_state(config)
        if not snapshot.values:
            return None

        update = await _compact_once(
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
        从状态里重建会话历史。

        工具调用不是另存一份记录，而是**从消息本身推出来**：`AIMessage.tool_calls` 是调用，
        按 `tool_call_id` 配对的 `ToolMessage` 是结果——两边都在检查点里。
        模型先调工具、再给结论，所以卡片挂在后面那条有正文的 assistant 消息上。

        压缩只改"发给模型的视图"，所以这里的消息一条不少；额外给出压缩分界的位置，
        前端据此插一条分隔线——用户仍然看得到被摘要掉的原文。
        """
        snapshot = await self.graph.aget_state(self._config(thread_id))
        messages = snapshot.values.get("messages", [])
        results = {
            message.tool_call_id: message
            for message in messages
            if isinstance(message, ToolMessage)
        }
        compaction = snapshot.values.get("compaction") or {}
        boundary = (
            compaction.get("from_index")
            if _usable_compaction(compaction, len(messages))
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
        logger.debug(
            "读取历史 thread=%s 消息=%d 工具调用=%d 压缩次数=%s",
            thread_id,
            len(out),
            calls,
            compaction.get("count", 0),
        )
        return HistoryView(messages=out, compaction=info)

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

        翻译表：`on_chat_model_stream` → text_delta、`on_tool_start/end` → tool_start/end、
        compact 节点跑完 → compact。一轮里模型可能被调用多次（每次工具返回后都要再问一次），
        所以 usage 要**累加**，message_id 取最后一次的。中断不在事件流里（见 LESSONS），
        跑完从状态快照读。

        摘要调用（tags 里带 `compact`，见 app/agent/context.py）不算"模型的回答"：它的文本
        要挡在前端之外，usage 照常计入本轮——那笔钱是真实花掉的。
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
            tagged_compact = COMPACT_TAG in (event.get("tags") or [])

            if kind == "on_chat_model_stream":
                if tagged_compact:
                    continue
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
