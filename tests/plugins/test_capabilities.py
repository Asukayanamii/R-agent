"""判据①（阶段 D）：加一个工具不需要改代码、更不需要重建图——下一次模型调用就能用。"""

import itertools

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.harness.runtime import HarnessRuntime
from app.kernel import Context
from app.plugins.builtin import loop as loop_plugin
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.builtin.protocol.mapping import translate_events

pytestmark = pytest.mark.anyio


@tool
async def probe_extra(value: str) -> str:
    """运行时加进来的探针工具。"""
    return f'extra:{value}'


def make_recording_model(script, seen):
    """记录每次 bind_tools 收到的工具名——判据①看的就是这份记录。"""
    cursor = itertools.count()

    class RecordingModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return 'recording'

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            seen['bound'].append(sorted(item.name for item in tools))
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise NotImplementedError

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            index = min(next(cursor), len(script) - 1)
            return ChatResult(generations=[ChatGeneration(message=script[index])])

    return RecordingModel()


async def run_turn(runtime, thread_id: str, message: str):
    async with runtime.lease(thread_id) as version:
        async for _ in translate_events(
            version.graph,
            thread_id=thread_id,
            config={'configurable': {'thread_id': thread_id}},
            inputs={'messages': [HumanMessage(message)]},
        ):
            pass
        return await version.graph.aget_state({'configurable': {'thread_id': thread_id}})


def make_runtime(ctx, script, seen) -> HarnessRuntime:
    return HarnessRuntime(
        ctx,
        checkpointer=InMemorySaver(),
        model=make_recording_model(script, seen),
        summary_model=make_recording_model(script, seen),
        base=loop_plugin.plugin,
    )


async def test_new_tool_reaches_next_model_call_without_rebuild():
    seen = {'bound': []}
    usage = {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}
    script = [AIMessage(content='好的', usage_metadata=usage)]
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    runtime = make_runtime(ctx, script, seen)
    await runtime.setup()

    before = runtime.versions.current
    await run_turn(runtime, 't1', '第一轮')
    assert 'probe_extra' not in seen['bound'][-1]
    assert 'read' in seen['bound'][-1]

    # 运行时加一个工具：只动能力集（服务），不动图
    toolbox = ctx.get('tools')
    digest_before = toolbox.digest
    handle = toolbox.register(probe_extra)
    assert toolbox.digest != digest_before

    await run_turn(runtime, 't1', '第二轮')
    assert 'probe_extra' in seen['bound'][-1]

    # 结构没有重建：还是同一个版本
    assert runtime.versions.current is before
    assert runtime.versions.pending is None

    # 撤销（等价于插件卸载）：下一次模型调用不再看到它
    handle()
    await run_turn(runtime, 't1', '第三轮')
    assert 'probe_extra' not in seen['bound'][-1]


async def test_registered_tool_is_actually_executable():
    """判据①的另一半：不只是"模型看得到"，执行路径也现查注册表。"""
    seen = {'bound': []}
    usage = {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}
    call = {'name': 'probe_extra', 'args': {'value': 'hi'}, 'id': 'call_1', 'type': 'tool_call'}
    script = [
        AIMessage(content='调用', tool_calls=[call], usage_metadata=usage),
        AIMessage(content='完成', usage_metadata=usage),
    ]
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    ctx.get('tools').register(probe_extra)
    runtime = make_runtime(ctx, script, seen)
    await runtime.setup()

    snapshot = await run_turn(runtime, 't2', '用 probe_extra')

    results = [item for item in snapshot.values['messages'] if isinstance(item, ToolMessage)]
    assert results and results[0].content == 'extra:hi'
