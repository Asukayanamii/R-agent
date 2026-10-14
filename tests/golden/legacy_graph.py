"""改造前的图实现快照——**只用于 golden 对照，不再随主代码演进**。

逐行抄自阶段 B 之前的 `app/agent/graph.py` 与 `app/agent/tool_calls.py`（当时的实现），
作用是给"新旧实现事件序列逐帧一致"提供一个冻结的参照物：新实现（合成器 + builtin/loop）
跑出来的每一帧都要和它一致。**不要**在这里修改行为逻辑；它就该停在原地——
包括这里自带的一份工具执行实现（模块级注册表 + 审批名单），不 import 活代码。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_config
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import tools_condition
from langgraph.types import interrupt

from app.agent.compaction.policy import with_summary
from app.agent.compaction.runtime import COMPACT_NODE, compact_once
from app.agent.messages import error_message, text_of, usable_compaction
from app.plugins.builtin.model import build_model, build_summary_model
from app.agent.prompts import build_system_prompt
from app.plugins.builtin.retry import ainvoke_with_retry
from app.agent.runtime import current_workspace
from app.agent.tools import TOOLS

logger = logging.getLogger(__name__)

LEGACY_TOOL_MAP = {tool.name: tool for tool in TOOLS}
LEGACY_APPROVAL_REQUIRED: set[str] = set()
LEGACY_REJECT_MESSAGE = "用户拒绝执行该操作。"


def _legacy_workspace_token(config: dict):
    workspace = (config.get("configurable") or {}).get("workspace")
    if not workspace:
        return None
    return current_workspace.set(workspace)


async def legacy_invoke_call(call: dict, config: dict, approved: dict[str, bool]) -> ToolMessage:
    if not approved.get(call["id"], True):
        return ToolMessage(content=LEGACY_REJECT_MESSAGE, tool_call_id=call["id"])

    tool = LEGACY_TOOL_MAP.get(call["name"])
    if tool is None:
        return error_message(call["id"], f"未知工具：{call['name']}")

    try:
        output = await tool.ainvoke(
            {"name": call["name"], "args": call["args"], "id": call["id"], "type": "tool_call"},
            config=config,
        )
    except GraphBubbleUp:
        raise
    except ToolException as exc:
        return error_message(call["id"], str(exc))
    except Exception as exc:
        return error_message(call["id"], f"工具执行失败：{exc}")
    if isinstance(output, ToolMessage):
        return output
    return ToolMessage(content=text_of(output), tool_call_id=call["id"])


async def legacy_run_calls(
    calls: list[dict], config: dict, approved: dict[str, bool]
) -> list[ToolMessage]:
    happened: list[BaseException] = []

    async def run(call: dict) -> ToolMessage:
        try:
            return await legacy_invoke_call(call, config, approved)
        except BaseException as exc:
            happened.append(exc)
            raise

    tasks = [asyncio.create_task(run(call)) for call in calls]
    try:
        _, running = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    except BaseException:
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
        if task.exception() is None:
            results.append(task.result())

    if happened:
        raise happened[0]
    return results


async def legacy_run_tools(state: MessagesState):
    config = get_config()
    calls = state["messages"][-1].tool_calls

    approved: dict[str, bool] = {}
    for call in calls:
        if call["name"] in LEGACY_APPROVAL_REQUIRED:
            decision = interrupt(
                {
                    "prompt": f"请求调用 {call['name']}，参数 {call['args']}",
                    "options": ["确认", "取消"],
                }
            )
            approved[call["id"]] = decision == "确认"
            logger.info("工具审批 name=%s 决定=%s", call["name"], decision)

    token = _legacy_workspace_token(config)
    try:
        results = await legacy_run_calls(calls, config, approved)
    finally:
        if token is not None:
            current_workspace.reset(token)

    return {"messages": results}


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
    builder.add_node("tools", legacy_run_tools)
    builder.add_node(COMPACT_NODE, compact_context)
    builder.add_edge(START, COMPACT_NODE)
    builder.add_edge(COMPACT_NODE, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    builder.add_edge("tools", COMPACT_NODE)
    return builder.compile(checkpointer=checkpointer)
