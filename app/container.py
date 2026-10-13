"""
组装根：把三层拼起来，并管理它们的生命周期。

"有 key 走 LangGraph、没 key 直接报错、显式开了开关才用桩"以及连接何时开关，都是应用装配
的事情，不属于表现层、业务层或数据访问层中的任何一层，因此单独放在这里。

装配走内核的 ``Context``：每个组件按"模块 → 服务名"注册（``runner`` / ``checkpointer`` /
``index`` / ``workspaces`` / ``chat``），对外接口不变——上层仍然只认 ``get_service()``
与 ``AgentRunner``。阶段 B 起，这些直连会逐步换成 ``ctx.plugin(...)`` 的内置插件；
内核的路径锚点（``paths.set_root``）也在这里钉到项目根。
"""

import logging
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from app.agent.runner import AgentRunner, StubRunner
from app.common.config import PROJECT_ROOT
from app.kernel import ConsoleExporter, Context, LoggingExporter
from app.kernel.paths import set_root
from app.web.service.chat_service import ChatService

logger = logging.getLogger(__name__)

KERNEL_LOGGER = "app.kernel"
"""内核日志的 logger 名：挂在 app.* 之下，级别与格式跟着 LOG_LEVEL 与根配置走。"""

_exit_stack = AsyncExitStack()
_context: Context | None = None
_service_handles: list = []
_service_names: list[str] = []


def _install_kernel_logging(ctx: Context) -> None:
    """把内核自带的控制台出口换成标准 logging。

    内核默认往 stderr 打带 ANSI 颜色的行；内联进本应用后要统一到 app.* 的日志配置：
    级别由 LOG_LEVEL 决定（内核那层不设阈值），名字落在 ``app.kernel`` 下。
    """
    for exporter in list(ctx.logger.exporters):
        if isinstance(exporter, ConsoleExporter):
            ctx.logger.exporters.delete(exporter)
    ctx.logger.exporter(LoggingExporter(KERNEL_LOGGER))


def _provide(ctx: Context, name: str, value: object) -> None:
    """注册一个服务并留住撤销句柄：有装上就要有卸下（与插件的 effect 语义一致）。"""
    _service_handles.append(ctx.provide(name, value))
    _service_names.append(name)


async def _load_plugins(ctx: Context) -> None:
    """装配内置插件：必需项缺了启动失败；可选项失败只降级（功能退化，逐项点名）。

    顺序有意义：协议先上（`webui` 依赖 `protocol:v1`）；工具箱/压缩/上下文都在 `loop`
    之前——`loop` 在装载时就按服务组装骨架（可选服务缺了就退化）。
    """
    from app.harness.assets import AssetRegistry
    from app.plugins.builtin import (
        approvals,
        compaction,
        context,
        exporters,
        graph_tools,
        model,
        protocol,
        retry,
        sandbox,
        skillbook,
        store,
        toolbox,
        webui,
    )

    # 界面类接缝（ctx.assets）：界面插件把静态资产挂到这张表上，装卸都不动路由表
    AssetRegistry(ctx)

    # 必需：存储（checkpointer + 索引 + 审计，先上，别的都建在它上面）、模型与重试
    # （loop 的模型调用链靠它们；没配 key 时模型插件给占位实现）、协议（wire 形状与编码）、
    # 工具箱（工具服务，loop 的服务依赖）。
    await ctx.plugin(store.plugin, store.default_config(), essential=True)
    await ctx.plugin(model.plugin, model.default_config(), essential=True)
    await ctx.plugin(retry.plugin, retry.default_config(), essential=True)
    await ctx.plugin(protocol.plugin, essential=True)
    await ctx.plugin(toolbox.plugin, essential=True)

    # 可选：装载失败只降级（点名但不带崩内核）。
    # 每个插件的配置**由插件自己声明默认值**（`default_config()` 引用总体配置 .env），
    # 装配脚本只负责把 base 交进去——将来"用户在前端改设置"的落点就在这里：
    # base 与用户覆盖 dict 合并后再交给插件（覆盖层还没做，形状先留好）。
    optional = (compaction, context, approvals, exporters, sandbox, skillbook, graph_tools, webui)
    for module in optional:
        name = module.PLUGIN_ID
        base = module.default_config() if hasattr(module, 'default_config') else None
        try:
            await ctx.plugin(module.plugin, base)
        except Exception as error:  # noqa: BLE001 - 可选插件的任何装载失败都只降级
            logger.warning('可选插件 %s 未装载：%s', name, error)


def _compaction_settings(ctx: Context):
    """压缩设置：优先取插件（用户可能覆盖过它的配置），插件不在就引用总体配置。"""
    from app.agent.compaction.policy import CompactionSettings

    service = ctx.get('compaction')
    return service.settings if service is not None else CompactionSettings.from_app_config()


async def _build_runner(ctx: Context) -> AgentRunner:
    from app.common.config import CONFIG_WARNINGS, GRAPH_VERSIONS_KEEP, STUB_ENABLED

    for warning in CONFIG_WARNINGS:
        logger.warning("配置回退：%s", warning)

    if STUB_ENABLED:
        logger.warning("AGENT_STUB=1，对话走桩实现：复读消息、不调模型")
        return StubRunner()

    from app.agent.langgraph_runner import LangGraphRunner
    from app.harness.runtime import HarnessRuntime
    from app.harness.versions import VersionRegistry
    from app.plugins.builtin import loop

    # 模型 / 存储 / 压缩 / 技能与约定各自的自述都归它们自己的插件（builtin/*）——
    # 装配根只报"装配本身"的事，不然同一件事两处写、改一处忘一处。
    # 图不再是"构建"出来的：HarnessRuntime 装载**骨架插件**（由这里交进去）、
    # 合成片段、编译并进版本账本；runner 只是它的门面。
    runtime = HarnessRuntime(
        ctx,
        checkpointer=ctx.get('checkpointer'),
        base=loop.plugin,
        versions=VersionRegistry(keep=GRAPH_VERSIONS_KEEP),
        on_version=getattr(ctx.get('graph_versions'), 'record', None),
    )
    await runtime.setup()
    return LangGraphRunner(runtime, ctx.get("protocol"), _compaction_settings(ctx))


