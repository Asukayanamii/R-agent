"""待确认中断的"可见性"，与结构变更后的报错（实测修复的用例）。

实测发现两件事，各钉一条：

1. `StateSnapshot.tasks/interrupts` 是 LangGraph 用**读取方自己的节点表**重建的——
   中断落在片段节点上时，全局图会把它读成"没有"（带工作区选对图才读得到）。
   所以 `is_interrupted` / `pending_interrupts` 必须带 `workspace`，`chat_service` 已如此调用。
2. 中断所在节点被结构变更摘掉后，图读不出它，`resume` 不能含糊成"没有待确认"——
   要按原始检查点的 `__interrupt__` 残留写报"结构已变更"，且会话本体不受影响。
"""

import json

import pytest
from langchain_core.messages import AIMessage

from test_self_edit import USAGE, begin_turn, make_wired

pytestmark = pytest.mark.anyio

TRAP_PY = '''\
from langgraph.types import interrupt


async def trap_node(state):
    interrupt({'prompt': 'trap 询问', 'options': ['继续', '取消']})
    return {}


def apply(ctx):
    ctx.provide('trap', trap_node)


plugin = {'apply': apply, 'name': 'ws/trap'}
'''
TRAP_JSON = {
    'id': 'workspace/trap',
    'anchor': 'pre_model',
    'chain': [{'name': 'trap_node', 'interface': 'trap'}],
}


async def mount_trap(libraries, workspace) -> None:
    """在工作区插件库里放一个"在 pre_model 上必然 interrupt"的片段。"""
    directory = workspace / '.r-agent' / 'plugins'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'trap.py').write_text(TRAP_PY, encoding='utf-8')
    (directory / 'trap.json').write_text(
        json.dumps(TRAP_JSON, ensure_ascii=False), encoding='utf-8'
    )
    await libraries.trust(str(workspace), 'trap.py')
    await libraries.trust(str(workspace), 'trap.json')


async def make_runner(runtime):
    """协议服务就位后包一个 runner（与 test_self_edit 的做法一致）。"""
    from app.agent.langgraph_runner import LangGraphRunner
    from app.plugins.builtin import protocol as protocol_plugin

    await runtime.ctx.plugin(protocol_plugin.plugin, essential=True)
    return LangGraphRunner(runtime, runtime.ctx.get('protocol'))


def error_messages(frames) -> list[str]:
    return [
        item.model_dump()['data']['message'] for item in frames if item.type == 'error'
    ]


async def test_interrupt_on_fragment_node_needs_workspace_to_read(tmp_path):
    """片段节点上的中断：带工作区读得到；不带（全局图）读不到——把读取图的边界钉住。"""
    script = [AIMessage(content='好', usage_metadata=USAGE)]
    runtime, libraries, workspace = await make_wired(tmp_path, script)
    await mount_trap(libraries, workspace)
    await begin_turn(runtime, workspace)
    runner = await make_runner(runtime)

    frames = [item async for item in runner.stream('t1', '触发', workspace=str(workspace))]
    assert any(item.type == 'interrupt' for item in frames)

    assert await runner.is_interrupted('t1', str(workspace)) is True
    assert len(await runner.pending_interrupts('t1', str(workspace))) == 1
    # 对照：不带工作区 = 拿全局图读。任务按**读取图**的节点表重建，片段节点不在全局图里，
    # 中断就会被读成"没有"——这正是调用方必须把工作区带上的原因。
    assert await runner.is_interrupted('t1') is False


async def test_resume_after_interrupt_node_removed_reports_structure_change(tmp_path):
    """节点被摘掉后 resume：明确报"结构已变更"而不是"没有待确认"；会话本体不受影响。"""
    script = [AIMessage(content='好', usage_metadata=USAGE)]
    runtime, libraries, workspace = await make_wired(tmp_path, script)
    await mount_trap(libraries, workspace)
    await begin_turn(runtime, workspace)
    runner = await make_runner(runtime)

    frames = [item async for item in runner.stream('t1', '触发', workspace=str(workspace))]
    assert any(item.type == 'interrupt' for item in frames)

    await libraries.untrust(str(workspace), 'trap.json')
    await begin_turn(runtime, workspace)

    frames = [item async for item in runner.resume('t1', '继续', workspace=str(workspace))]
    messages = error_messages(frames)
    assert any('结构已变更' in message for message in messages), messages
    assert not any('无需 resume' in message for message in messages), messages

    # 会话没卡：发新消息照常走一轮；之后的 resume 回到"无需 resume"——
    # 残留写在旧的检查点层里，新的一轮把它甩在后面，不该被继续误报。
    frames = [item async for item in runner.stream('t1', '还在吗', workspace=str(workspace))]
    assert not any(item.type == 'error' for item in frames)

    frames = [item async for item in runner.resume('t1', '继续', workspace=str(workspace))]
    messages = error_messages(frames)
    assert any('无需 resume' in message for message in messages), messages
