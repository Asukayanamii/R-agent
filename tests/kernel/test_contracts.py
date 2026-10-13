"""契约依赖测试（C1）：provides / requires 装载时校验——不满足就拒绝装载。

与服务依赖（inject）的分工：服务依赖缺了是 PENDING（等它来）；
契约依赖对不上是拒绝装载（形状不对，等不来）。
"""

import pytest

from app.kernel import Context, CordisError

pytestmark = pytest.mark.anyio


def make_plugin(name: str, **attrs):
    """最小插件：函数 + 任意的 name/provides/requires 声明。"""

    def apply(ctx):  # noqa: ARG001
        return None

    apply.__name__ = name
    apply.name = name
    for key, value in attrs.items():
        setattr(apply, key, value)
    return apply


async def test_provides_then_requires_passes():
    ctx = Context()
    provider = make_plugin('provider', provides='wire:v1')
    await ctx.plugin(provider)

    consumer = make_plugin('consumer', requires='wire:v1')
    fiber = ctx.plugin(consumer)
    await fiber

    assert ctx.registry.contract_active('wire:v1')
    assert ctx.registry.provided_contracts() == ('wire:v1',)


async def test_requires_without_provider_is_rejected_at_load():
    ctx = Context()
    consumer = make_plugin('consumer', requires='wire:v2')

    with pytest.raises(CordisError) as info:
        ctx.plugin(consumer)

    assert 'wire:v2' in str(info.value)
    assert '当前可用：无' in str(info.value)


async def test_wrong_version_is_rejected_even_if_other_version_exists():
    """提供的是 v1、要的是 v2 → 拒绝（加字段不算破坏、改语义才 bump，版本必须对上）。"""
    ctx = Context()
    await ctx.plugin(make_plugin('provider', provides='wire:v1'))

    with pytest.raises(CordisError) as info:
        ctx.plugin(make_plugin('consumer', requires='wire:v2'))

    assert 'wire:v2' in str(info.value)
    assert 'wire:v1' in str(info.value)  # 报出当前可用的是哪些


async def test_provider_failure_does_not_make_contract_available():
    ctx = Context()

    def broken(ctx):
        raise RuntimeError('boom')

    broken.name = 'broken'
    broken.provides = 'wire:v1'
    fiber = ctx.plugin(broken)
    with pytest.raises(RuntimeError):
        await fiber

    assert ctx.registry.contract_active('wire:v1') is False


async def test_contract_is_withdrawn_on_unload():
    ctx = Context()
    fiber = ctx.plugin(make_plugin('provider', provides='wire:v1'))
    await fiber
    assert ctx.registry.contract_active('wire:v1')

    await fiber.dispose()
    assert ctx.registry.contract_active('wire:v1') is False

    with pytest.raises(CordisError):
        ctx.plugin(make_plugin('consumer', requires='wire:v1'))


async def test_dict_form_contracts():
    ctx = Context()
    await ctx.plugin({'apply': lambda ctx: None, 'name': 'p', 'provides': ['a:v1', 'b:v1']})
    assert ctx.registry.provided_contracts() == ('a:v1', 'b:v1')

    await ctx.plugin({'apply': lambda ctx: None, 'name': 'c', 'requires': 'a:v1'})
