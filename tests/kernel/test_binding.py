"""服务绑定的返回值语义（A1 适配用例）。

注册式调用（``ctx.service.register(...)`` 返回 Disposer）的返回值经常被直接丢掉
——这类调用不能留下 never-awaited 协程警告；而异步方法的协程仍要保留调用方绑定。
"""

import gc
import warnings

import pytest

from app.kernel import Context, Service

pytestmark = pytest.mark.anyio


class Sink(Service):
    def __init__(self, ctx):
        super().__init__(ctx, 'sink')

    def add(self, item):
        return self.ctx.cleanup(lambda: None, f'add({item})')


async def test_ignored_disposer_result_leaves_no_warning(recwarn):
    ctx = Context()
    await ctx.plugin(Sink)

    ctx.sink.add('a')  # 拿了就扔：注册式调用的常见写法
    gc.collect()

    leaked = [
        warning
        for warning in recwarn.list
        if issubclass(warning.category, RuntimeWarning)
        and 'never awaited' in str(warning.message)
    ]
    assert leaked == []


async def test_disposer_result_can_still_be_awaited():
    ctx = Context()
    log = []

    class Svc(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'svc')

        def resource(self):
            return self.ctx.cleanup(lambda: log.append('closed'), 'svc.resource')

    await ctx.plugin(Svc)
    handle = ctx.svc.resource()
    await handle
    assert log == ['closed']


async def test_async_method_body_sees_caller_context():
    """异步方法在 await 期间保持调用方绑定：方法体内的 self.ctx 是调用方上下文。"""
    ctx = Context()
    seen = []

    class Provider(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'provider')

        async def observe(self):
            seen.append(self.ctx.fiber.name)

    await ctx.plugin(Provider)

    async def consumer(caller_ctx):
        await caller_ctx.provider.observe()

    fiber = ctx.plugin({'apply': consumer, 'name': 'consumer', 'inject': ['provider']})
    await fiber
    assert seen == ['consumer']
