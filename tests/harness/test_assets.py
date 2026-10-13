"""资产路由测试（C2）：表驱动、装卸即时生效、路由表不动、穿越被兜住。

加上"一进程多前端"那组（一个内核进程、多个前端并发访问）：默认 alias 之外还能挂任意 alias，
请求按别名查表——装载即 200、卸载即 404，通用规则不许遮蔽业务路由。
"""

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.harness.assets import ASSETS_STATE_ATTR, AssetRegistry, assets_router
from app.harness.composer import PLUGIN_SCOPE
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


# ------------------------------------------------------------------ 一进程多前端


def make_app(ctx: Context) -> FastAPI:
    """与装配根同形：注册表放 app.state；业务路由在前、资产路由在后。"""
    app = FastAPI()

    @app.get('/chat/skills')
    async def skills() -> dict:
        return {'ok': True}

    app.include_router(assets_router())
    setattr(app.state, ASSETS_STATE_ATTR, ctx.get('assets'))
    return app


def write_panel(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'index.html').write_text(f'<h1>{text}</h1>', encoding='utf-8')
    return directory


async def mount_plugin(ctx: Context, alias: str, directory: Path):
    """挂一个 alias（真走插件装载：effect 挂在 fiber 上，卸载即撤销）。

    先接住 fiber 再 `await`：`await ctx.plugin(...)` 等到的是**插件的返回值**，不是 fiber。
    """

    def apply(context) -> None:
        context.assets.mount(alias, directory)

    fiber = ctx.plugin({'apply': apply, 'name': f'test/panel-{alias}'})
    await fiber
    return fiber


async def get(app: FastAPI, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.get(path)


async def test_two_frontends_serve_concurrently(tmp_path):
    ctx = Context()
    AssetRegistry(ctx)
    app = make_app(ctx)
    await mount_plugin(ctx, 'panel-a', write_panel(tmp_path / 'a', 'panel A'))
    await mount_plugin(ctx, 'panel-b', write_panel(tmp_path / 'b', 'panel B'))

    # 真并发：两个前端的请求同时在飞，互不影响（按 alias 查表，请求之间没有共享状态）
    alpha, beta = await asyncio.gather(
        get(app, '/panel-a/index.html'),
        get(app, '/panel-b/index.html'),
    )

    assert alpha.status_code == 200 and 'panel A' in alpha.text
    assert beta.status_code == 200 and 'panel B' in beta.text


async def test_unmounted_alias_is_404(tmp_path):
    ctx = Context()
    AssetRegistry(ctx)
    app = make_app(ctx)

    assert (await get(app, '/panel-a/index.html')).status_code == 404


async def test_unload_takes_the_alias_away(tmp_path):
    """卸载即 404：撤销挂在 fiber 上，不用动路由表。"""
    ctx = Context()
    AssetRegistry(ctx)
    app = make_app(ctx)
    fiber = await mount_plugin(ctx, 'panel-a', write_panel(tmp_path / 'a', 'panel A'))
    assert (await get(app, '/panel-a/index.html')).status_code == 200

    await fiber.dispose()

    assert (await get(app, '/panel-a/index.html')).status_code == 404


async def test_default_alias_wins_over_the_generic_rule(tmp_path):
    """`/ui` 走默认那条：通用规则排在后面，不能抢走默认前端。"""
    ctx = Context()
    AssetRegistry(ctx)
    app = make_app(ctx)
    await mount_plugin(ctx, 'ui', write_panel(tmp_path / 'ui', 'default front'))

    assert 'default front' in (await get(app, '/ui')).text


async def test_generic_rule_does_not_shadow_business_routes(tmp_path):
    """通用规则排在业务路由之后：`/chat/skills` 仍然归 chat，不会落到资产表上。"""
    ctx = Context()
    AssetRegistry(ctx)
    app = make_app(ctx)
    await mount_plugin(ctx, 'chat', write_panel(tmp_path / 'chat', 'not this'))

    response = await get(app, '/chat/skills')

    assert response.status_code == 200 and response.json() == {'ok': True}


async def test_scope_marker_is_irrelevant_for_assets(tmp_path):
    """资产是**进程级**的（与结构不同，不按工作区分域）——这条行为写成用例钉住。

    插件在 `PLUGIN_SCOPE` 作用域里挂的 alias，在任何会话看到的都是同一张表。
    """
    ctx = Context()
    AssetRegistry(ctx)
    app = make_app(ctx)
    token = PLUGIN_SCOPE.set('scope-x')
    try:
        await mount_plugin(ctx, 'panel-a', write_panel(tmp_path / 'a', 'panel A'))
    finally:
        PLUGIN_SCOPE.reset(token)

    assert (await get(app, '/panel-a/index.html')).status_code == 200
