"""builtin/skillbook：内置技能库（可选插件）——服务 ``skill_roots``。

随包带一份**教学技能**（`skills/r-agent-plugins/`）：讲清插件内核的原理、怎么写插件、
怎么启用与排查。技能正文是只读资料，所以装载时顺手把库目录加进沙箱的**受信只读根**
（`ctx.sandbox.trust(...)`，阶段 K2 的服务）——模型按索引去读正文不会弹授权卡片；
沙箱插件没装就跳过（那时读正文会走一次常规授权，功能不坏）。

技能索引由 `builtin/context` 现取 `skill_roots` 合并进系统提示词，所以这个插件
装卸/热重载都即时反映在索引里（正文与受信根随插件生命周期成对出现）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.kernel import Schema, Service

PLUGIN_ID = 'builtin/skillbook'
SKILLS_DIR = Path(__file__).parent / 'skills'
"""随包技能库：里面有 `r-agent-plugins/`（每个技能一个目录，目录里是 SKILL.md）。"""


class Config(Schema):
    enabled = Schema.boolean().default(True).description('关掉 = 不提供这份教学技能库')


class SkillRoots(Service):
    """服务 ``skill_roots``：`roots()` 给出随包技能库目录（`builtin/context` 现取）。"""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, 'skill_roots')
        self.enabled = bool((config or {}).get('enabled', True))
        self.dir = SKILLS_DIR

    def roots(self) -> tuple[str, ...]:
        if not self.enabled or not self.dir.is_dir():
            return ()
        return (str(self.dir),)


def apply(ctx: Any, config: Any = None) -> None:
    service = SkillRoots(ctx, config)
    roots = service.roots()
    if not roots:
        ctx.logger.info('内置技能库已关闭或不存在：%s', SKILLS_DIR)
        return

    # 正文是只读资料：加进沙箱受信只读根，读它不弹授权。
    # `trust()` 返回撤销函数，由**我们**登记成 effect（挂在当前 fiber 上）——插件卸载时
    # 受信根一起收回。服务在不在用 `ctx.get` 问（属性访问要求声明 inject，而这里是可选依赖）。
    sandbox = ctx.get('sandbox')
    for root in roots:
        if sandbox is not None:
            ctx.effect(lambda root=root: sandbox.trust(root), f'skillbook.trust({root})')
        else:
            ctx.logger.warn('沙箱策略服务不在：读内置技能正文会走一次常规授权')
            break

    ctx.logger.info('内置技能库已就绪：%s', '、'.join(roots))


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'Config': Config,
}
