"""
图的组装：状态、节点、边。

agent ↔ tools 循环，外加"每次进 agent 之前先看水位"的压缩检查。节点都很薄——
真正的活儿在 `tool_calls` / `compaction.runtime` / `messages` 里，这里只负责把它们接到
LangGraph 上，以及处理只有图才知道的事（superstep 上限）。
"""

import logging

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition

from app.agent.compaction.policy import with_summary
from app.agent.compaction.runtime import COMPACT_NODE, compact_once
from app.agent.messages import usable_compaction
from app.agent.models import build_model, build_summary_model
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tool_calls import run_tools
from app.agent.tools import TOOLS

logger = logging.getLogger(__name__)

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


def log_step_limit(thread_id: str, exc: Exception) -> None:
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
    model = model or build_model()
    model_with_tools = model.bind_tools(TOOLS)
    summary_model = summary_model or build_summary_model()

    async def compact_context(state: AgentState) -> dict:
        """超水位就把中段摘要掉；不超（或压不动）就原样放行，返回空更新。"""
        thread_id = (get_config().get("configurable") or {}).get("thread_id") or "-"
        return await compact_once(
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
        if usable_compaction(compaction, len(messages)):
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
    builder.add_node("tools", run_tools)
    builder.add_node(COMPACT_NODE, compact_context)
    builder.add_edge(START, COMPACT_NODE)
    builder.add_edge(COMPACT_NODE, "agent")
    builder.add_conditional_edges(
        "agent", tools_condition, {"tools": "tools", END: END}
    )
    builder.add_edge("tools", COMPACT_NODE)
    return builder.compile(checkpointer=checkpointer)
