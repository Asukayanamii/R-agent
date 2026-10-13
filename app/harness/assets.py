"""资产注册表与那条动态路由（设计稿 §2.5 的"界面"类接缝）。

界面类插件的静态资产走**表驱动**：装载/卸载只改"alias → 目录"这张表，
**不动 Starlette 的路由表**（运行时增删路由没有正式 API，也是一个已知坑）。
未装载、文件不存在、路径穿越 → 一律 404。

路由由 :func:`assets_router` 提供（默认 ``/ui``）；注册表放在
``request.app.state.assets``——装配根在 lifespan 里放进去，所以路由表本身任何时刻都不变。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.kernel import Service

__all__ = ['AssetRegistry', 'assets_router', 'ASSETS_STATE_ATTR']

ASSETS_STATE_ATTR = 'assets'
"""资产注册表在 ``app.state`` 上的属性名（路由与装配根共享这个约定）。"""


def _alias_key(alias: str) -> str:
    """别名归一化：``/ui`` 与 ``ui`` 是同一个挂点。"""
    return alias.strip('/')


class AssetRegistry(Service):
    """``ctx.assets``：alias → 目录的注册表。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx, 'assets')
        self.mounts: dict[str, Path] = {}

    def mount(self, alias: str, directory: str | Path) -> Any:
        """把一个目录挂到 alias 下；返回撤销句柄，随调用方 fiber 卸载自动撤销。"""
        key = _alias_key(alias)
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError(f'资产目录不存在：{root}')

        def install() -> Callable[[], bool]:
            self.mounts[key] = root

            def withdraw() -> bool:
                if self.mounts.get(key) == root:
                    self.mounts.pop(key, None)
                    return True
                return False

            return withdraw

        return self.ctx.effect(install, f'ctx.assets.mount({alias!r})')

    def resolve(self, alias: str, relative: str) -> Path | None:
        """把请求路径解析成文件；未装载、越界、不是文件都返回 ``None``（调用方给 404）。"""
        root = self.mounts.get(_alias_key(alias))
        if root is None:
            return None
        candidate = (root / (relative or 'index.html')).resolve()
        if candidate != root and root not in candidate.parents:
            return None
        return candidate if candidate.is_file() else None


def assets_router(alias: str = 'ui') -> APIRouter:
    """一条动态路由：查表 → FileResponse；未装载/不存在/穿越 → 404。

    注册表按请求从 ``app.state`` 取——装配根启动时放进去一次，路由表从不增删。
    """
    router = APIRouter()

    @router.get(f'/{alias}')
    @router.get(f'/{alias}/{{path:path}}')
    async def serve_asset(request: Request, path: str = ''):
        registry = getattr(request.app.state, ASSETS_STATE_ATTR, None)
        target = registry.resolve(alias, path) if registry is not None else None
        if target is None:
            raise HTTPException(status_code=404, detail='资产未装载或不存在')
        return FileResponse(target)

    return router
