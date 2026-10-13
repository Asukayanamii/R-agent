"""日志出口接入标准 logging 的测试（A1 适配用例）。

宿主用 ``LOG_LEVEL`` 统一控制 ``app.*`` 的级别，所以内核出口的默认阈值必须是
"放行全部"、由标准 logging 过滤；日志名也要落在宿主指定的 logger 下。
"""

import logging

import pytest

from app.kernel import ConsoleExporter, Context, LoggingExporter

pytestmark = pytest.mark.anyio

LOGGER_NAME = 'app.kernel.test'


def _bridge(ctx: Context, name: str = LOGGER_NAME) -> None:
    """按宿主的换装方式接上桥：去掉控制台出口，挂标准 logging 出口。"""
    ctx.logger.exporters.clear()
    ctx.logger.exporter(LoggingExporter(name))


def test_default_console_exporter_can_be_swapped():
    ctx = Context()
    assert any(isinstance(exporter, ConsoleExporter) for exporter in ctx.logger.exporters)

    _bridge(ctx)
    assert [type(exporter).__name__ for exporter in ctx.logger.exporters] == ['LoggingExporter']


async def test_kernel_log_lands_in_stdlib_logging(caplog):
    caplog.set_level(logging.DEBUG)
    ctx = Context()
    _bridge(ctx)

    ctx.logger.info('装配完成 %d 个服务', 2)
    ctx.logger.warn('注意')
    ctx.logger.error('坏了')
    ctx.logger.debug('细节')

    records = [record for record in caplog.records if record.name == LOGGER_NAME]
    assert [record.levelno for record in records] == [
        logging.INFO,
        logging.WARNING,
        logging.ERROR,
        logging.DEBUG,
    ]
    assert records[0].getMessage() == 'root: 装配完成 2 个服务'


async def test_debug_is_forwarded_and_logging_filters(caplog):
    """内核出口不按 CORDIS_LOG_LEVEL 过滤：级别判断发生在标准 logging 这一层。"""
    caplog.set_level(logging.INFO)
    ctx = Context()
    _bridge(ctx)

    ctx.logger.debug('不该出现')
    assert [record for record in caplog.records if record.name == LOGGER_NAME] == []

    caplog.set_level(logging.DEBUG)
    ctx.logger.debug('该出现了')
    assert [
        record.getMessage() for record in caplog.records if record.name == LOGGER_NAME
    ] == ['root: 该出现了']


async def test_exporter_follows_plugin_lifecycle():
    ctx = Context()
    ctx.logger.exporters.clear()

    def plugin(ctx):
        ctx.logger.exporter(LoggingExporter('app.kernel.test.lifecycle'))

    fiber = ctx.plugin(plugin)
    await fiber
    assert len(ctx.logger.exporters) == 1

    await fiber.dispose()
    assert len(ctx.logger.exporters) == 0
