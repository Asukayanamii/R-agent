"""
工具执行：人工审批、并行调用、失败与中断的收口。

不用 LangGraph 自带的 `ToolNode`，原因见 `run_tools` 的说明。执行语义都在这一个文件里：

- 工具"没做成"（`common.fail()` 抛 `ToolException`）→ 带 `status=error` 的回执，模型照旧收到原话
- 中断（工具内部的沙箱授权询问）→ **原样往上抛**，让节点停下来等人工确认
- 同一轮的多个调用并行跑，第一个中断/异常出现就取消其余
"""

import asyncio
import logging
from pathlib import Path

from langchain_core.messages import ToolMessage
from langchain_core.tools import ToolException
from langgraph.config import get_config
from langgraph.errors import GraphBubbleUp
from langgraph.graph import MessagesState
from langgraph.types import interrupt

from app.agent.messages import error_message
from app.agent.runtime import current_workspace
from app.agent.tools import APPROVAL_REQUIRED, TOOLS

logger = logging.getLogger(__name__)

TOOL_MAP = {tool.name: tool for tool in TOOLS}

REJECT_MESSAGE = "用户拒绝执行该操作。"


def _workspace_token(config: dict):
    """
    把本次运行的工作区放进 ContextVar，供沙箱判定信任边界。

    RunnableConfig 是 LangGraph 给"每次调用的参数"准备的位置，不自造 state 字段。
    """
    workspace = (config.get("configurable") or {}).get("workspace")
    if not workspace:
        return None
    return current_workspace.set(Path(workspace))


async def run_tools(state: MessagesState):
    """
    逐个征求人工确认，然后并行执行通过的调用。

    不用 ToolNode 的原因：它会执行 AIMessage 里的全部调用（含已被回绝的），
    并产生重复 tool_call_id 的 ToolMessage，审批拒绝因此形同虚设。
    这里统一承担审批与执行，单一执行路径。

    审批仍然一个一个来：interrupt 的恢复值是按发生顺序回填的，一次只弹一张卡
    才谈得上"回答的是哪一个"。执行阶段则并行，见 run_calls。
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
        results = await run_calls(calls, config, approved)
    finally:
        if token is not None:
            current_workspace.reset(token)

    return {"messages": results}


async def invoke_call(
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
        return error_message(call["id"], f"未知工具：{call['name']}")

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
        return error_message(call["id"], str(exc))
    except Exception as exc:
        # 其余异常（参数校验失败、工具内部 bug）：同样是失败，标出是异常
        logger.warning("工具异常 name=%s：%s", call["name"], exc)
        return error_message(call["id"], f"工具执行失败：{exc}")
    return ToolMessage(content=str(output), tool_call_id=call["id"])


async def run_calls(
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
            return await invoke_call(call, config, approved)
        except BaseException as exc:
            # 记"实际发生"的先后，run_calls 结尾要用（为什么见那里）
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
