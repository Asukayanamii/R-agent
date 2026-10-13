"""HarnessRuntime 测试（B7）：装配、必需插件校验、结构变化只在回合边界生效。"""

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from support import make_chain, noop

from app.harness.fragment import Fragment, NodeSpec, StructureError
from app.harness.runtime import HarnessRuntime
from app.kernel import Context, CordisError

pytestmark = pytest.mark.anyio


def make_model() -> BaseChatModel:
    class Stub(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return 'stub'

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            raise NotImplementedError

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
            raise NotImplementedError

    return Stub()


def make_runtime(ctx: Context | None = None, **kwargs) -> HarnessRuntime:
    return HarnessRuntime(
        ctx or Context(),
        checkpointer=InMemorySaver(),
        model=make_model(),
        summary_model=make_model(),
        **kwargs,
    )


async def make_ready_runtime(**kwargs) -> HarnessRuntime:
    """装载 toolbox（loop 的服务依赖）并完成装配的 runtime。"""
    from app.plugins.builtin import toolbox

    runtime = make_runtime(**kwargs)
    await runtime.ctx.plugin(toolbox.plugin, essential=True)
    await runtime.setup()
    return runtime


async def test_setup_loads_loop_and_composes():
    runtime = await make_ready_runtime()

    version = runtime.versions.current
    assert version is not None
    assert version.base_id == 'builtin/loop'
    assert version.anchors == ('entry', 'pre_model', 'post_model', 'post_tools', 'finish')
    assert len(version.node_names) == 9
    assert 'stamp' in version.node_names
    # 片段来源：骨架在结构接缝里
    assert [item.id for item in runtime.structure.collect()] == ['builtin/loop']


async def test_maybe_recompose_is_noop_when_unchanged():
    runtime = await make_ready_runtime()

    assert await runtime.maybe_recompose() is False


async def test_structure_change_takes_effect_at_next_lease():
    runtime = await make_ready_runtime()
    first = runtime.versions.current

    # 模拟一个插件装载：贡献一条挂在 pre_model 上的链
    def plugin(ctx):
        ctx.structure.contribute(
            Fragment(
                id='workspace/probe',
                anchor='pre_model',
                nodes=(NodeSpec('probe_node', run=noop),),
            )
        )

    fiber = runtime.ctx.plugin(plugin)
    await fiber

    assert await runtime.maybe_recompose() is True
    pending = runtime.versions.pending
    assert pending is not None and pending.id == first.id + 1
    assert 'probe_node' in pending.node_names
    assert pending.structure_hash != first.structure_hash

    # 回合边界之前：当前图还是旧的
    assert runtime.versions.current is first

    async with runtime.lease('t1') as version:
        assert version.id == pending.id
        assert 'probe_node' in version.node_names

    # 卸下插件：片段自动撤销，下一次合成回到原样
    await fiber.dispose()
    assert await runtime.maybe_recompose() is True
    assert 'probe_node' not in runtime.versions.pending.node_names


async def test_essential_plugin_failure_fails_startup(monkeypatch):
    from app.plugins.builtin import loop, toolbox

    broken = {
        'apply': loop.apply,
        'name': 'loop-broken',
        'inject': ['model', 'summary_model', 'tools', 'service-that-does-not-exist'],
    }
    monkeypatch.setattr(loop, 'plugin', broken)

    runtime = make_runtime()
    await runtime.ctx.plugin(toolbox.plugin, essential=True)
    with pytest.raises(CordisError) as info:
        await runtime.setup()
    assert 'loop-broken' in str(info.value)


async def test_failed_compose_keeps_current_version():
    runtime = await make_ready_runtime()
    first = runtime.versions.current

    # 插件贡献一个形状不对的片段：锚点不存在 → 装载即失败（不改变运行图）
    def plugin(ctx):
        ctx.structure.contribute(make_chain('x', anchor='nowhere'))

    fiber = runtime.ctx.plugin(plugin)
    with pytest.raises(StructureError):
        await fiber

    assert runtime.versions.current is first
    assert runtime.versions.pending is None
    assert await runtime.maybe_recompose() is False


async def test_foreign_structure_service_is_reused():
    """ctx 里已经有结构接缝时沿用同一个（一个应用一棵插件树、一张片段表）。"""
    from app.plugins.builtin import toolbox

    ctx = Context()
    await ctx.plugin(toolbox.plugin, essential=True)
    runtime_a = make_runtime(ctx)
    await runtime_a.setup()

    runtime_b = make_runtime(ctx)
    assert runtime_b.structure is runtime_a.structure
