"""登记 API 测试（A4）：``ctx.spawn`` / ``ctx.resource.acquire`` 与"卸载无残留"。

计划 A4 的判据：装载 → 卸载后 ``asyncio.all_tasks()`` 的快照一致；
长连接/句柄通过登记 API 拿的，卸载时必须收干净。
"""

import asyncio
import gc
import logging
from contextlib import contextmanager

import pytest

from app.kernel import Context

pytestmark = pytest.mark.anyio


async def test_spawn_task_is_cancelled_on_dispose():
    ctx = Context()
    started = asyncio.Event()
    stopped = []

    async def worker():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append('settled')

    def plugin(plugin_ctx):
        plugin_ctx.spawn(worker)

    fiber = ctx.plugin(plugin)
    await fiber
    await asyncio.wait_for(started.wait(), 1)

    await fiber.dispose()
    assert stopped == ['settled']


async def test_spawn_leaves_no_residue_tasks():
    """装载 → 卸载后 all_tasks() 快照回到装载前（A4 判据）。"""
    ctx = Context()

    async def worker():
        await asyncio.Event().wait()

    def plugin(plugin_ctx):
        plugin_ctx.spawn(worker)
        plugin_ctx.spawn(worker)

    before = asyncio.all_tasks()
    fiber = ctx.plugin(plugin)
    await fiber
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks() - before) == 2  # 两个后台任务确实在飞

    await fiber.dispose()
    assert asyncio.all_tasks() == before


async def test_spawn_failure_is_logged_not_leaked(caplog):
    caplog.set_level(logging.ERROR, logger='asyncio')
    ctx = Context()
    ctx.logger.exporters.clear()

    async def broken():
        raise RuntimeError('boom')

    def plugin(plugin_ctx):
        plugin_ctx.spawn(broken)

    fiber = ctx.plugin(plugin)
    await fiber
    await asyncio.sleep(0)

    # 失败落地在内核日志里：后台任务没人 await，不在这里落地就无声无息
    assert any('boom' in repr(message.args) for message in ctx.logger.buffer)

    await fiber.dispose()
    del fiber
    gc.collect()
    # 异常已被内部的 done 回调取走：不会变成 asyncio 的 never-retrieved 噪音
    assert [
        record for record in caplog.records if 'never retrieved' in record.getMessage()
    ] == []


async def test_acquire_closes_resource_on_dispose():
    ctx = Context()
    events = []

    class Conn:
        def __init__(self):
            events.append('open')

        def close(self):
            events.append('close')

    def plugin(plugin_ctx):
        conn = plugin_ctx.resource.acquire(lambda: Conn())
        events.append(('got', isinstance(conn, Conn)))

    fiber = ctx.plugin(plugin)
    await fiber
    assert events == ['open', ('got', True)]

    await fiber.dispose()
    assert events == ['open', ('got', True), 'close']


async def test_acquire_awaits_async_close():
    ctx = Context()
    closed = []

    class AsyncConn:
        async def aclose(self):
            await asyncio.sleep(0)
            closed.append(True)

    def plugin(plugin_ctx):
        plugin_ctx.resource.acquire(lambda: AsyncConn())

    fiber = ctx.plugin(plugin)
    await fiber
    await fiber.dispose()
    assert closed == [True]


async def test_acquire_enters_context_manager():
    ctx = Context()
    events = []

    @contextmanager
    def open_thing():
        events.append('enter')
        try:
            yield {'handle': True}
        finally:
            events.append('exit')

    def plugin(plugin_ctx):
        thing = plugin_ctx.resource.acquire(open_thing)
        events.append(thing)

    fiber = ctx.plugin(plugin)
    await fiber
    assert events == ['enter', {'handle': True}]

    await fiber.dispose()
    assert events == ['enter', {'handle': True}, 'exit']


async def test_acquire_rejects_coroutine_result():
    ctx = Context()

    async def open_async():
        return object()

    def plugin(plugin_ctx):
        with pytest.raises(TypeError):
            plugin_ctx.resource.acquire(lambda: open_async())

    fiber = ctx.plugin(plugin)
    await fiber
    await fiber.dispose()
