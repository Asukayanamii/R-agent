"""图版本审计（阶段 I）：每次合成留一行，含 reason / spec / mermaid；审计失败不影响装配。"""

import json

import pytest

from support import noop
from test_runtime import make_runtime

from app.harness.fragment import Fragment, NodeSpec
from app.kernel import Context
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.builtin.store.graph_version_dao import GraphVersionDao
from app.plugins.builtin.store.graph_version_service import GraphVersionService

pytestmark = pytest.mark.anyio


async def make_audited(tmp_path):
    """真装配 + 真落表（临时库）：返回 (runtime, dao)。"""
    dao = GraphVersionDao(str(tmp_path / 'audit.db'))
    await dao.open()
    service = GraphVersionService(dao)

    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    runtime = make_runtime(ctx, on_version=service.record)
    await runtime.setup()
    return runtime, dao


async def test_startup_version_is_audited(tmp_path):
    runtime, dao = await make_audited(tmp_path)
    rows = await dao.recent()

    assert len(rows) == 1
    row = rows[0]
    assert row['reason'] == '启动'
    assert row['scope'] == ''
    assert row['base'] == 'builtin/loop'
    assert row['protocol'] is None  # 这棵测试树没装协议插件
    assert row['structure_hash'] == runtime.versions.current.structure_hash
    assert row['participants'] == ['builtin/loop']
    assert row['mermaid'].strip()  # 图存档（回放的依据之一）
    assert [item['id'] for item in row['spec']['fragments']] == ['builtin/loop']
    assert row['created_at']
    json.dumps(row, ensure_ascii=False)  # 全字段可 JSON 化（inspect / 事件要用）
    await dao.close()


async def test_structure_change_is_audited(tmp_path):
    runtime, dao = await make_audited(tmp_path)
    before = (await dao.recent())[0]['structure_hash']

    def plugin(ctx):
        ctx.structure.contribute(
            Fragment(
                id='workspace/probe',
                anchor='pre_model',
                nodes=(NodeSpec('probe_node', run=noop),),
            )
        )

    await runtime.ctx.plugin(plugin)
    async with runtime.lease('t1'):
        pass

    rows = await dao.recent()
    assert [row['reason'] for row in rows] == ['结构变化', '启动']
    assert rows[0]['structure_hash'] != before
    assert 'workspace/probe' in rows[0]['participants']
    pre_model = next(item for item in rows[0]['mounts'] if item['anchor'] == 'pre_model')
    assert pre_model['chain'] == ['workspace/probe']
    await dao.close()


async def test_circuit_break_is_audited(tmp_path):
    runtime, dao = await make_audited(tmp_path)

    for _ in range(3):  # 连续三次 → 熔断（阈值 3）
        runtime.failures.record('workspace/probe', 'probe_node', RuntimeError('boom'))
    async with runtime.lease('t1'):
        pass

    rows = await dao.recent()
    assert rows[0]['reason'] == '熔断'
    await dao.close()


async def test_first_compose_of_a_new_scope_is_a_structure_change(tmp_path):
    """新作用域的**首次**合成不是熔断：审计的 reason 不能把"没记过"当"熔断名单变了"。"""
    runtime, dao = await make_audited(tmp_path)

    await runtime.maybe_recompose(scope='scope-x')

    rows = await dao.recent('scope-x')
    assert rows and rows[0]['reason'] == '结构变化'
    await dao.close()


async def test_audit_failure_does_not_break_compose(tmp_path):
    """审计回调抛错只记 WARNING：装配照常，版本照常生效。"""
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)

    async def boom(*args):
        raise RuntimeError('audit down')

    runtime = make_runtime(ctx, on_version=boom)
    await runtime.setup()

    assert runtime.versions.current is not None
    assert runtime.versions.current.node_names
