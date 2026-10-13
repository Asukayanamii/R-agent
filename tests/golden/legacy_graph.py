"""改造前的图实现快照——**只用于 golden 对照，不再随主代码演进**。

逐行抄自阶段 B 之前的 `app/agent/graph.py`（build_graph 当时的实现），作用是给
"新旧实现事件序列逐帧一致"提供一个冻结的参照物：新实现（合成器 + builtin/loop）
跑出来的每一帧都要和它一致。**不要**在这里修改行为逻辑；它就该停在原地。
"""

from __future__ import annotations

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
from app.agent.prompts import build_system_prompt
from app.agent.retry import ainvoke_with_retry
from app.agent.tool_calls import run_tools
from app.agent.tools import TOOLS

logger = logging.getLogger(__name__)


class LegacyState(MessagesState):
    compaction: dict | None
    usage_anchor: dict | None


def legacy_build_graph(
    checkpointer: BaseCheckpointSaver,
    model: BaseChatModel | None = None,
    summary_model: BaseChatModel | None = None,
):
    model = model or build_model()
    model_with_tools = model.bind_tools(TOOLS)
    summary_model = summary_model or build_summary_model()

    async def compact_context(state: LegacyState) -> dict:
        config = get_config()
        thread_id = (config.get("configurable") or {}).get("thread_id") or "-"
        return await compact_once(
            state,
            summary_model=summary_model,
            thread_id=thread_id,
            config=config,
        )

    async def call_model(state: LegacyState):
        messages = state["messages"]
        compaction = state.get("compaction") or {}
        config = get_config()
        prompt = build_system_prompt((config.get("configurable") or {}).get("workspace"))
        if usable_compaction(compaction, len(messages)):
            prompt = with_summary(prompt, compaction["summary"])
            messages = [messages[0], *messages[compaction["from_index"] :]]
        elif compaction:
            logger.warning(
                "压缩锚点越界，本次按全量发送 from_index=%s 共%d条",
                compaction.get("from_index"),
                len(messages),
            )

        response = await ainvoke_with_retry(
            model_with_tools,
            [{"role": "system", "content": prompt}, *messages],
            config=config,
            thread_id=(config.get("configurable") or {}).get("thread_id") or "-",
            label="模型调用",
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

    builder = StateGraph(LegacyState)
    builder.add_node("agent", call_model)
    builder.add_node("tools", run_tools)
    builder.add_node(COMPACT_NODE, compact_context)
    builder.add_edge(START, COMPACT_NODE)
    builder.add_edge(COMPACT_NODE, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    builder.add_edge("tools", COMPACT_NODE)
    return builder.compile(checkpointer=checkpointer)
