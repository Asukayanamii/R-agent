"""合成报告（D7）：字段全部生成、可 JSON 化、点名叫出"未参与/停用/空锚点"。"""

import json

import pytest

from support import noop

from app.harness.fragment import Fragment, NodeSpec
from app.kernel import Context

pytestmark = pytest.mark.anyio


async def make_runtime():
    from test_runtime import make_ready_runtime

    return await make_ready_runtime()


async def test_report_lists_base_mounts_and_frequencies():
    runtime = await make_runtime()
    report = runtime.build_report()

    assert report.version == runtime.versions.current.id
    assert report.base == 'builtin/loop'
    assert report.protocol is None  # 这棵测试树没装协议插件
    assert report.participants == ('builtin/loop',)
    assert [mount.anchor for mount in report.mounts] == [
        'entry',
        'pre_model',
        'post_model',
        'post_tools',
        'finish',
    ]
    assert all(mount.empty for mount in report.mounts)
    assert report.mounts[0].frequency == '每轮 1 次'
    assert report.effective_at

    rendered = report.render()
    assert '内置' not in rendered
    assert 'entry' in rendered and '（空，透传，每轮 1 次）' in rendered
    json.dumps(report.to_dict())  # 必须可 JSON 化（graph_updated 事件要用）


async def test_report_shows_mount_chain_and_warnings():
    runtime = await make_runtime()

    def plugin(ctx):
        ctx.structure.contribute(
            Fragment(
                id='workspace/probe',
                anchor='pre_model',
                nodes=(NodeSpec('probe_node', run=noop),),
            )
        )

    await runtime.ctx.plugin(plugin)
    await runtime.maybe_recompose()
    report = runtime.build_report()

    pre_model = next(mount for mount in report.mounts if mount.anchor == 'pre_model')
    assert pre_model.chain == ('workspace/probe',)
    assert 'workspace/probe' in report.participants


async def test_report_names_pending_plugins():
    """Cordis 的 PENDING 是静默的：报告必须点名"为什么没参与"。"""
    runtime = await make_runtime()

    def waiting(ctx):
        pass

    waiting.name = 'workspace/waiting'
    waiting.inject = ['search']
    await runtime.ctx.plugin(waiting)

    report = runtime.build_report()
    assert {'id': 'workspace/waiting', 'reason': '缺少服务 search'} in report.not_participating
    assert 'workspace/waiting（缺少服务 search）' in report.render()


async def test_report_names_disabled_fragments():
    runtime = await make_runtime()
    for _ in range(3):  # 连续失败达到阈值才熔断
        runtime.failures.record('workspace/bad', 'boom_node', RuntimeError('炸了'))

    report = runtime.build_report()
    assert report.disabled and report.disabled[0]['id'] == 'workspace/bad'
    assert 'workspace/bad' in report.render()
