"""沙箱授权文件的硬阻断（A3 用例）与策略槽（K2 用例）。

授权文件（`<工作区>/.my_agent/sandbox.json` 与 `~/.my_agent/sandbox.json`）读写都
**硬阻断、永不提示**：内存缓存优先让写入当场不生效，但重启后会被采纳——那是真实的
持久化越权路径。同时不能误伤：只拦这两个文件本身，工作区里同名文件和技能目录照常。

策略槽由 `builtin/sandbox` 插件装载时安装（禁区名单 / 受信只读根 / 是否询问），
卸载即回默认——默认行为与改造前一致。
"""

from pathlib import Path

import pytest

from app.agent import sandbox
from app.agent.runtime import current_workspace
from app.common.exceptions import SandboxDenied
from app.kernel import Context
from app.plugins.builtin import sandbox as sandbox_plugin

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    """每个用例换一个临时工作区，并把模块级的授权缓存清空（不读用户的真实授权）。"""
    monkeypatch.setattr(sandbox, '_memory', {})
    monkeypatch.setattr(sandbox, '_global', sandbox._Grant())
    token = current_workspace.set(tmp_path)
    yield tmp_path
    current_workspace.reset(token)


@pytest.fixture
def prompts(monkeypatch):
    """记录 interrupt 是否被调用——硬阻断必须先于任何询问。"""
    asked = []
    monkeypatch.setattr(sandbox, 'interrupt', lambda payload: asked.append(payload))
    return asked


@pytest.mark.parametrize('mode', [sandbox.READ, sandbox.WRITE])
def test_workspace_auth_file_is_hard_blocked(workspace, mode, prompts):
    target = workspace / sandbox.CONFIG_DIR / sandbox.CONFIG_FILE

    with pytest.raises(SandboxDenied) as info:
        sandbox.guard_path(str(target), mode)

    assert '受保护路径' in str(info.value)
    assert prompts == []  # 永不提示


@pytest.mark.parametrize('mode', [sandbox.READ, sandbox.WRITE])
def test_global_auth_file_is_hard_blocked(workspace, mode, prompts):
    # 只做路径判定，不碰用户的真实 ~/.my_agent/sandbox.json（拒绝发生在任何读写之前）
    target = Path.home() / sandbox.CONFIG_DIR / sandbox.CONFIG_FILE

    with pytest.raises(SandboxDenied) as info:
        sandbox.guard_path(str(target), mode)

    assert '受保护路径' in str(info.value)
    assert prompts == []


@pytest.mark.parametrize('mode', [sandbox.READ, sandbox.WRITE])
def test_plugin_trust_file_is_hard_blocked(workspace, mode, prompts):
    """插件授权清单同一条检查：agent 不能读它、更不能自己给自己批准插件（阶段 E）。"""
    target = workspace / sandbox.CONFIG_DIR / 'plugins.json'

    with pytest.raises(SandboxDenied) as info:
        sandbox.guard_path(str(target), mode)

    assert '受保护路径' in str(info.value)
    assert prompts == []


def test_same_named_file_elsewhere_is_not_hard_blocked(workspace):
    """同名文件不误伤：工作区里别处的 sandbox.json 是普通文件，照常放行。"""
    target = workspace / 'notes' / 'sandbox.json'

    resolved = sandbox.guard_path(str(target), sandbox.WRITE)

    assert resolved == target.resolve()


def test_skill_file_still_writable(workspace):
    """技能目录照常可写：禁区只收授权文件本身。"""
    target = workspace / sandbox.CONFIG_DIR / 'skills' / 'demo' / 'SKILL.md'

    resolved = sandbox.guard_path(str(target), sandbox.WRITE)

    assert resolved == target.resolve()


# ------------------------------------------------------------------ 策略槽（K2）


def test_policy_default_is_the_old_behaviour():
    """没有插件时：策略就是模块里的常量（受信根只来自 .env 的 SKILLS_DIR）。"""
    policy = sandbox.current_policy()

    assert policy.deny_read == sandbox.DENY_READ
    assert policy.deny_write == sandbox.DENY_WRITE
    assert policy.ask is True


def test_policy_can_add_deny_patterns(workspace, prompts):
    """自定义禁区名单生效：命中就硬阻断（永不提示）。"""
    previous = sandbox.use_policy(
        sandbox.SandboxPolicy(deny_read=(*sandbox.DENY_READ, '*.secret'))
    )
    try:
        target = workspace / 'notes.secret'
        with pytest.raises(SandboxDenied):
            sandbox.guard_path(str(target), sandbox.READ)
        assert prompts == []
    finally:
        sandbox.use_policy(previous)


def test_policy_ask_false_refuses_outside_workspace(workspace, prompts, tmp_path):
    """`ask=False`：工作区外直接拒绝，不弹卡片（无人值守部署的收紧类策略）。"""
    outside = tmp_path.parent / 'outside.txt'
    previous = sandbox.use_policy(sandbox.SandboxPolicy(ask=False))
    try:
        with pytest.raises(SandboxDenied) as info:
            sandbox.guard_path(str(outside), sandbox.READ)
        assert '不询问' in str(info.value)
        assert prompts == []
    finally:
        sandbox.use_policy(previous)


async def test_plugin_installs_and_restores_the_policy(workspace):
    """插件装载安装策略、卸载还原默认——成对出现，不留全局残留。"""
    ctx = Context()
    fiber = ctx.plugin(sandbox_plugin.plugin, {'ask': False, 'trusted_read_roots': ['X:/lib']})
    await fiber

    policy = sandbox.current_policy()
    assert policy.ask is False
    assert policy.trusted_read_roots == ('X:/lib',)

    result = fiber.dispose()
    if result is not None:
        await result
    assert sandbox.current_policy().ask is True
    assert sandbox.current_policy().trusted_read_roots == ()


async def test_service_trust_extends_read_roots(workspace, prompts, tmp_path):
    """`trust(path)`：把应用自己的库加进受信只读根，读它不弹授权。"""
    library = tmp_path.parent / 'library'
    library.mkdir(exist_ok=True)
    (library / 'SKILL.md').write_text('# 技能', encoding='utf-8')

    ctx = Context()
    fiber = ctx.plugin(sandbox_plugin.plugin, {'trusted_read_roots': []})
    await fiber
    try:
        # 先确认它本来要走"弹卡片"那条路（prompts 记一笔），而不是被硬阻断
        sandbox.guard_path(str(library / 'SKILL.md'), sandbox.READ)
        assert len(prompts) == 1

        sandbox._memory.clear()  # 清掉"本次运行有效"的内存授权：第二次只能靠受信根
        ctx.get('sandbox').trust(library)
        resolved = sandbox.guard_path(str(library / 'SKILL.md'), sandbox.READ)

        assert resolved == (library / 'SKILL.md').resolve()
        assert len(prompts) == 1  # 没再问
    finally:
        result = fiber.dispose()
        if result is not None:
            await result
