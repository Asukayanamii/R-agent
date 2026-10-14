"""路径锚点：宿主把内核的"相对路径"钉在项目根上。

内核不依赖 ``app.*``，所以它不知道项目根在哪；宿主（R-agent 的装配根）用
:func:`set_root` 把锚点钉在 ``PROJECT_ROOT``。此后内核里一切相对路径都按锚点解析，
不再随"从哪个目录启动进程"漂移——从 PyCharm 直接运行时 cwd 不是项目根，
不锚定的相对路径会静默指到别处去。

没有调用过 :func:`set_root` 时退回进程当前目录，与内核独立运行时的行为一致。
"""

from __future__ import annotations

import os

__all__ = ['set_root', 'get_root', 'resolve']

_root: str | None = None


def set_root(path: str | os.PathLike) -> None:
    """把相对路径的锚点钉到 ``path``（由宿主的装配根调用一次）。"""
    global _root
    _root = os.path.abspath(os.fspath(path))


def get_root() -> str:
    """当前锚点；没有设置过时是进程当前目录。"""
    return _root if _root is not None else os.getcwd()


def resolve(path: str | os.PathLike) -> str:
    """把路径解析为绝对路径：绝对路径原样归一化，相对路径挂到锚点下。"""
    text = os.fspath(path)
    if os.path.isabs(text):
        return os.path.abspath(text)
    return os.path.abspath(os.path.join(get_root(), text))
