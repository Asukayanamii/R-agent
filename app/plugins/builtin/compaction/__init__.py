"""builtin/compaction：可选插件——服务 ``compaction``：`compact` 节点的实现。

卸下它 = 不再自动压缩（骨架里的 `compact` 退化成透传节点），其余照常——
与 `COMPACT_ENABLED=0` 同一语义。装载条件由装配脚本决定（配置→插件在不在）。
"""

from __future__ import annotations

from typing import Any, Callable

from app.agent.nodes import make_compact_node
from app.kernel import Service

PLUGIN_ID = 'builtin/compaction'


class Compaction(Service):
    """服务 ``compaction``：把压缩节点暴露成骨架上的 `compact`。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx, 'compaction')
        self._node: Callable[..., Any] | None = None

    @property
    def node(self) -> Callable[..., Any]:
        """抽象节点的实现：`compact` 走的就是它（摘要模型经服务注入）。"""
        if self._node is None:
            self._node = make_compact_node(self._service_ctx.summary_model)
        return self._node


def apply(ctx: Any) -> None:
    Compaction(ctx)
    ctx.logger.info('压缩节点已就绪')


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'inject': ['summary_model'],
}
