"""资源登记：``ctx.resource``。

插件拿长连接/句柄的**正路**：``acquire()`` 打开资源，并把"关闭"登记成调用方 fiber
的 effect——插件卸载（或热重载）时自动 ``aclose()`` / ``close()`` / ``__exit__``，
不需要手写 teardown。

与恶意插件的边界要诚实：进程内拦不住 ``import os`` 直连的 socket。登记 API 保证的是
"通过它拿的东西，卸载时一定收干净"（配合"卸载无残留"用例）。
"""

from __future__ import annotations

from typing import Any, Callable

from .utils import CallerBound, is_awaitable

__all__ = ['ResourceService']


def _close_handler(resource: Any) -> tuple[Any, Callable[[], Any]]:
    """把 ``acquire`` 的返回值解成 ``(交给调用方的句柄, 关闭函数)``。

    支持四种形态：自带 ``aclose()``/``close()`` 的对象、同步上下文管理器、
    裸 disposer。异步上下文管理器（``__aenter__``）不支持——进入它必须 await，
    而 ``acquire`` 是同步 API；那种资源用 ``ctx.effect(open_fn)``。
    """
    if is_awaitable(resource):
        # 拒绝之前先把它关掉：否则调用方拿到 TypeError 的同时，还背一句
        # "coroutine was never awaited" 的警告（这个协程是我们调 open_fn 造出来的，
        # 没有别人持有它）
        close = getattr(resource, 'close', None)
        if callable(close):
            close()
        raise TypeError(
            'acquire 拿到的是协程：open_fn 要返回"打开后就绪"的句柄，'
            '协程形式的资源请用 ctx.effect(open_fn)'
        )
    if hasattr(resource, '__aenter__') and hasattr(resource, '__aexit__'):
        raise TypeError(
            'acquire 不支持异步上下文管理器（进入它必须 await）：'
            '改用 ctx.effect(open_fn)，或在 open_fn 里自行 aenter 后返回句柄'
        )
    if hasattr(resource, '__enter__') and hasattr(resource, '__exit__'):
        value = resource.__enter__()
        return value, lambda: resource.__exit__(None, None, None)
    for name in ('aclose', 'close'):
        method = getattr(resource, name, None)
        if callable(method):
            return resource, method
    if callable(resource):
        return resource, resource
    raise TypeError(
        'acquire 认不出这个返回值：需要自带 close()/aclose() 的对象、上下文管理器，或可调用对象'
    )


class ResourceService(CallerBound):
    """``ctx.resource``：打开资源并登记关闭，成对出现。"""

    def __init__(self, ctx: Any) -> None:
        self._service_ctx = ctx

    def acquire(self, open_fn: Callable[[], Any], label: str = 'ctx.resource.acquire()') -> Any:
        """打开一个资源并登记关闭；返回调用方要用的那个句柄。"""
        resource, disposer = _close_handler(open_fn())
        self.ctx.effect(lambda: disposer, label)
        return resource
