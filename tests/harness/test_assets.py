"""资产路由测试（C2）：表驱动、装卸即时生效、路由表不动、穿越被兜住。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.harness.assets import ASSETS_STATE_ATTR, AssetRegistry, assets_router
from app.kernel import Context

pytestmark = pytest.mark.anyio


def write_assets(root, title: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'index.html').write_text(f'<!doctype html><title>{title}</title>', encoding='utf-8')
    nested = root / 'nested'
    nested.mkdir(exist_ok=True)
    (nested / 'app.js').write_text('console.log(1)', encoding='utf-8')


def make_client(registry: AssetRegistry) -> TestClient:
    app = FastAPI()
    app.include_router(assets_router('ui'))
    setattr(app.state, ASSETS_STATE_ATTR, registry)
    return TestClient(app)


async def test_mount_resolve_and_withdraw(tmp_path):
    ctx = Context()
    registry = AssetRegistry(ctx)
    write_assets(tmp_path / 'webui', 'v1')

    handle = registry.mount('/ui', tmp_path / 'webui')
    assert registry.resolve('ui', '') == (tmp_path / 'webui' / 'index.html').resolve()
    assert registry.resolve('/ui', 'nested/app.js') is not None
    assert registry.resolve('ui', 'missing.js') is None

    handle()
    assert registry.resolve('ui', '') is None


async def test_mount_follows_plugin_lifecycle(tmp_path):
    ctx = Context()
    registry = AssetRegistry(ctx)
    write_assets(tmp_path / 'webui', 'v1')

    def plugin(ctx):
        ctx.assets.mount('/ui', tmp_path / 'webui')

    fiber = ctx.plugin(plugin)
    await fiber
    assert registry.resolve('ui', '') is not None

    await fiber.dispose()
    assert registry.resolve('ui', '') is None


async def test_traversal_is_refused(tmp_path):
    ctx = Context()
    registry = AssetRegistry(ctx)
    write_assets(tmp_path / 'webui', 'v1')
    (tmp_path / 'secret.txt').write_text('机密', encoding='utf-8')
    registry.mount('/ui', tmp_path / 'webui')

    assert registry.resolve('ui', '../secret.txt') is None
    assert registry.resolve('ui', 'nested/../../secret.txt') is None
    assert registry.resolve('ui', '..\\secret.txt') is None


async def test_router_serves_and_404s(tmp_path):
    ctx = Context()
    registry = AssetRegistry(ctx)
    client = make_client(registry)

    # 未装载
    assert client.get('/ui').status_code == 404
    assert client.get('/ui/').status_code == 404
    assert client.get('/ui/nested/app.js').status_code == 404

    routes_before = len(client.app.routes)
    write_assets(tmp_path / 'webui', 'v1')
    registry.mount('/ui', tmp_path / 'webui')

    response = client.get('/ui')
    assert response.status_code == 200
    assert 'v1' in response.text
    assert client.get('/ui/').status_code == 200
    assert client.get('/ui/nested/app.js').status_code == 200
    assert client.get('/ui/missing.js').status_code == 404
    assert client.get('/ui/..%2fsecret.txt').status_code == 404

    # 换一份资产：立即生效；路由表长度全程不变
    write_assets(tmp_path / 'webui2', 'v2')
    registry.mount('/ui', tmp_path / 'webui2')
    assert 'v2' in client.get('/ui/').text

    registry.mounts.clear()
    assert client.get('/ui/').status_code == 404
    assert len(client.app.routes) == routes_before


async def test_mount_requires_existing_directory(tmp_path):
    ctx = Context()
    registry = AssetRegistry(ctx)
    with pytest.raises(ValueError):
        registry.mount('/ui', tmp_path / 'nope')
