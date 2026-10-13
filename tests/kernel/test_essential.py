"""必需插件的启动校验（B1）。

必需插件（loop / protocol / toolbox 这类）可替换、不可停用：缺了要**启动失败并点名**，
不带一个半死的内核跑。覆盖 FAILED 与 PENDING 两种"不在 ACTIVE"。
"""

import pytest

from app.kernel import Context, CordisError

pytestmark = pytest.mark.anyio


async def test_essential_active_passes():
    ctx = Context()

    def plugin(ctx):
        pass

    fiber = ctx.plugin(plugin, essential=True)
    await fiber

    ctx.registry.ensure_essentials()  # 不抛


async def test_essential_failure_is_reported_with_reason():
    ctx = Context()

    def broken(ctx):
        raise RuntimeError('boom')

    fiber = ctx.plugin(broken, essential=True)
    with pytest.raises(RuntimeError):
        await fiber

    with pytest.raises(CordisError) as info:
        ctx.registry.ensure_essentials()
    assert 'broken' in str(info.value)
    assert 'boom' in str(info.value)


async def test_essential_pending_is_reported():
    ctx = Context()

    def watcher(ctx):
        pass

    watcher.inject = ['missing']
    fiber = ctx.plugin(watcher, essential=True)
    await fiber
    assert fiber.state.name == 'PENDING'

    with pytest.raises(CordisError) as info:
        ctx.registry.ensure_essentials()
    assert 'watcher' in str(info.value)
    assert 'PENDING' in str(info.value)


async def test_optional_failure_does_not_fail_startup():
    ctx = Context()

    def broken(ctx):
        raise RuntimeError('boom')

    fiber = ctx.plugin(broken)  # 非必需：失败只是它自己的事
    with pytest.raises(RuntimeError):
        await fiber

    ctx.registry.ensure_essentials()  # 只校验必需清单
