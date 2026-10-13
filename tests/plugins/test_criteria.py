"""阶段 C 完成判据（①②③④）的插件层用例。

① 换一份 webui 插件目录 = 换个前端，内核零改动；
② 停用（卸载）webui → `/ui` 404——`/chat/*` 不受影响（全应用视角在冒烟里验证）；
③ webui 的 `requires` 改成 protocol:v2（不存在）→ **拒绝装载**并报明原因；
④ 桌面端加载的就是同一个 `/ui`：停用后拿到的是明确的 404（而不是白屏或 500）。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.harness.assets import ASSETS_STATE_ATTR, AssetRegistry, assets_router
from app.kernel import Context, CordisError
from app.plugins.builtin import protocol, webui

pytestmark = pytest.mark.anyio


def write_frontend(root, title: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'index.html').write_text(f'<!doctype html><title>{title}</title>', encoding='utf-8')


def make_webui(directory, *, requires='protocol:v1', name='workspace/other-webui'):
    """与 builtin/webui 同形的另一份前端插件（换前端 = 换这个目录）。"""

    def apply(ctx):
        ctx.assets.mount('/ui', directory)

    return {'apply': apply, 'name': name, 'requires': requires}


def make_client(registry: AssetRegistry) -> TestClient:
    app = FastAPI()
    app.include_router(assets_router())
    setattr(app.state, ASSETS_STATE_ATTR, registry)
    return TestClient(app)


@pytest.fixture
def assembled(tmp_path):
    """协议已就位、资产表已建好的一棵插件树。"""
    ctx = Context()
    registry = AssetRegistry(ctx)
    return ctx, registry, make_client(registry)


async def test_swap_webui_directory_serves_another_frontend(assembled, tmp_path):
    ctx, registry, client = assembled
    await ctx.plugin(protocol.plugin)

    write_frontend(tmp_path / 'front-a', '前端 A')
    first = ctx.plugin(make_webui(tmp_path / 'front-a'))
    await first
    assert '前端 A' in client.get('/ui/').text

    # 卸下第一份、换上第二份：内核侧零改动
    await first.dispose()
    assert client.get('/ui/').status_code == 404

    write_frontend(tmp_path / 'front-b', '前端 B')
    second = ctx.plugin(make_webui(tmp_path / 'front-b'))
    await second
    assert '前端 B' in client.get('/ui/').text


async def test_disabling_webui_returns_404(assembled, tmp_path):
    ctx, registry, client = assembled
    await ctx.plugin(protocol.plugin)

    write_frontend(tmp_path / 'front', '前端')
    fiber = ctx.plugin(make_webui(tmp_path / 'front'))
    await fiber
    response = client.get('/ui/')
    assert response.status_code == 200

    await fiber.dispose()
    # 桌面端（pywebview）加载的也是这个地址：拿到的是明确的 404，而不是白屏
    failed = client.get('/ui/')
    assert failed.status_code == 404
    assert '未装载' in failed.text


async def test_requires_unknown_version_is_rejected(assembled, tmp_path):
    ctx, registry, client = assembled
    await ctx.plugin(protocol.plugin)
    write_frontend(tmp_path / 'front', '前端')

    with pytest.raises(CordisError) as info:
        ctx.plugin(make_webui(tmp_path / 'front', requires='protocol:v2'))

    # 报明原因，并报出当前可用的是哪些
    assert 'protocol:v2' in str(info.value)
    assert 'protocol:v1' in str(info.value)


async def test_requires_before_provider_is_rejected(assembled, tmp_path):
    """装载顺序反了也不行：形状不对，等不来（不是 PENDING）。"""
    ctx, registry, client = assembled
    write_frontend(tmp_path / 'front', '前端')

    with pytest.raises(CordisError):
        ctx.plugin(make_webui(tmp_path / 'front'))


async def test_real_webui_plugin_serves_the_bundled_frontend(assembled):
    ctx, registry, client = assembled
    await ctx.plugin(protocol.plugin)
    await ctx.plugin(webui.plugin)

    response = client.get('/ui/')
    assert response.status_code == 200
    assert 'my-agent' in response.text or '<html' in response.text.lower()
    # 自带资产（vendor）也能取到
    assert client.get('/ui/vendor/marked.umd.js').status_code == 200
