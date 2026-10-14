"""builtin/exporters：可选插件——观测出口：把内核事件落成审计记录。

这是**观测类**接缝的样板（§2.5）：只监听、不改行为、即时生效。
记录同时进日志（`ctx.logger`）与内存环形缓冲（`exporters.recent`，供诊断与用例）。

将来新的观测事件在这里发出——出口坏了不影响主链路：监听器抛异常由事件总线自己收口。
"""

from __future__ import annotations

from typing import Any

from app.kernel import Service

PLUGIN_ID = 'builtin/exporters'
KEEP = 100


class Exporters(Service):
    """服务 ``exporters``：审计出口。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx, 'exporters')
        self.recent: list[dict[str, Any]] = []

    def emit(self, kind: str, **fields: Any) -> None:
        record = {'kind': kind, **fields}
        self.recent = [*self.recent, record][-KEEP:]
        self._service_ctx.logger('exporters').info('观测 %s：%s', kind, fields)


def apply(ctx: Any) -> None:
    exporters = Exporters(ctx)

    def on_plugin(fiber: Any) -> None:
        exporters.emit('plugin', name=fiber.name, state=fiber.state.name)

    def on_status(fiber: Any, old_state: Any) -> None:
        exporters.emit('status', name=fiber.name, old=old_state.name, new=fiber.state.name)

    ctx.on('internal/plugin', on_plugin)
    ctx.on('internal/status', on_status)
    ctx.logger.info('观测出口已就绪（插件/状态变化都记账）')


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
}
