"""HarnessRuntime：LangGraphRunner 的唯一入口。

装配三件事：

1. 提供依赖服务（阶段 B 给的是裸模型对象；阶段 D 换成 ``builtin/model`` 插件）；
2. 装载内置插件（``loop`` 是必需插件——校验不过 = 启动失败，不带着半死的内核跑）；
3. 收集片段 → 合成 → 进版本账本（对外只暴露"当前图 + 租约"）。

结构变化（贡献/撤销片段）由 ``maybe_recompose()`` 生成新的 pending 版本，
**只在回合边界生效**——进行中的回合始终跑它开始时的那版图（版本账本负责）。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.kernel import Context

from app.harness.composer import GraphVersion, StructureService, compose
from app.harness.versions import VersionRegistry

__all__ = ['HarnessRuntime']

logger = logging.getLogger(__name__)


class HarnessRuntime:
    """图的合成与版本：LangGraphRunner 从这里借图。"""

    def __init__(
        self,
        ctx: Context,
        *,
        checkpointer: BaseCheckpointSaver,
        model: BaseChatModel,
        summary_model: BaseChatModel,
        versions: VersionRegistry | None = None,
    ) -> None:
        self.ctx = ctx
        self.checkpointer = checkpointer
        self.model = model
        self.summary_model = summary_model
        self.versions = versions or VersionRegistry()
        self.structure = ctx.get('structure') or StructureService(ctx)
        self._next_version_id = 1
        self._composed_revision = -1
        self._loop_fiber: Any = None

    # ---------------------------------------------------------------- 装配
    async def setup(self) -> None:
        """提供依赖 → 装载必需插件 → 校验 → 首次合成。"""
        if self.ctx.get('model') is None:
            self.ctx.provide('model', self.model)  # 阶段 D 由 builtin/model 插件接管
        if self.ctx.get('summary_model') is None:
            self.ctx.provide('summary_model', self.summary_model)

        from app.plugins.builtin import loop

        self._loop_fiber = self.ctx.plugin(loop.plugin, essential=True)
        await self._loop_fiber
        self.ctx.registry.ensure_essentials()
        await self.recompose()

    # ---------------------------------------------------------------- 合成
    def _resolve_interface(self, name: str) -> Any:
        """抽象节点的实现从服务解析（``interface`` 就是服务名）。"""
        return self.ctx.get(name)

    async def recompose(self) -> GraphVersion:
        """按当前片段合成一个新版本，交给版本账本。

        已有 current（说明有在跑的回合）时新版本只是 pending，等下一次 ``lease()`` 生效；
        首次装配没有在跑的东西，直接生效。
        """
        version = compose(
            self.structure.collect(),
            version_id=self._next_version_id,
            checkpointer=self.checkpointer,
            resolve_interface=self._resolve_interface,
        )
        self._next_version_id += 1
        self._composed_revision = self.structure.revision
        self.versions.stage(version)
        if self.versions.current is None:
            self.versions.apply_pending()
        logger.info(
            "图已合成 v%d：哈希 %s，节点 %d 个，锚点 %s",
            version.id,
            version.structure_hash[:12],
            len(version.node_names),
            "、".join(version.anchors),
        )
        return version

    async def maybe_recompose(self) -> bool:
        """片段集合没变就不重合成；变了才产生新版本。返回是否合成了新版本。"""
        if (
            self.versions.current is not None
            and self._composed_revision == self.structure.revision
        ):
            return False
        await self.recompose()
        return True

    # ---------------------------------------------------------------- 租约
    @asynccontextmanager
    async def lease(self, thread_id: str) -> AsyncIterator[GraphVersion]:
        """回合边界：先应用 pending，再把这一回合钉在当时的版本上。"""
        async with self.versions.lease(thread_id) as version:
            yield version
