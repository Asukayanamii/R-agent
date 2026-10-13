"""builtin/approvals：可选插件——服务 ``approvals``：哪些工具执行前要人工确认。

原来的模块级 `APPROVAL_REQUIRED` 变成这里的插件配置（``{"required": [...]}``）。
它是**收紧类**能力：名单在每次执行时现取，改名单即时生效（§4.3）——
与"放宽类走回合边界"相对，安全闸门晚一步都可能出事。
"""

from __future__ import annotations

from typing import Any

from app.kernel import Schema, Service

PLUGIN_ID = 'builtin/approvals'


class Config(Schema):
    required = Schema.array(Schema.string()).default([]).description('需要人工确认的工具名')


class Approvals(Service):
    """服务 ``approvals``：审批名单。"""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, 'approvals')
        options = dict(config or {})
        self.required = frozenset(options.get('required') or ())

    def requires(self, name: str) -> bool:
        return name in self.required


def apply(ctx: Any, config: Any = None) -> None:
    approvals = Approvals(ctx, config)
    if approvals.required:
        ctx.logger.info('审批策略已就绪：需要确认的工具 %s', '、'.join(sorted(approvals.required)))
    else:
        ctx.logger.info('审批策略已就绪：无（工具执行不弹确认）')


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'Config': Config,
}
