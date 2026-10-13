"""沙箱授权文件的硬阻断（A3 用例）。

授权文件（`<工作区>/.my_agent/sandbox.json` 与 `~/.my_agent/sandbox.json`）读写都
**硬阻断、永不提示**：内存缓存优先让写入当场不生效，但重启后会被采纳——那是真实的
持久化越权路径。同时不能误伤：只拦这两个文件本身，工作区里同名文件和技能目录照常。
"""

from pathlib import Path

import pytest

from app.agent import sandbox
from app.agent.runtime import current_workspace
from app.exceptions import SandboxDenied


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
