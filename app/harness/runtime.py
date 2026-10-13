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

from app.harness.circuit import FailureLedger
from app.harness.composer import GraphVersion, StructureService, compose, render_mermaid
from app.harness.report import CompositionReport, Mount
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
        self.failures = FailureLedger()
        self._next_version_id = 1
        self._composed_revision = -1
        self._composed_circuit = 0
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
        """抽象节点的实现从服务解析（``interface`` 就是服务名）。

        服务可以给出 ``.node`` 作为它的节点实现（如服务 ``tools``）；没有就要求服务自身可调用。
        """
        service = self.ctx.get(name)
        if service is None:
            return None
        return getattr(service, 'node', service)

    # ---------------------------------------------------------------- 能力清单
    def capabilities(self) -> dict[str, Any]:
        """能力清单（进结构戳；`toolset_updated` 也用它比对）——与结构版本**分开记**（§4.1）。"""
        toolbox = self.ctx.get('tools')
        if toolbox is None:
            return {}
        return {'tools': toolbox.digest, 'count': len(toolbox.names())}

    def tool_names(self) -> list[str]:
        toolbox = self.ctx.get('tools')
        return toolbox.names() if toolbox is not None else []

    def mermaid_of(self, version: GraphVersion | None = None) -> str:
        """某个版本图的 mermaid 源码（`graph_updated` 卡片与诊断用）。"""
        version = version or self.versions.current
        if version is None:
            return ''
        return render_mermaid(version.graph)

    async def recompose(self) -> GraphVersion:
        """按当前片段合成一个新版本，交给版本账本。

        已被熔断（连续失败达到阈值）的片段不参与合成——"自动摘出"的落点就在这里。

        已有 current（说明有在跑的回合）时新版本只是 pending，等下一次 ``lease()`` 生效；
        首次装配没有在跑的东西，直接生效。
        """
        collected = self.structure.collect()
        active = [item for item in collected if not self.failures.is_disabled(item.id)]
        version = compose(
            active,
            version_id=self._next_version_id,
            checkpointer=self.checkpointer,
            resolve_interface=self._resolve_interface,
            failures=self.failures,
            capabilities=self.capabilities,
        )
        self._next_version_id += 1
        self._composed_revision = self.structure.revision
        self._composed_circuit = self.failures.revision
        self.versions.stage(version)
        if self.versions.current is None:
            self.versions.apply_pending()
        logger.info("图已合成 v%d：哈希 %s，节点 %d 个，锚点 %s",
            version.id, version.structure_hash[:12], len(version.node_names), "、".join(version.anchors))
        logger.info("合成报告：\n%s", self.build_report(version).render())
        return version

    # ---------------------------------------------------------------- 报告
    def build_report(self, version: GraphVersion | None = None) -> CompositionReport:
        """按当前片段集合与账本生成合成报告（日志 / `graph_updated` / inspect 共用）。"""
        version = version or self.versions.current
        collected = self.structure.collect()
        active_ids = {
            item.id for item in collected if not self.failures.is_disabled(item.id)
        }
        base = self.structure.base()

        mounts: list[Mount] = []
        if base is not None:
            for anchor in base.anchors:
                chain = tuple(
                    item.id
                    for item in collected
                    if item.anchor == anchor and item.id in active_ids
                )
                mounts.append(Mount(anchor=anchor, chain=chain))

        return CompositionReport(
            version=version.id if version else 0,
            structure_hash=version.structure_hash if version else '',
            base=base.id if base else '（无骨架）',
            protocol=getattr(self.ctx.get('protocol'), 'version', None),
            participants=tuple(sorted(active_ids)),
            not_participating=tuple(self._pending_plugins()),
            disabled=tuple(
                {'id': key, 'reason': value}
                for key, value in sorted(self.failures.disabled.items())
            ),
            mounts=tuple(mounts),
            warnings=tuple(version.warnings) if version else (),
        )

    def _pending_plugins(self) -> list[dict[str, Any]]:
        """未参与合成的插件——Cordis 的 PENDING 是静默的，报告必须点名（§3.1 第 10 条）。"""
        pending: list[dict[str, Any]] = []
        for runtime in self.ctx.registry.values():
            for fiber in runtime.fibers:
                if fiber.state.name != 'PENDING':
                    continue
                missing = fiber.missing_services()
                pending.append({
                    'id': runtime.name or fiber.name,
                    'reason': f'缺少服务 {"、".join(missing)}' if missing else '依赖未就绪',
                })
        return pending

    async def maybe_recompose(self) -> bool:
        """片段集合或熔断名单没变就不重合成；变了才产生新版本。返回是否合成了新版本。"""
        if (
            self.versions.current is not None
            and self._composed_revision == self.structure.revision
            and self._composed_circuit == self.failures.revision
        ):
            return False
        await self.recompose()
        return True

    # ---------------------------------------------------------------- 租约
    @asynccontextmanager
    async def lease(self, thread_id: str) -> AsyncIterator[GraphVersion]:
        """回合边界：先把结构/熔断的变化合成进 pending（应用在下一次租约），再钉版本。

        重合成失败**不改变运行图**——记一条警告，本轮继续用当前版本（§3.1）。
        """
        try:
            await self.maybe_recompose()
        except Exception as error:  # noqa: BLE001 - 装配失败不许带走本回合
            logger.warning("重合成失败，本轮继续用当前版本：%s", error)
        async with self.versions.lease(thread_id) as version:
            yield version
