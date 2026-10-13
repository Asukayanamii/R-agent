"""builtin/compaction：可选插件——服务 ``compaction``：`compact` 节点的实现。

**它的旋钮就是它的配置**（`Config`）：开关、触发比例、保留预算、上下文窗口。
默认值引用总体配置（`.env`）——改一处仍然够用；将来前端要改设置，
改的就是这个插件配置的覆盖层（装配时 base 与覆盖 dict 合并）。

卸下它 = 不再自动压缩（骨架里的 `compact` 退化成透传节点），其余照常——
与 `COMPACT_ENABLED=0` 同一语义。**手动** `/chat/compact` 不受 `enabled` 影响
（走 `LangGraphRunner.compact`，`force=True` 跳过所有闸门）。
"""

from __future__ import annotations

from typing import Any, Callable

from app.agent.compaction.policy import CompactionSettings
from app.agent.nodes import make_compact_node
from app.kernel import Schema, Service

PLUGIN_ID = 'builtin/compaction'


class Config(Schema):
    enabled = Schema.boolean().default(True).description('关掉自动压缩；手动 /chat/compact 照常')
    trigger_ratio = Schema.number().default(0.8).description('触发线 = 上下文窗口 × 该比例')
    keep_tokens = Schema.integer().default(20_000).description('压缩时保留的最近原文预算（tokens）')
    context_window = Schema.integer().default(64_000).description('模型的上下文窗口（tokens）')


def default_config() -> dict:
    """插件默认配置：引用总体配置（`.env`），保持"改一处"的习惯。"""
    settings = CompactionSettings.from_app_config()
    return {
        'enabled': settings.enabled,
        'trigger_ratio': settings.trigger_ratio,
        'keep_tokens': settings.keep_tokens,
        'context_window': settings.context_window,
    }


class Compaction(Service):
    """服务 ``compaction``：把压缩节点暴露成骨架上的 `compact`，并带住这份配置。"""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, 'compaction')
        self.settings = CompactionSettings.from_mapping(config)
        self._node: Callable[..., Any] | None = None

    @property
    def node(self) -> Callable[..., Any]:
        """抽象节点的实现：`compact` 走的就是它（摘要模型经服务注入）。"""
        if self._node is None:
            self._node = make_compact_node(
                self._service_ctx.summary_model,
                self.settings,
                self._service_ctx.get('retry'),
            )
        return self._node


def apply(ctx: Any, config: Any = None) -> None:
    compaction = Compaction(ctx, config)
    settings = compaction.settings
    # 内核日志的 % 格式化只认 %s/%d/%f 这类码，不支持宽度与精度——百分比先算成整数再进格式串
    ctx.logger.info(
        '压缩节点已就绪：窗口 %d，触发 %d%%（%d），保留 %d%s',
        settings.context_window,
        int(settings.trigger_ratio * 100),
        settings.trigger_tokens,
        settings.keep_tokens,
        '' if settings.enabled else '；自动压缩已关（手动照常）',
    )


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'inject': ['summary_model'],
    'Config': Config,
}
