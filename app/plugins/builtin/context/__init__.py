"""builtin/context：可选插件——服务 ``context``：技能索引 + AGENTS.md 的静态注入。

它负责"每轮都该生效的那段静态上下文"（项目约定与技能索引，见 README）。
卸下它就不再注入——模型只能靠工具自己去找约定文件，属于功能退化。
"""

from __future__ import annotations

from typing import Any

from app.agent.prompts import render_static_context
from app.kernel import Service

PLUGIN_ID = 'builtin/context'


class ContextService(Service):
    """服务 ``context``：给出某工作区的静态上下文块。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx, 'context')

    def static(self, workspace: str | None) -> str:
        """本工作区的静态上下文块（项目约定 + 技能索引）。"""
        return render_static_context(workspace)


def apply(ctx: Any) -> None:
    ContextService(ctx)
    ctx.logger.info('静态上下文注入已就绪（项目约定 + 技能索引）')


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
}
