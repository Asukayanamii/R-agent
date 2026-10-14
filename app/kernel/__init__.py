"""app/kernel：R-agent 内联的插件内核（Cordis 框架的 Python 复刻版）。

一个"万物皆插件"的依赖注入 + 事件驱动框架。**不 import 任何 ``app.*`` 模块**——
将来整体搬走只需挪目录；宿主经装配根（``app/container.py``）与它相接。

.. code-block:: python

    import asyncio
    from app.kernel import Context, Service

    class Greeter(Service):
        def __init__(self, ctx):
            super().__init__(ctx, 'greeter')

        def greet(self, who):
            return f'Hello, {who}!'

    def consumer(ctx):
        ctx.logger.info(ctx.greeter.greet('world'))

    async def main():
        ctx = Context()
        ctx.plugin(Greeter)
        ctx.plugin(consumer, inject=['greeter'])
        await ctx.fiber.await_()

    asyncio.run(main())
"""

from .context import Context
from .events import EventsService, Hook
from .fiber import Disposer, EffectMeta, Fiber, FiberState, PluginRuntime
from .logger import ConsoleExporter, Exporter, Logger, LoggerLevel, LoggerService, LoggingExporter
from .reflect import Impl, Property, ReflectService
from .registry import Inject, RegistryService, resolve_plugin
from .resource import ResourceService
from .schema import Issue, Schema, as_schema, validate_config
from .service import Service
from .utils import CordisError, DisposableList, Symbol, ValidationError, symbols

__version__ = '0.1.0'

__all__ = [
    'Context',
    'Service',
    'Fiber',
    'FiberState',
    'Disposer',
    'EffectMeta',
    'PluginRuntime',
    'EventsService',
    'Hook',
    'ReflectService',
    'RegistryService',
    'ResourceService',
    'Logger',
    'LoggerService',
    'LoggerLevel',
    'Exporter',
    'ConsoleExporter',
    'LoggingExporter',
    'Schema',
    'Issue',
    'as_schema',
    'validate_config',
    'Inject',
    'resolve_plugin',
    'CordisError',
    'ValidationError',
    'DisposableList',
    'Symbol',
    'symbols',
    'Property',
    'Impl',
    '__version__',
]
