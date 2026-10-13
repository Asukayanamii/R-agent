"""节点壳与熔断（D6）：判据②③。

② 插件节点抛异常 → 会话继续 + 留痕（日志 / 事件 / 状态）+ 连续 3 次自动摘出合成输入；
③ `GraphBubbleUp` 不被吞（`interrupt()` 照常上抛）。
"""

import itertools

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphBubbleUp

from app.harness.circuit import FailureLedger, wrap_node
from app.harness.fragment import Fragment, NodeSpec
from app.harness.runtime import HarnessRuntime
from app.kernel import Context
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.builtin.protocol.mapping import translate_events

pytestmark = pytest.mark.anyio

FRAGMENT_ID = 'workspace/probe'


class _Bubble(GraphBubbleUp):
    """冒充 interrupt() 抛出的那种异常（真实的那类只在图内构造得出来）。"""


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
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='好', usage_metadata=usage))])

    return Stub()


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


async def make_runtime_with_probe(probe_node) -> HarnessRuntime:
    """真骨架 + 一条挂在 post_model 上、节点必炸的探针片段。"""
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    runtime = HarnessRuntime(
        ctx, checkpointer=InMemorySaver(), model=make_model(), summary_model=make_model()
    )
    await runtime.setup()

    def plugin(plugin_ctx):
        plugin_ctx.structure.contribute(
            Fragment(
                id=FRAGMENT_ID,
                anchor='post_model',
                nodes=(NodeSpec('probe_node', run=probe_node),),
            )
        )

    await ctx.plugin(plugin)
    return runtime


# ------------------------------------------------------------------ 壳的单元行为


async def test_shell_records_failure_and_returns_empty_update():
    ledger = FailureLedger()

    async def broken(state):  # noqa: ARG001
        raise RuntimeError('boom')

    shell = wrap_node('probe_node', broken, FRAGMENT_ID, ledger)
    update = await shell({})

    assert update['plugin_failures'][0]['fragment'] == FRAGMENT_ID
    assert update['plugin_failures'][0]['error'] == 'boom'
    assert ledger.counts[FRAGMENT_ID] == 1


async def test_shell_reraises_graph_bubble_up():
    ledger = FailureLedger()

    async def bubbling(state):  # noqa: ARG001
        raise _Bubble()

    shell = wrap_node('probe_node', bubbling, FRAGMENT_ID, ledger)
    with pytest.raises(_Bubble):
        await shell({})

    assert ledger.records == []  # 没有被当成"节点失败"
    assert ledger.counts == {}


async def test_ledger_resets_on_success():
    ledger = FailureLedger()
    assert ledger.record(FRAGMENT_ID, 'n', RuntimeError('一')).error == '一'
    ledger.reset(FRAGMENT_ID)
    assert ledger.record(FRAGMENT_ID, 'n', RuntimeError('二')).error == '二'
    assert ledger.counts[FRAGMENT_ID] == 1  # 连续的：成功一次即清零
    assert not ledger.is_disabled(FRAGMENT_ID)


# ------------------------------------------------------------------ 端到端


async def test_plugin_node_failure_does_not_kill_the_session():
    async def boom(state):  # noqa: ARG001
        raise RuntimeError('boom')

    runtime = await make_runtime_with_probe(boom)
    snapshot = await run_turn(runtime, 't1', '你好')

    # 会话跑完了；失败留痕在状态里（前端事件由映射层从同一条记录发出）
    failures = snapshot.values['plugin_failures']
    assert failures[-1]['fragment'] == FRAGMENT_ID
    assert failures[-1]['node'] == 'probe_node'
    assert 'boom' in failures[-1]['error']
    assert runtime.failures.counts[FRAGMENT_ID] == 1


async def test_circuit_breaker_disables_fragment_after_three_failures():
    async def boom(state):  # noqa: ARG001
        raise RuntimeError('boom')

    runtime = await make_runtime_with_probe(boom)
    for index in range(3):
        await run_turn(runtime, 't1', f'第{index}轮')

    assert runtime.failures.is_disabled(FRAGMENT_ID)
    assert '连续失败 3 次' in runtime.failures.disabled[FRAGMENT_ID]

    # 下一次回合边界：新版本里不再有这条片段（"自动摘出合成输入"）
    async with runtime.lease('t1') as version:
        assert 'probe_node' not in version.node_names
        assert runtime.versions.pending is None

    snapshot = await run_turn(runtime, 't1', '停用之后')
    assert snapshot.values['plugin_failures'].__len__() == 3  # 没有新的失败记录
