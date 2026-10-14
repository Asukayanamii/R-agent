"""builtin/context：可选插件——服务 ``context``：技能索引 + AGENTS.md 的静态注入。

**它的旋钮就是它的配置**（`Config`）：技能开关与全局库路径、约定注入开关与祖先遍历。
默认值引用总体配置（`.env`）——改一处仍然够用；将来前端要改设置，
改的就是这个插件配置的覆盖层（装配时 base 与覆盖 dict 合并）。

它负责"每轮都该生效的那段静态上下文"。卸下它就不再注入——模型只能靠工具自己去找
约定文件，属于功能退化。
"""

from __future__ import annotations

from typing import Any

from app.agent.prompts import ContextSettings, render_static_context
from app.kernel import Schema, Service

PLUGIN_ID = 'builtin/context'


class Config(Schema):
    skills_enabled = Schema.boolean().default(True).description('发现技能并把索引注入提示词')
    skills_dir = Schema.string().default('').description('全局技能库（同时是沙箱的受信只读根）')
    agents_md_enabled = Schema.boolean().default(True).description('注入 AGENTS.md / CLAUDE.md')
    ancestors = Schema.boolean().default(True).description('向上遍历祖先目录找约定文件')


def default_config() -> dict:
    """插件默认配置：引用总体配置（`.env`）。"""
    settings = ContextSettings.from_app_config()
    return {
        'skills_enabled': settings.skills_enabled,
        'skills_dir': settings.skills_dir,
        'agents_md_enabled': settings.agents_md_enabled,
        'ancestors': settings.ancestors,
    }


class ContextService(Service):
    """服务 ``context``：给出某工作区的静态上下文块。"""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, 'context')
        self.settings = ContextSettings.from_mapping(config)

    def static(self, workspace: str | None) -> str:
        """本工作区的静态上下文块（项目约定 + 技能索引）。"""
        return render_static_context(
            workspace, self.settings, extra_skill_dirs=self._extra_roots()
        )

    def _extra_roots(self) -> tuple[str, ...]:
        """插件提供的技能库（如 `builtin/skillbook`）——**现取**：装载顺序与热重载都不影响。"""
        service = self._service_ctx.get('skill_roots')
        return tuple(service.roots()) if service is not None else ()


def apply(ctx: Any, config: Any = None) -> None:
    service = ContextService(ctx, config)
    settings = service.settings
    ctx.logger.info(
        '静态上下文注入已就绪：技能 %s%s、约定 %s%s',
        '开' if settings.skills_enabled else '关',
        f'（全局库 {settings.skills_dir}）' if settings.skills_enabled and settings.skills_dir else '',
        '开' if settings.agents_md_enabled else '关',
        '（含祖先目录）' if settings.agents_md_enabled and settings.ancestors else '',
    )


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'Config': Config,
}
