"""插件发现与信任（阶段 E）：两级插件库、按文件批准、热重载、删文件回到原样。

完成判据（计划 §8）：把一条 ``anchor="pre_model"`` 的 json 片段放进工作区 → 批准 →
图里多一条链 → 删文件 → 自动回到原样；**全程不改框架代码**。
"""

import asyncio
import json
from pathlib import Path

import pytest

from test_runtime import make_ready_runtime

from app.plugins.discover import PluginDiscovery

pytestmark = pytest.mark.anyio

TASK_NODE = 'research_notes'
FRAGMENT = {
    'id': 'workspace/research',
    'anchor': 'pre_model',
    'chain': [
        {
            'name': TASK_NODE,
            'interface': 'research',
            'reads': ['messages'],
        }
    ],
}


async def research_node(state: dict) -> dict:
    """片段的实现（由服务 `research` 提供）：纯声明片段自己不携带实现。"""
    return {}


def plugins_dir(workspace: Path) -> Path:
    return workspace / '.my_agent' / 'plugins'


def write_plugin(directory: Path, payload=FRAGMENT, name: str = 'research.json') -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return path


def approve(workspace: Path, *names: str) -> None:
    target = workspace / '.my_agent' / 'plugins.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({'trusted': list(names)}, ensure_ascii=False), encoding='utf-8')


async def make_runtime_with_impl():
    runtime = await make_ready_runtime()
    runtime.ctx.provide('research', research_node)
    return runtime


async def begin_turn(runtime, thread_id: str = 't1') -> None:
    """模拟一次回合边界：`lease()` 获取时把 pending 原子替换成 current（§4.4）。"""
    async with runtime.lease(thread_id):
        pass


async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def fragment_ids(runtime) -> list[str]:
    return [item.id for item in runtime.structure.collect()]


# ------------------------------------------------------------------ 完成判据


async def test_workspace_fragment_round_trip(tmp_path):
    """放文件 → 批准 → 图里多一条链 → 删文件 → 回到原样。"""
    runtime = await make_runtime_with_impl()
    original_hash = runtime.versions.current.structure_hash
    assert fragment_ids(runtime) == ['builtin/loop']

    write_plugin(plugins_dir(tmp_path))
    discovery = PluginDiscovery(runtime.ctx, global_dir=None, workspace=tmp_path, watch=False)

    # 1. 没批准 → 待批准，图不动
    report = await discovery.scan()
    assert report.pending == ['research.json']
    assert report.loaded == []
    await begin_turn(runtime)
    assert fragment_ids(runtime) == ['builtin/loop']

    # 2. 批准 → 装载（用户/界面写授权清单；agent 写不了：沙箱硬阻断）
    approve(tmp_path, 'research.json')
    report = await discovery.scan()
    assert report.loaded == ['research.json（工作区）']

    # 还没到回合边界：报告必须点出"待生效"，不能拿现状冒充正在跑的图
    report = runtime.build_report()
    assert report.pending_change is True
    assert '待生效' in report.render()
    assert TASK_NODE not in runtime.versions.current.node_names

    # 3. 下一个回合边界换图：pre_model 锚点上多一条链
    await begin_turn(runtime)
    version = runtime.versions.current
    assert TASK_NODE in version.node_names
    assert version.structure_hash != original_hash
    assert runtime.build_report().pending_change is False
    pre_model = next(
        mount for mount in runtime.build_report().mounts if mount.anchor == 'pre_model'
    )
    assert pre_model.chain == ('workspace/research',)
    assert not pre_model.empty

    # 4. 删文件 → 撤销 → 回合边界回到原样
    (plugins_dir(tmp_path) / 'research.json').unlink()
    report = await discovery.scan()
    assert report.removed == ['research.json']
    await begin_turn(runtime)
    assert fragment_ids(runtime) == ['builtin/loop']
    assert runtime.versions.current.structure_hash == original_hash


# ------------------------------------------------------------------ 信任


async def test_global_library_is_trusted(tmp_path):
    """全局库装载即信任：不写授权清单也装载。"""
    runtime = await make_runtime_with_impl()
    global_dir = tmp_path / 'global'
    write_plugin(global_dir, {**FRAGMENT, 'id': 'global/research'})

    discovery = PluginDiscovery(runtime.ctx, global_dir=global_dir, workspace=None, watch=False)
    report = await discovery.scan()

    assert report.pending == []
    assert report.loaded == ['research.json（全局）']
    await begin_turn(runtime)
    assert TASK_NODE in runtime.versions.current.node_names


async def test_workspace_python_plugin_needs_approval(tmp_path):
    """工作区 `.py` 插件装载即执行代码——同样只认授权清单里的文件名。"""
    runtime = await make_runtime_with_impl()
    directory = tmp_path / '.my_agent' / 'plugins'
    directory.mkdir(parents=True)
    (directory / 'notes.py').write_text(
        'async def node(state):\n'
        '    return {}\n'
        '\n'
        'def apply(ctx):\n'
        '    ctx.provide("notes_impl", node)\n'
        '\n'
        'plugin = {"apply": apply, "name": "workspace/notes"}\n',
        encoding='utf-8',
    )

    discovery = PluginDiscovery(runtime.ctx, global_dir=None, workspace=tmp_path, watch=False)
    report = await discovery.scan()
    assert report.pending == ['notes.py']
    assert runtime.ctx.get('notes_impl') is None

    approve(tmp_path, 'notes.py')
    report = await discovery.scan()
    assert report.loaded == ['notes.py（工作区）']
    assert runtime.ctx.get('notes_impl') is not None


