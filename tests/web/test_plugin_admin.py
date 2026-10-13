"""插件管理服务（阶段 N）：列清单 / 启用停用 / 重读 / 删除。

判据（"用户手动管理插件"这件事要成立）：
- 一次请求拿到"我有哪些插件、谁没生效、为什么"（含内置那批，和模型看到的是同一份数据）；
- 停用与启用是**同一件事的两面**（改 `_` 前缀 + 工作区库同步信任清单），没有第三种状态；
- 危险动作（删除）先撤信任、只收文件名，返回里写明怎么回来；
- 越界与不存在的文件在服务层就被拒（`InvalidInput`），不靠界面自觉。
"""

import json

import pytest

from app.common.exceptions import InvalidInput
from app.kernel import Context
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.discover import PluginLibraries, write_trust
from app.web.service.plugin_admin import PluginAdmin

pytestmark = pytest.mark.anyio

GOOD = {
    'id': 'workspace/panel',
    'anchor': 'pre_model',
    'chain': [{'name': 'panel_node', 'interface': 'probe'}],
}


async def probe_node(state: dict) -> dict:
    return {}


def write_fragment(directory, filename: str, payload: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')


async def make_admin(tmp_path):
    """真装配（runtime 提供 `structure`，toolbox 提供 `tools`）+ 两级插件库 + 管理服务。"""
    from test_runtime import make_runtime

    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    ctx.provide('probe', probe_node)

    global_dir = tmp_path / 'global'
    global_dir.mkdir()
    (global_dir / 'marker.py').write_text(
        'def apply(ctx):\n    pass\n\n\nplugin = {"apply": apply, "name": "deploy/marker"}\n',
        encoding='utf-8',
    )

    workspace = tmp_path / 'ws'
    plugins = workspace / '.my_agent' / 'plugins'
    write_fragment(plugins, 'panel.json', GOOD)
    write_fragment(
        plugins,
        'later.json',
        {
            'id': 'workspace/later',
            'anchor': 'pre_model',
            'chain': [{'name': 'later_node', 'interface': 'probe'}],
        },
    )
    write_trust(workspace, {'panel.json'})

    libraries = PluginLibraries(ctx, global_dir=global_dir, workspace_enabled=True, watch=False)
    await libraries.start()
    runtime = make_runtime(ctx, on_scope=libraries.ensure)
    await runtime.setup()
    await libraries.ensure(str(workspace))
    return PluginAdmin(libraries), workspace, global_dir


def rows(overview: dict) -> dict[str, dict]:
    return {row['file']: row for row in overview['plugins'] if row.get('file')}


async def test_overview_lists_both_libraries_and_states(tmp_path):
    admin, workspace, global_dir = await make_admin(tmp_path)

    overview = await admin.overview(str(workspace))
    by_file = rows(overview)

    assert overview['enabled'] is True
    assert overview['libraries']['全局'] == str(global_dir)
    assert overview['libraries']['工作区'] == str(workspace / '.my_agent' / 'plugins')
    assert by_file['marker.py']['source'] == '全局'
    assert by_file['panel.json']['state'] == 'ACTIVE'
    assert by_file['later.json']['state'] == '待批准'
    assert any(row['source'] == '内置' for row in overview['plugins'])


async def test_disable_and_enable_are_two_sides_of_one_thing(tmp_path):
    admin, workspace, _ = await make_admin(tmp_path)
    directory = workspace / '.my_agent' / 'plugins'

    # 停用：改 `_` 前缀 + 撤信任 → 文件还在，状态是"已停用"（不是"待批准"）
    await admin.set_enabled('工作区', str(workspace), 'panel.json', False)
    stopped = await admin.overview(str(workspace))
    assert (directory / '_panel.json').is_file()
    assert not (directory / 'panel.json').exists()
    assert rows(stopped)['_panel.json']['state'] == '已停用'

    # 启用：去掉前缀 + 写回信任清单
    await admin.set_enabled('工作区', str(workspace), '_panel.json', True)
    back = await admin.overview(str(workspace))
    assert (directory / 'panel.json').is_file()
    assert rows(back)['panel.json']['state'] == 'ACTIVE'


async def test_enabling_a_pending_file_approves_it(tmp_path):
    """对"待批准"点启用 = 批准（第一次使用的入口），不另开一个动作。"""
    admin, workspace, _ = await make_admin(tmp_path)

    assert rows(await admin.overview(str(workspace)))['later.json']['state'] == '待批准'

    overview = await admin.set_enabled('工作区', str(workspace), 'later.json', True)

    assert rows(overview)['later.json']['state'] == 'ACTIVE'
    # 消息也要如实：批准一个待批准的文件不能回"本来就是这个状态"
    assert '本来就是这个状态' not in overview['message']


async def test_global_disable_renames_and_unloads(tmp_path):
    admin, workspace, global_dir = await make_admin(tmp_path)

    overview = await admin.set_enabled('全局', str(workspace), 'marker.py', False)

    assert (global_dir / '_marker.py').is_file()
    assert rows(overview)['_marker.py']['state'] == '已停用'

    overview = await admin.set_enabled('全局', str(workspace), '_marker.py', True)

    assert (global_dir / 'marker.py').is_file()
    assert rows(overview)['marker.py']['state'] == 'ACTIVE'


async def test_reload_rereads_an_unchanged_file(tmp_path):
    admin, workspace, _ = await make_admin(tmp_path)

    result = await admin.reload('工作区', str(workspace), 'panel.json')

    assert '装载' in result['message']


async def test_remove_takes_the_file_and_the_trust_away(tmp_path):
    admin, workspace, _ = await make_admin(tmp_path)
    directory = workspace / '.my_agent' / 'plugins'

    result = await admin.remove('工作区', str(workspace), 'panel.json')

    assert not (directory / 'panel.json').exists()
    assert 'panel.json' not in rows(result)
    assert '同名文件' in result['message']


@pytest.mark.parametrize('bad', ['../escape.py', 'sub/panel.json', 'notes.txt', 'missing.json'])
async def test_bad_file_names_are_refused(tmp_path, bad):
    admin, workspace, _ = await make_admin(tmp_path)

    with pytest.raises(InvalidInput):
        await admin.set_enabled('工作区', str(workspace), bad, False)
