"""阶段 D 的事件（D8）：toolset_updated / graph_updated / plugin_failed。

判据之外的增量验证：变化以**回合边界**为准落到事件里，且只在"相对这个会话上次看到的东西"
变了的时候发——不是每轮都刷。
"""

import itertools

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.langgraph_runner import LangGraphRunner
from app.harness.fragment import Fragment, NodeSpec
from app.harness.runtime import HarnessRuntime
from app.kernel import Context
from app.plugins.builtin import loop as loop_plugin
from app.plugins.builtin import protocol as protocol_plugin
from app.plugins.builtin import toolbox as toolbox_plugin

pytestmark = pytest.mark.anyio


@tool
async def probe_tool(value: str) -> str:
    """探针工具。"""
    return value


def make_model():
    usage = {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}
    cursor = itertools.count()

    class Stub(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return 'stub'

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise NotImplementedError

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            next(cursor)
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content='好', usage_metadata=usage))]
            )

    return Stub()


async def make_runner() -> tuple[LangGraphRunner, Context]:
    ctx = Context()
    await ctx.plugin(protocol_plugin.plugin, essential=True)
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    runtime = HarnessRuntime(
        ctx, checkpointer=InMemorySaver(), model=make_model(), summary_model=make_model()
    )
    await runtime.setup()
    return LangGraphRunner(runtime, ctx.get('protocol')), ctx


async def frames(runner: LangGraphRunner, thread_id: str, message: str) -> list[dict]:
    return [
        event.model_dump(mode='json')
        async for event in runner.stream(thread_id, message)
    ]


async def test_no_notices_for_the_first_turn():
    runner, ctx = await make_runner()
    first = await frames(runner, 't1', '第一轮')

    assert [frame['type'] for frame in first] == ['message_end']


async def test_toolset_updated_after_capability_change():
    runner, ctx = await make_runner()
    await frames(runner, 't1', '第一轮')

    ctx.get('tools').register(probe_tool)
    second = await frames(runner, 't1', '第二轮')
    kinds = [frame['type'] for frame in second]
    assert 'toolset_updated' in kinds
    notice = next(frame for frame in second if frame['type'] == 'toolset_updated')
    assert 'probe_tool' in notice['data']['tools']

    # 只发一次：下一轮相对这个会话已经没有变化
    third = await frames(runner, 't1', '第三轮')
    assert 'toolset_updated' not in [frame['type'] for frame in third]


async def test_graph_updated_after_structure_change():
    runner, ctx = await make_runner()
    await frames(runner, 't1', '第一轮')

    # 结构变化：贡献一条链（下一个回合边界生效）
    def plugin(plugin_ctx):
        plugin_ctx.structure.contribute(
            Fragment(
                id='workspace/probe',
                anchor='pre_model',
                nodes=(NodeSpec('probe_node', run=lambda state: {}),),
            )
        )

    await ctx.plugin(plugin)
    second = await frames(runner, 't1', '第二轮')
    notice = next(frame for frame in second if frame['type'] == 'graph_updated')
    assert notice['data']['version'] == 2
    assert notice['data']['mermaid'].startswith('---') or 'graph TD' in notice['data']['mermaid']
    assert notice['data']['base'] == loop_plugin.PLUGIN_ID


async def test_plugin_failed_event_from_mapping():
    runner, ctx = await make_runner()

    async def boom(state):  # noqa: ARG001
        raise RuntimeError('探针炸了')

    def plugin(plugin_ctx):
        plugin_ctx.structure.contribute(
            Fragment(
                id='workspace/boom',
                anchor='post_model',
                nodes=(NodeSpec('boom_node', run=boom),),
            )
        )

    await ctx.plugin(plugin)
    await frames(runner, 't1', '第一轮')  # 这一轮刚合成进来，先跑起来
    second = await frames(runner, 't1', '第二轮')

    failures = [frame for frame in second if frame['type'] == 'plugin_failed']
    assert failures and failures[0]['data']['fragment'] == 'workspace/boom'
    assert failures[0]['data']['node'] == 'boom_node'
    assert '探针炸了' in failures[0]['data']['error']
