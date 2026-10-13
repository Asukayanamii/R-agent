"""builtin/atomic_tools：批量工具，以及"顶替内置工具可回退"的语义。

判据：**工具调用是一轮一次模型往返**——批量版要能把"改多处/读多段"压成一次调用；
失败时要能说清"第几条、为什么、前面几条已应用"（批量最怕默默改一半）；
同名顶替在插件卸载后必须**还原**原来那个工具（"注册即 effect"＝回到注册前，不是删掉名字）。
"""

import pytest

from app.agent.runtime import current_workspace
from app.agent import sandbox
from app.kernel import Context
from app.plugins.builtin import atomic_tools
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.builtin.atomic_tools import edit_many, read_many

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """把工作区指向 tmp_path：工具的越界判定按它来，测试里的临时文件才算"工作区内"。"""
    monkeypatch.setattr(sandbox, '_memory', {})
    monkeypatch.setattr(sandbox, '_global', sandbox._Grant())
    token = current_workspace.set(tmp_path)
    yield tmp_path
    current_workspace.reset(token)


async def make_tools(tmp_path):
    """真装配：toolbox 提供 tools 服务，atomic_tools 往它里面注册。"""
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    fiber = ctx.plugin(atomic_tools.plugin)
    await fiber
    return ctx, fiber, tmp_path


async def test_edit_many_applies_in_one_call(tmp_path):
    ctx, _, _ = await make_tools(tmp_path)
    target = tmp_path / 'app.js'
    target.write_text('alpha\nbeta\ngamma\n', encoding='utf-8')

    result = await edit_many.ainvoke(
        {
            'edits': [
                {'path': str(target), 'old_string': 'alpha', 'new_string': 'ALPHA'},
                {'path': str(target), 'old_string': 'gamma', 'new_string': 'GAMMA'},
            ]
        }
    )

    assert target.read_text(encoding='utf-8') == 'ALPHA\nbeta\nGAMMA\n'
    # 直接 ainvoke 只拿到正文；artifact（给界面画的差异）由正常调用路径包进 ToolMessage
    assert '已修改 2 处' in result
    assert '替换 1 处' in result


async def test_edit_many_stops_and_says_what_applied(tmp_path):
    ctx, _, _ = await make_tools(tmp_path)
    target = tmp_path / 'app.js'
    target.write_text('alpha\nbeta\n', encoding='utf-8')

    with pytest.raises(Exception) as error:
        await edit_many.ainvoke(
            {
                'edits': [
                    {'path': str(target), 'old_string': 'alpha', 'new_string': 'ALPHA'},
                    {'path': str(target), 'old_string': '不存在的那段', 'new_string': 'x'},
                ]
            }
        )

    text = str(error.value)
    assert '第 2 条' in text and '前面 1 条已应用' in text
    # 第一条真的落了盘：批量工具的语义是"改到哪算哪，但要说清楚"
    assert target.read_text(encoding='utf-8') == 'ALPHA\nbeta\n'


async def test_read_many_reads_several_windows(tmp_path):
    ctx, _, _ = await make_tools(tmp_path)
    first = tmp_path / 'a.txt'
    second = tmp_path / 'b.txt'
    first.write_text('\n'.join(f'line{i}' for i in range(1, 11)) + '\n', encoding='utf-8')
    second.write_text('hello\n', encoding='utf-8')

    out = await read_many.ainvoke(
        {
            'reads': [
                {'path': str(first), 'offset': 3, 'limit': 2},
                {'path': str(second)},
                {'path': str(tmp_path / 'missing.txt')},   # 单个失败不该毁掉整批
            ]
        }
    )

    assert 'line3' in out and 'line5' not in out          # 窗口生效
    assert 'hello' in out
    assert '文件不存在' in out                              # 失败项就地说明
    assert '读了 3 段' in out


async def test_tools_are_visible_and_revocable(tmp_path):
    """注册即 effect：装载后能看到这两个工具，卸载后回到装载前（工具集与顶替都还原）。"""
    ctx, fiber, _ = await make_tools(tmp_path)
    tools = ctx.get('tools')

    assert 'edit_many' in tools.names() and 'read_many' in tools.names()

    await fiber.dispose()

    assert 'edit_many' not in tools.names() and 'read_many' not in tools.names()


async def test_replacing_a_tool_restores_it_on_unload(tmp_path):
    """同名顶替必须可回退：卸载插件后回来的是**原来那个**工具，而不是"名字消失"。"""
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    tools = ctx.get('tools')
    original = tools.get('grep')

    def apply(context) -> None:
        async def upgraded(pattern: str) -> str:
            return '顶替版'

        upgraded.name = 'grep'
        context.tools.register(upgraded)

    fiber = ctx.plugin({'apply': apply, 'name': 'test/upgrade-grep', 'inject': ['tools']})
    await fiber
    assert tools.get('grep') is not original  # 顶上去了

    await fiber.dispose()

    assert tools.get('grep') is original      # 还原，而不是消失
