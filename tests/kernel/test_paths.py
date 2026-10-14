"""路径锚点测试（A1 适配用例）。

内核的相对路径必须能钉在宿主给的项目根上，而不是跟着进程 cwd 漂
——从 PyCharm 直接运行时 cwd 不是项目根。没设锚点时保持旧的 cwd 语义。
"""

import os
from pathlib import Path

import pytest

from app.kernel import Context
from app.kernel.loader import start_app
from app.kernel.paths import get_root, resolve, set_root

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def restore_root():
    """每个用例后把锚点还原，避免相互污染。"""
    import app.kernel.paths as paths

    previous = paths._root
    yield
    paths._root = previous


def test_resolve_defaults_to_cwd(monkeypatch, tmp_path):
    import app.kernel.paths as paths

    monkeypatch.setattr(paths, '_root', None)
    monkeypatch.chdir(tmp_path)
    assert get_root() == str(tmp_path)
    assert resolve('plugins/x.py') == str(tmp_path / 'plugins' / 'x.py')


def test_resolve_anchors_to_root(tmp_path):
    set_root(tmp_path)
    assert get_root() == str(tmp_path)
    assert resolve('plugins/x.py') == str(tmp_path / 'plugins' / 'x.py')
    # 绝对路径不受锚点影响
    elsewhere = tmp_path.parent / 'elsewhere.py'
    assert resolve(str(elsewhere)) == str(elsewhere)


async def test_loader_relative_path_uses_root(tmp_path, monkeypatch):
    """Loader 的配置文件相对路径按锚点解析，不按 cwd。"""
    plugin_dir = tmp_path / 'plugins'
    plugin_dir.mkdir()
    (plugin_dir / 'hello.py').write_text(
        'def apply(ctx):\n'
        "    ctx.provide('hello', 'from-root')\n",
        encoding='utf-8',
    )
    (tmp_path / 'cordis.yml').write_text(
        '- id: hello\n  name: ./plugins/hello.py\n', encoding='utf-8'
    )

    set_root(tmp_path)
    elsewhere = tmp_path.parent / 'not-the-root'
    elsewhere.mkdir(exist_ok=True)
    monkeypatch.chdir(elsewhere)

    ctx = await start_app('cordis.yml')
    assert ctx.get('hello') == 'from-root'
    assert Path(ctx.loader.path) == tmp_path / 'cordis.yml'