async def _build_libraries(ctx: Context):
    """两级插件库（全局 + 按会话工作区）：返回管理器；没启用返回 None。"""
    from app.common.config import PLUGINS_DIR, PLUGINS_ENABLED, PLUGINS_WORKSPACE, PLUGIN_WATCH
    from app.plugins.discover import PluginLibraries

    if not PLUGINS_ENABLED:
        logger.info("插件发现已关闭（PLUGINS_ENABLED=0）：只有内置插件参与装配")
        return None

    libraries = PluginLibraries(
        ctx,
        global_dir=PLUGINS_DIR,
        workspace_enabled=PLUGINS_WORKSPACE,
        watch=PLUGIN_WATCH,
    )
    await libraries.start()
    logger.info(
        "插件库：全局目录 %s；工作区库 %s；热重载 %s——%s",
        PLUGINS_DIR or "（未配置）",
        "开（按会话工作区分域，各自一套图版本）" if PLUGINS_WORKSPACE else "关（PLUGINS_WORKSPACE=0）",
        "开（内容指纹轮询，回合边界生效）" if PLUGIN_WATCH else "关（只扫启动那一次）",
        libraries.loaded_summary(),
    )
    return libraries


async def startup() -> None:
    """
    构建数据访问层与业务层。

    顺序有意义：先把历史工作区迁进工作区表（要读 thread_index 上的旧列），
    再回填索引，最后清理旧版留下的空会话行。
    """
    global _context
    if _context is not None:
        return

    # 存储（checkpointer / 索引 / 审计）归 builtin/store：这里不再自己建连接与 DAO
    # 内核锚点钉在项目根：从 PyCharm 之类的入口启动时 cwd 不是项目根
    set_root(PROJECT_ROOT)
    ctx = Context()
    _install_kernel_logging(ctx)

    # 插件自己把服务挂上 ctx：存储（checkpointer/索引/审计）、模型与重试、协议、工具箱……
    await _load_plugins(ctx)

    runner = await _build_runner(ctx)
    _provide(ctx, "runner", runner)

    # 只读的图视图（服务 `graph`）：报告 / 片段清单 / 审计——`inspect_graph` 工具与排查都吃它
    runtime = getattr(runner, "runtime", None)
    if runtime is not None:
        from app.harness.inspect import GraphInfo

        _provide(ctx, "graph", GraphInfo(runtime))

    # 插件库在 runner **之后**：骨架（loop）要先在架子上——片段是照着锚点表校验的。
    # `on_scope` 装到 runtime 上（stub 模式没有 runtime，整段跳过）。
    libraries = await _build_libraries(ctx)
    if libraries is not None:
        _provide(ctx, "plugins", libraries)
        if runtime is not None:
            runtime.on_scope = libraries.ensure

    service = ChatService(
        runner, ctx.get("index"), ctx.get("workspaces"), ctx.get("protocol")
    )
    _provide(ctx, "chat", service)

    adopted, dropped = await service.adopt_workspaces()
    if adopted:
        logger.info("已把 %d 个会话的工作区迁入工作区表", adopted)
    if dropped:
        logger.info("已删掉 thread_index 上的历史工作区列")

    filled = await service.ensure_index()
    if filled:
        logger.info("已回填 %d 个会话到索引", filled)

    purged = await service.purge_placeholders()
    if purged:
        logger.info("已清理 %d 条只设过工作区、没有对话的空索引", purged)

    rebound = await service.adopt_default_workspace()
    if rebound:
        logger.info("已把 %d 个没有归属的会话绑到应用所在目录", rebound)

    # 装配完成的总校验：必需的插件都必须在 ACTIVE（stub 模式也走这一条）
    ctx.registry.ensure_essentials()

    _context = ctx
    ctx.logger("container").info("内核就绪：服务 %s", "、".join(_service_names))


async def shutdown() -> None:
    global _context
    for handle in reversed(_service_handles):
        handle()
    _service_handles.clear()
    _service_names.clear()
    _context = None
    await _exit_stack.aclose()
    logger.debug("服务已关闭，连接已释放")


def get_service() -> ChatService:
    if _context is None:
        raise RuntimeError("服务尚未初始化，请确认应用已走 lifespan 启动")
    return _context.get("chat")


def get_protocol() -> Any:
    """协议服务（`ctx.protocol`）：wire 形状的构造与 SSE 编码的唯一入口。

    api 层只经它拿握手事件与流编码——换一份协议实现，这一层不用改。
    """
    if _context is None:
        raise RuntimeError("服务尚未初始化，请确认应用已走 lifespan 启动")
    return _context.get("protocol")


def get_assets() -> Any:
    """资产注册表（`ctx.assets`）：`/ui` 那条动态路由按请求查它。"""
    if _context is None:
        raise RuntimeError("服务尚未初始化，请确认应用已走 lifespan 启动")
    return _context.get("assets")


def get_context() -> Context:
    """装配出来的内核上下文（诊断与测试用：查纤维状态、装卸插件都在它上面）。"""
    if _context is None:
        raise RuntimeError("服务尚未初始化，请确认应用已走 lifespan 启动")
    return _context