# ------------------------------------------------------------------ 热重载


async def test_concurrent_scans_load_a_file_once(tmp_path):
    """批准（立刻扫一次）与轮询任务会并发扫同一个目录：同一个新文件只能装载一次。

    不加锁时两边都过了"还没装载"的判断 → 装载两次：第二次撞 `FRAGMENT_DUPLICATE`（白噪音），
    万一两次都成功还会留下一个没人认领的 fiber。
    """
    runtime = await make_runtime_with_impl()
    write_plugin(plugins_dir(tmp_path))
    approve(tmp_path, 'research.json')
    discovery = PluginDiscovery(runtime.ctx, global_dir=None, workspace=tmp_path, watch=False)

    first, second = await asyncio.gather(discovery.scan(), discovery.scan())

    loaded = [item for report in (first, second) for item in report.loaded]
    assert loaded == ['research.json（工作区）']
    assert not first.failed and not second.failed
    await begin_turn(runtime)
    assert fragment_ids(runtime) == ['builtin/loop', 'workspace/research']


async def test_broken_file_is_not_retried_every_scan(tmp_path):
    """坏文件不每秒重试：同一份内容在冷却期内跳过（改内容立刻重试）——否则每秒一条 ERROR 堆栈。"""
    runtime = await make_runtime_with_impl()
    write_plugin(
        plugins_dir(tmp_path),
        {
            'id': 'workspace/broken',
            'anchor': 'pre_model',
            'chain': [{'name': 'broken_node', 'interface': 'no_such_service'}],
        },
        name='broken.json',
    )
    approve(tmp_path, 'broken.json')
    discovery = PluginDiscovery(runtime.ctx, global_dir=None, workspace=tmp_path, watch=False)

    first = await discovery.scan()
    second = await discovery.scan()  # 同一份内容：冷却期内不再重试、也不再报

    assert [name for name, _ in first.failed] == ['broken.json']
    assert second.failed == []

    # 改了内容就立刻重试（这次给它一个存在的服务，应当装载成功）
    write_plugin(
        plugins_dir(tmp_path),
        {
            'id': 'workspace/broken',
            'anchor': 'pre_model',
            'chain': [{'name': 'broken_node', 'interface': 'research'}],
        },
        name='broken.json',
    )
    third = await discovery.scan()

    assert third.failed == []
    assert third.loaded == ['broken.json（工作区）']


async def test_watch_loop_reverts_after_file_removal(tmp_path):
    """热重载：轮询看得见删除，结构来源自动撤销；图在下一个回合边界回到原样。"""
    runtime = await make_runtime_with_impl()
    original_hash = runtime.versions.current.structure_hash
    write_plugin(plugins_dir(tmp_path))
    approve(tmp_path, 'research.json')

    discovery = PluginDiscovery(
        runtime.ctx, global_dir=None, workspace=tmp_path, watch=True, interval=0.05
    )
    await discovery.start()
    await begin_turn(runtime)
    assert TASK_NODE in runtime.versions.current.node_names

    (plugins_dir(tmp_path) / 'research.json').unlink()
    assert await wait_until(lambda: fragment_ids(runtime) == ['builtin/loop'])

    await begin_turn(runtime)
    assert runtime.versions.current.structure_hash == original_hash


async def test_edit_conflict_is_reloaded_from_content(tmp_path):
    """改内容（同秒等长也算）会被内容指纹看见：先摘旧的再装新的。"""
    runtime = await make_runtime_with_impl()
    write_plugin(plugins_dir(tmp_path))
    approve(tmp_path, 'research.json')
    discovery = PluginDiscovery(runtime.ctx, global_dir=None, workspace=tmp_path, watch=False)
    await discovery.scan()
    await begin_turn(runtime)
    assert TASK_NODE in runtime.versions.current.node_names

    write_plugin(
        plugins_dir(tmp_path),
        {**FRAGMENT, 'chain': [{**FRAGMENT['chain'][0], 'name': 'research_v2'}]},
    )
    report = await discovery.scan()
    assert report.removed == ['research.json']
    assert report.loaded == ['research.json（工作区）']

    await begin_turn(runtime)
    version = runtime.versions.current
    assert 'research_v2' in version.node_names
    assert TASK_NODE not in version.node_names


# ------------------------------------------------------------------ 坏文件不拖垮别人


async def test_broken_declarations_are_reported_not_fatal(tmp_path):
    runtime = await make_runtime_with_impl()
    directory = tmp_path / '.my_agent' / 'plugins'
    directory.mkdir(parents=True)
    (directory / 'bad-json.json').write_text('{ 不是 JSON', encoding='utf-8')
    (directory / 'missing-impl.json').write_text(
        json.dumps(
            {
                'id': 'workspace/ghost',
                'anchor': 'pre_model',
                'chain': [{'name': 'ghost_node', 'interface': 'no_such_service'}],
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )
    write_plugin(plugins_dir(tmp_path), {**FRAGMENT, 'id': 'workspace/good'}, name='good.json')
    approve(tmp_path, 'bad-json.json', 'missing-impl.json', 'good.json')

    discovery = PluginDiscovery(runtime.ctx, global_dir=None, workspace=tmp_path, watch=False)
    report = await discovery.scan()

    codes = dict(report.failed)
    assert 'DECL_JSON' in codes['bad-json.json']
    assert 'INTERFACE_MISSING' in codes['missing-impl.json']
    assert report.loaded == ['good.json（工作区）']

    await begin_turn(runtime)
    names = runtime.versions.current.node_names
    assert TASK_NODE in names  # 好的那个照常进图
    assert 'ghost_node' not in names
