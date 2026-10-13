"""内置 skillbook（阶段 L）：随包教学技能库 + 受信只读根。

判据：装载后系统提示词的技能索引里出现 `r-agent-plugins`（作用域标"内置"）、
读它的正文不弹授权卡；卸下则索引里消失、受信根一起收回。
"""

from pathlib import Path

import pytest

from app.agent import sandbox
from app.agent.runtime import current_workspace
from app.agent.skills import load_skills
from app.kernel import Context
from app.plugins.builtin import context as context_plugin
from app.plugins.builtin import sandbox as sandbox_plugin
from app.plugins.builtin import skillbook

pytestmark = pytest.mark.anyio

SKILL_MD = Path(skillbook.SKILLS_DIR) / 'r-agent-plugins' / 'SKILL.md'


def service_roots(ctx) -> tuple[str, ...]:
    service = ctx.get('skill_roots')
    return service.roots() if service is not None else ()


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """技能库在工作区之外——所以"读它放行"只能来自受信根。"""
    monkeypatch.setattr(sandbox, '_memory', {})
    monkeypatch.setattr(sandbox, '_global', sandbox._Grant())
    token = current_workspace.set(tmp_path)
    yield tmp_path
    current_workspace.reset(token)


@pytest.fixture
def prompts(monkeypatch):
    asked = []
    monkeypatch.setattr(sandbox, 'interrupt', lambda payload: asked.append(payload))
    return asked


async def dispose(fiber) -> None:
    result = fiber.dispose()
    if result is not None:
        await result


async def test_skillbook_lands_in_the_skill_index(workspace):
    ctx = Context()
    await ctx.plugin(
        context_plugin.plugin,
        {'skills_enabled': True, 'skills_dir': '', 'agents_md_enabled': False, 'ancestors': False},
    )
    assert 'r-agent-plugins' not in ctx.get('context').static(str(workspace))

    fiber = ctx.plugin(skillbook.plugin, {'enabled': True})
    await fiber
    try:
        block = ctx.get('context').static(str(workspace))
        assert 'r-agent-plugins' in block          # 索引里出现了
        assert 'skillbook' in block                # 位置指向随包技能库
        assert '写插件' in block                    # 描述照"什么时候用它"写

        # 作用域标"内置"（渲染层只给名字/描述/路径，作用域用于日志与排障）
        skills = load_skills(str(workspace), global_dir='', enabled=True,
                             extra_dirs=service_roots(ctx))
        entry = next(item for item in skills if item.name == 'r-agent-plugins')
        assert entry.scope == '内置'
    finally:
        await dispose(fiber)

    assert 'r-agent-plugins' not in ctx.get('context').static(str(workspace))


async def test_skillbook_trusts_its_read_root(workspace, prompts):
    """装载时把库目录加进受信只读根：模型按索引读正文不弹授权。"""
    ctx = Context()
    await ctx.plugin(sandbox_plugin.plugin, {'trusted_read_roots': []})

    fiber = ctx.plugin(skillbook.plugin, {'enabled': True})
    await fiber
    try:
        resolved = sandbox.guard_path(str(SKILL_MD), sandbox.READ)
        assert resolved == SKILL_MD.resolve()
        assert prompts == [], '走受信根，不该弹授权卡'
    finally:
        await dispose(fiber)

    # 卸下后受信根一起收回：这条路径重新需要授权
    sandbox.guard_path(str(SKILL_MD), sandbox.READ)
    assert len(prompts) == 1


async def test_skillbook_can_be_disabled(workspace):
    ctx = Context()
    fiber = ctx.plugin(skillbook.plugin, {'enabled': False})
    await fiber
    try:
        service = ctx.get('skill_roots')
        assert service is not None and service.roots() == ()
    finally:
        await dispose(fiber)
