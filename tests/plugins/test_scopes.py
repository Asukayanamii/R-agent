"""插件作用域（阶段 J）：按会话工作区分区。

关键判据：**A 工作区批准的插件不进 B 工作区的图**——各自一套片段、版本与信任清单；
没有插件的（或没批准的）工作区照旧共用全局那张图。
"""

import json
from pathlib import Path

import pytest

from support import noop
from test_discovery import wait_until
from test_runtime import make_runtime

from app.kernel import Context
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.discover import PluginLibraries, scope_of, write_trust

pytestmark = pytest.mark.anyio


def make_workspace(tmp_path: Path, name: str, node: str) -> Path:
    """一个工作区 + 一条待批准片段（声明里挂一个抽象节点，实现由 `probe` 服务给）。"""
    workspace = tmp_path / name
    directory = workspace / '.my_agent' / 'plugins'
    directory.mkdir(parents=True)
    (directory / f'{name}.json').write_text(
        json.dumps(
            {
                'id': f'workspace/{name}',
                'anchor': 'pre_model',
                'chain': [{'name': node, 'interface': 'probe'}],
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    return workspace


async def probe_node(state: dict) -> dict:
    return {}


async def make_scoped(tmp_path, **kwargs):
    """真装配 + 插件库（工作区开关打开），runtime 的 on_scope 接到 libraries.ensure。"""
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    ctx.provide('probe', probe_node)
    libraries = PluginLibraries(
        ctx, global_dir=None, workspace_enabled=True, watch=False, **kwargs
    )
    await libraries.start()
    runtime = make_runtime(ctx, on_scope=libraries.ensure)
    await runtime.setup()
    return runtime, libraries


async def turn(runtime, workspace: str, thread_id: str = 't1') -> None:
    async with runtime.lease(thread_id, scope=workspace):
        pass


async def test_two_workspaces_do_not_leak(tmp_path):
    """A 批准的插件只在 A 的图里；B 没批准 → 用全局图（谁的插件都没有）。"""
    ws_a = make_workspace(tmp_path, 'a', 'node_a')
    ws_b = make_workspace(tmp_path, 'b', 'node_b')
    write_trust(ws_a, {'a.json'})  # 只批准 A

    runtime, libraries = await make_scoped(tmp_path)

    await turn(runtime, str(ws_a))
    scope_a = scope_of(ws_a)
    assert scope_a in runtime.scopes
    assert 'node_a' in runtime.registry(scope_a).current.node_names
    assert 'node_b' not in runtime.registry(scope_a).current.node_names

    # B 还没批准：它的库不算"有插件"，会话共用全局那张图
    assert await libraries.ensure(str(ws_b)) == ''
    await turn(runtime, str(ws_b))
    assert 'node_b' not in runtime.versions.current.node_names
    assert 'node_a' not in runtime.versions.current.node_names


async def test_trust_is_per_workspace(tmp_path):
    """信任清单按工作区各自一份：A 批准不生效于 B。"""
    ws_a = make_workspace(tmp_path, 'a', 'node_a')
    ws_b = make_workspace(tmp_path, 'b', 'node_b')
    write_trust(ws_a, {'a.json'})

    runtime, libraries = await make_scoped(tmp_path)

    assert await libraries.ensure(str(ws_a)) == scope_of(ws_a)
    assert await libraries.ensure(str(ws_b)) == ''
    # 服务端批准 B 之后，B 才有自己的图
    assert await libraries.trust(str(ws_b), 'b.json') is True
    assert await libraries.ensure(str(ws_b)) == scope_of(ws_b)
    await turn(runtime, str(ws_b))
    assert 'node_b' in runtime.registry(scope_of(ws_b)).current.node_names


async def test_report_and_audit_carry_scope(tmp_path):
    """报告与审计都带作用域：事后能看出"这一版图属于哪个工作区"。"""
    ws_a = make_workspace(tmp_path, 'a', 'node_a')
    write_trust(ws_a, {'a.json'})
    runtime, _ = await make_scoped(tmp_path)

    await turn(runtime, str(ws_a))
    scope_a = scope_of(ws_a)
    report = runtime.build_report(scope=scope_a)
    assert report.scope == scope_a
    assert 'workspace/a' in report.participants
    pre_model = next(item for item in report.mounts if item.anchor == 'pre_model')
    assert pre_model.chain == ('workspace/a',)

    # 全局那份报告不受影响（它只看得见全局片段）
    assert 'workspace/a' not in runtime.build_report().participants


async def test_lru_reclaims_oldest_workspace(tmp_path):
    """超过上限时回收最久没用过的那个工作区：它的片段一并撤销。"""
    ws_a = make_workspace(tmp_path, 'a', 'node_a')
    ws_b = make_workspace(tmp_path, 'b', 'node_b')
    write_trust(ws_a, {'a.json'})
    write_trust(ws_b, {'b.json'})

    runtime, libraries = await make_scoped(tmp_path, max_workspaces=1)

    await turn(runtime, str(ws_a))
    scope_a = scope_of(ws_a)
    assert 'node_a' in runtime.registry(scope_a).current.node_names

    await turn(runtime, str(ws_b))
    assert await wait_until(
        lambda: 'workspace/a' not in [item.id for item in runtime.structure.collect(scope_a)]
    )


async def test_disabled_keeps_single_graph(tmp_path):
    """`PLUGINS_WORKSPACE=0`（工作区库关）：所有工作区共用全局那张图，不建作用域账本。"""
    ws_a = make_workspace(tmp_path, 'a', 'node_a')
    write_trust(ws_a, {'a.json'})

    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    ctx.provide('probe', probe_node)
    libraries = PluginLibraries(ctx, global_dir=None, workspace_enabled=False, watch=False)
    await libraries.start()
    runtime = make_runtime(ctx, on_scope=libraries.ensure)
    await runtime.setup()

    assert await libraries.ensure(str(ws_a)) == ''
    await turn(runtime, str(ws_a))
    assert runtime.scopes == ('',)
    assert 'node_a' not in runtime.versions.current.node_names
