"""图的版本账本：pending/current、租约、兼容判定、回收（设计稿 §4.4）。

守恒、跟抽象层级无关的三条：

- **只在回合边界切换**：``stage()`` 放置 pending，``lease()`` 获取时（= 回合开始）原子替换——
  进行中的回合永远钉在它开始时的那个版本上，新回合拿到新版本；
- **兼容判定**：resume 之前，待确认中断所在**节点名**要在当前 spec 里存在；
  否则停下来报错，而不是把一个旧位置硬塞进新结构；
- **回收**：无在飞租约、未超 LRU 保留数、且不被 protect 判为需要保留的旧版本才可以丢掉。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Callable, Iterable, Iterator

from app.harness.composer import GraphVersion

__all__ = ['VersionRegistry', 'DEFAULT_KEEP']

DEFAULT_KEEP = 4
"""LRU 保留的历史版本数（阶段 E 接 ``GRAPH_VERSIONS_KEEP`` 配置项）。"""


class VersionRegistry:
    """版本账本。所有权单一：谁持有租约，谁就用它拿到的那版图跑完。"""

    def __init__(self, keep: int = DEFAULT_KEEP) -> None:
        self.keep = max(1, keep)
        self._versions: dict[int, GraphVersion] = {}
        self._current: GraphVersion | None = None
        self._pending: GraphVersion | None = None
        self._leases: dict[int, int] = {}
        self._threads: dict[int, set[str]] = {}

    # ---------------------------------------------------------------- 查询
    @property
    def current(self) -> GraphVersion | None:
        return self._current

    @property
    def pending(self) -> GraphVersion | None:
        return self._pending

    def known(self) -> tuple[GraphVersion, ...]:
        return tuple(self._versions.values())

    def lease_count(self, version_id: int) -> int:
        return self._leases.get(version_id, 0)

    def threads_of(self, version_id: int) -> set[str]:
        return set(self._threads.get(version_id, set()))

    # ---------------------------------------------------------------- 版本流转
    def stage(self, version: GraphVersion) -> None:
        """放置一个新版本（pending）；换到运行图上要等下次 ``lease()``（回合边界）。"""
        self._versions[version.id] = version
        self._pending = version
        self._leases.setdefault(version.id, 0)
        self._threads.setdefault(version.id, set())

    def apply_pending(self) -> GraphVersion | None:
        """把 pending 原子替换为 current；返回被替换下来的旧版本（没有 pending 时 None）。"""
        if self._pending is None:
            return None
        previous = self._current
        self._current = self._pending
        self._pending = None
        return previous

    @asynccontextmanager
    async def lease(self, thread_id: str) -> Iterator[GraphVersion]:
        """获取一份租约：进行中的回合钉在当时的版本上，直到退出。

        获取时先应用 pending——**结构变化只在回合边界生效**的落点就在这里。
        """
        self.apply_pending()
        version = self._current
        if version is None:
            raise RuntimeError('还没有可用的图版本：装配未完成或首次合成失败')
        self._leases[version.id] = self._leases.get(version.id, 0) + 1
        self._threads.setdefault(version.id, set()).add(thread_id)
        try:
            yield version
        finally:
            self._leases[version.id] -= 1

    # ---------------------------------------------------------------- 兼容判定
    def missing_nodes(self, node_names: Iterable[str]) -> list[str]:
        """这些节点名里，当前版本没有的部分（resume 兼容判定的依据）。

        待确认中断所在节点在新结构里不存在时，返回非空——调用方据此明确报错，
        而不是把一个已经不在图里的位置硬接着跑。
        """
        if self._current is None:
            return sorted(set(node_names))
        return sorted(set(node_names) - self._current.node_names)

    # ---------------------------------------------------------------- 回收
    def recycle(self, protect: Callable[[set[str]], bool] | None = None) -> list[int]:
        """回收旧版本；返回被丢掉的版本 id（升序）。

        当前版本与 pending 永不回收；有在飞租约的不回收；``protect`` 拿到该版本用过的
        thread 集合，返回 True 表示这片会话还压着它（例如还有待确认中断）。
        """
        alive = sorted(self._versions)
        keep_ids = set(alive[-self.keep :])
        dropped: list[int] = []
        for version_id in alive:
            if version_id in keep_ids:
                continue
            if self._current is not None and version_id == self._current.id:
                continue
            if self._pending is not None and version_id == self._pending.id:
                continue
            if self._leases.get(version_id, 0) > 0:
                continue
            if protect is not None and protect(self._threads.get(version_id, set())):
                continue
            del self._versions[version_id]
            self._leases.pop(version_id, None)
            self._threads.pop(version_id, None)
            dropped.append(version_id)
        return dropped
