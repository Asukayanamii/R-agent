"""HarnessRuntime：LangGraphRunner 的唯一入口。

装配三件事：

1. 提供依赖服务（阶段 B 给的是裸模型对象；阶段 D 换成 ``builtin/model`` 插件）；
2. 装载**骨架插件**（`base=` 由装配根传进来）——校验不过 = 启动失败，不带着半死的内核跑；
3. 收集片段 → 合成 → 进版本账本（对外只暴露"当前图 + 租约"）。

结构变化（贡献/撤销片段）由 ``maybe_recompose()`` 生成新的 pending 版本，
**只在回合边界生效**——进行中的回合始终跑它开始时的那版图（版本账本负责）。
"""

from __future__ import annotations

import logging
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver

from app.kernel import Context, CordisError

from app.harness.circuit import FailureLedger, fail_key
from app.harness.composer import GraphVersion, StructureService, compose, render_mermaid
from app.harness.report import CompositionReport, Mount
from app.harness.versions import DEFAULT_KEEP, VersionRegistry

__all__ = ['HarnessRuntime']

MAX_SCOPES = 8
"""同时保留几个作用域的版本账本（LRU）。插件 fiber 的回收在插件库那侧，同一数量级。"""

logger = logging.getLogger(__name__)


class HarnessRuntime:
    """图的合成与版本：LangGraphRunner 从这里借图。"""

    def __init__(
        self,
        ctx: Context,
        *,
        checkpointer: BaseCheckpointSaver,
        model: BaseChatModel | None = None,
        summary_model: BaseChatModel | None = None,
        base: Any = None,
        versions: VersionRegistry | None = None,
        on_version: Any = None,
        on_scope: Any = None,
    ) -> None:
        self.ctx = ctx
        self.checkpointer = checkpointer
        self.model = model
        self.summary_model = summary_model
        self.base = base
        """骨架插件（`role="base"` 的那个）。由**装配根**交进来——harness 不认识具体插件，
        否则"装配层"就反过来依赖了它要装配的东西（分层守卫：`tests/test_layout.py`）。"""
        self.on_version = on_version
        """每次合成成功后的回调（`(version, report, reason)`，可同步可异步）：图版本审计的落点。
        装配根接到 `GraphVersionService.record`；回调抛错只记 WARNING——**审计不许带走装配**。"""
        self.on_scope = on_scope
        """作用域准备回调（`(workspace) -> 规范化作用域`，可同步可异步）：租约开始时调用。
        装配根接到插件库的 `ensure()`——harness 照旧不认识插件，只知道"这个作用域来了，
        先让装配根把它那套库备好"。返回空串 = 该工作区没有自己的插件，共用全局那张图。"""
        self._keep = versions.keep if versions is not None else DEFAULT_KEEP
        self._registries: dict[str, VersionRegistry] = {
            '': versions or VersionRegistry(keep=self._keep)
        }
        self.structure = ctx.get('structure') or StructureService(ctx)
        self.failures = FailureLedger()
        self._next_version_id = 1
        self._composed_revision: dict[str, int] = {}
        self._composed_circuit: dict[str, int] = {}
        self._loop_fiber: Any = None

    @property
    def versions(self) -> VersionRegistry:
        """全局作用域（`''`）的版本账本——内置插件那套图，也是绝大多数调用方要的那个。"""
        return self._registries['']

    @property
    def scopes(self) -> tuple[str, ...]:
        """已在册的作用域（诊断用；`''` 恒在）。"""
        return tuple(self._registries)

    def registry(self, scope: str = '') -> VersionRegistry:
        """作用域的版本账本（懒建 + LRU）：每个工作区一套 pending/current 与历史。

        作用域数超过 `MAX_SCOPES` 时丢最久没用的那个账本（插件 fiber 由插件库那侧自己回收）；
        下一次用到它会按当时的结构重新合成一个新版本。
        """
        found = self._registries.get(scope)
        if found is not None:
            return found
        if len(self._registries) >= MAX_SCOPES:
            oldest = next((key for key in self._registries if key not in ('', scope)), None)
            if oldest is not None:
                del self._registries[oldest]
                self._composed_revision.pop(oldest, None)
                self._composed_circuit.pop(oldest, None)
                logger.info('作用域账本已回收（LRU）：%s', oldest)
        self._registries[scope] = VersionRegistry(keep=self._keep)
        return self._registries[scope]

    # ---------------------------------------------------------------- 装配
    async def setup(self) -> None:
        """提供依赖 → 装载骨架插件 → 校验 → 首次合成。"""
        if self.ctx.get('model') is None:
            if self.model is None:
                raise CordisError('没有可用的模型：builtin/model 插件没装，装配根也没给兜底模型')
            self.ctx.provide('model', self.model)  # 兜底路径（装配根自带模型时）
        if self.ctx.get('summary_model') is None:
            if self.summary_model is None:
                raise CordisError(
                    '没有可用的摘要模型：builtin/model 插件没装，装配根也没给兜底模型'
                )
            self.ctx.provide('summary_model', self.summary_model)

        if self.base is None:
            raise CordisError(
                '没有骨架插件：装配根必须把 role="base" 的插件经 `base=` 交进来'
                '（harness 不认识任何具体插件）'
            )
        self._loop_fiber = self.ctx.plugin(self.base, essential=True)
        await self._loop_fiber
        self.ctx.registry.ensure_essentials()
        await self.recompose(reason='启动')

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

    def mermaid_of(self, version: GraphVersion | None = None, scope: str = '') -> str:
        """某个版本图的 mermaid 源码（`graph_updated` 卡片与诊断用）。"""
        version = version or self.registry(scope).current
        if version is None:
            return ''
        return render_mermaid(version.graph)

    async def recompose(self, reason: str = '结构变化', scope: str = '') -> GraphVersion:
        """按当前片段合成一个新版本，交给该作用域的版本账本。

        已被熔断（连续失败达到阈值）的片段不参与合成——"自动摘出"的落点就在这里。

        已有 current（说明有在跑的回合）时新版本只是 pending，等下一次 ``lease()`` 生效；
        首次装配没有在跑的东西，直接生效。

        ``reason`` 进审计（启动 / 结构变化 / 熔断）：事后回放要能看出"为什么换了这版图"。
        """
        collected = self.structure.collect(scope)
        active = [
            item
            for item in collected
            if not self.failures.is_disabled(fail_key(item.id, item.scope))
        ]
        version = compose(
            active,
            version_id=self._next_version_id,
            checkpointer=self.checkpointer,
            resolve_interface=self._resolve_interface,
            failures=self.failures,
            capabilities=self.capabilities,
        )
        self._next_version_id += 1
        self._composed_revision[scope] = self.structure.revision(scope)
        self._composed_circuit[scope] = self.failures.revision
        registry = self.registry(scope)
        registry.stage(version)
        if registry.current is None:
            registry.apply_pending()
        logger.info("图已合成 v%d%s：哈希 %s，节点 %d 个，锚点 %s",
            version.id, f'（作用域 {scope}）' if scope else '',
            version.structure_hash[:12], len(version.node_names), "、".join(version.anchors))
        report = self.build_report(version, scope)
        logger.info("合成报告：\n%s", report.render())
        await self._notify(version, report, reason)
        return version

    async def _notify(self, version: GraphVersion, report: CompositionReport, reason: str) -> None:
        """图版本审计（旁路）：回调可同步可异步，**抛错只记 WARNING**——审计坏掉不该带走装配。"""
        if self.on_version is None:
            return
        try:
            result = self.on_version(version, report, reason)
            if inspect.isawaitable(result):
                await result
        except Exception as error:  # noqa: BLE001 - 审计是旁路
            logger.warning('图版本审计失败（不影响装配）：%s', error)

    # ---------------------------------------------------------------- 报告
    def build_report(
        self, version: GraphVersion | None = None, scope: str = ''
    ) -> CompositionReport:
        """按**该作用域**的片段集合与账本生成合成报告（日志 / `graph_updated` / inspect 共用）。"""
        version = version or self.registry(scope).current
        collected = self.structure.collect(scope)
        active_ids = {
            item.id
            for item in collected
            if not self.failures.is_disabled(fail_key(item.id, item.scope))
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
            scope=scope,
            # 片段集合变了但还没合成/还没到回合边界：报告不能拿"现状"冒充"正在跑的图"
            pending_change=self._composed_revision.get(scope, -1) != self.structure.revision(scope),
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

    async def maybe_recompose(self, scope: str = '') -> bool:
        """该作用域的片段集合或熔断名单没变就不重合成；变了才产生新版本。

        熔断的版本号是**全局**的（一次失败在任何作用域都作数），所以别的作用域熔断会让
        这里多合成一次——正确但略有浪费，换来的是"不用给账本按作用域拆两级计数"。
        """
        if (
            self.registry(scope).current is not None
            and self._composed_revision.get(scope, -1) == self.structure.revision(scope)
            and self._composed_circuit.get(scope, -1) == self.failures.revision
        ):
            return False
        # 没记过（新作用域第一次合成）按"没变"算：默认值取当前熔断版本号，别把首合成误报成熔断
        composed_circuit = self._composed_circuit.get(scope, self.failures.revision)
        await self.recompose(
            reason='熔断' if composed_circuit != self.failures.revision else '结构变化',
            scope=scope,
        )
        return True

    # ---------------------------------------------------------------- 租约
    @asynccontextmanager
    async def lease(self, thread_id: str, scope: str = '') -> AsyncIterator[GraphVersion]:
        """回合边界：先把结构/熔断的变化合成进 pending（应用在下一次租约），再钉版本。

        ``scope`` 是会话工作区（`on_scope` 会把它规范化；没有插件库那套回调时一律按全局走）。
        重合成失败**不改变运行图**——记一条警告，本轮继续用当前版本（§3.1）。
        """
        if scope and self.on_scope is not None:
            try:
                resolved = self.on_scope(scope)
                if inspect.isawaitable(resolved):
                    resolved = await resolved
                scope = resolved or ''
            except Exception as error:  # noqa: BLE001 - 库有问题也不该挡住本轮
                logger.warning('作用域 %s 的插件库准备失败，本轮用已有结构：%s', scope, error)
        elif self.on_scope is None:
            scope = ''  # 没有按作用域装载的插件库：所有工作区共用一张图
        try:
            await self.maybe_recompose(scope)
        except Exception as error:  # noqa: BLE001 - 装配失败不许带走本回合
            logger.warning("重合成失败，本轮继续用当前版本：%s", error)
        async with self.registry(scope).lease(thread_id) as version:
            yield version
