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
from app.config import PROJECT_ROOT
from app.dao.thread_index_dao import ThreadIndexDao
from app.dao.workspace_dao import WorkspaceDao
from app.kernel import ConsoleExporter, Context, LoggingExporter
from app.kernel.paths import set_root
from app.service.chat_service import ChatService

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
        protocol,
        toolbox,
        webui,
    )

    # 界面类接缝（ctx.assets）：界面插件把静态资产挂到这张表上，装卸都不动路由表
    AssetRegistry(ctx)

    # 必需：协议（wire 形状与编码）、工具箱（工具服务，loop 的服务依赖）
    await ctx.plugin(protocol.plugin, essential=True)
    await ctx.plugin(toolbox.plugin, essential=True)

    # 可选：装载失败只降级（点名但不带崩内核）。
    # 每个插件的配置**由插件自己声明默认值**（`default_config()` 引用总体配置 .env），
    # 装配脚本只负责把 base 交进去——将来"用户在前端改设置"的落点就在这里：
    # base 与用户覆盖 dict 合并后再交给插件（覆盖层还没做，形状先留好）。
    optional = (compaction, context, approvals, exporters, webui)
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
    from app.config import (
        AGENTS_ANCESTORS,
        AGENTS_MD_ENABLED,
        COMPACT_AT,
        COMPACT_ENABLED,
        COMPACT_KEEP_TOKENS,
        COMPACT_MODEL,
        CONFIG_WARNINGS,
        CONTEXT_WINDOW_SOURCE,
        GRAPH_VERSIONS_KEEP,
        LLM_BASE_URL,
        LLM_CONTEXT_WINDOW,
        LLM_MODEL,
        SKILLS_DIR,
        SKILLS_ENABLED,
        SQLITE_PATH,
        STUB_ENABLED,
        llm_configured,
    )

    for warning in CONFIG_WARNINGS:
        logger.warning("配置回退：%s", warning)

    if STUB_ENABLED:
        logger.warning("AGENT_STUB=1，对话走桩实现：复读消息、不调模型")
        return StubRunner()

    if not llm_configured():
        # 没配 key 只是"不能跑对话"：历史、列表、删除都读检查点，跟模型无关，照样可用
        logger.warning("未配置 LLM_API_KEY：对话会直接返回错误说明，历史与列表照常可读")

    from app.agent.langgraph_runner import LangGraphRunner
    from app.agent.models import build_model, build_summary_model
    from app.harness.runtime import HarnessRuntime
    from app.harness.versions import VersionRegistry

    if llm_configured():
        logger.info("对话走模型：model=%s base_url=%s", LLM_MODEL, LLM_BASE_URL)

    if COMPACT_ENABLED:
        # 窗口值写进日志、连来源一起写：填错了要能一眼看出来（默认 64k 对大窗口模型偏小）
        logger.info(
            "上下文压缩已开启：窗口 %d tokens（来源：%s），超过 %.0f%%（%d）触发，"
            "保留最近 %d；摘要模型 %s",
            LLM_CONTEXT_WINDOW,
            CONTEXT_WINDOW_SOURCE,
            COMPACT_AT * 100,
            int(LLM_CONTEXT_WINDOW * COMPACT_AT),
            COMPACT_KEEP_TOKENS,
            COMPACT_MODEL or f"跟随主模型（{LLM_MODEL}）",
        )
    else:
        logger.info(
            "自动压缩已关闭（COMPACT_ENABLED=0）：压缩插件以 enabled=false 装载，"
            "compact 节点照常存在（手动 /chat/compact 仍可用）"
        )

    # 技能与项目约定：把"从哪儿找"写进日志——不生效时第一个要看的就这几行
    if SKILLS_ENABLED:
        logger.info(
            "技能已开启：工作区库 <工作区>/.my_agent/skills；全局库 %s",
            SKILLS_DIR or "（未配置 SKILLS_DIR，只有工作区技能）",
        )
    else:
        logger.info("技能已关闭（SKILLS_ENABLED=0）")

    if AGENTS_MD_ENABLED:
        logger.info(
            "项目约定注入已开启：全局 ~/.my_agent/AGENTS.md%s",
            "＋祖先目录（到含 .git 的目录为止）＋工作区根"
            if AGENTS_ANCESTORS
            else "＋工作区根（AGENTS_ANCESTORS=0，不向上遍历）",
        )
    else:
        logger.info("项目约定注入已关闭（AGENTS_MD_ENABLED=0）")

    if SQLITE_PATH:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        db_file = Path(SQLITE_PATH)
        db_file.parent.mkdir(parents=True, exist_ok=True)
        checkpointer = await _exit_stack.enter_async_context(
            AsyncSqliteSaver.from_conn_string(str(db_file))
        )
        logger.info("会话状态落盘至 %s", db_file.resolve())
    else:
        from langgraph.checkpoint.memory import InMemorySaver

        logger.warning("SQLITE_PATH 为空，会话状态仅存于内存，重启即失")
        checkpointer = InMemorySaver()

    # 图不再是"构建"出来的：HarnessRuntime 装载 loop 插件（必需）、
    # 合成片段、编译并进版本账本；runner 只是它的门面。
    runtime = HarnessRuntime(
        ctx,
        checkpointer=checkpointer,
        model=build_model(),
        summary_model=build_summary_model(),
        versions=VersionRegistry(keep=GRAPH_VERSIONS_KEEP),
    )
    await runtime.setup()
    await _start_discovery(ctx)
    return LangGraphRunner(runtime, ctx.get("protocol"), _compaction_settings(ctx))


async def _start_discovery(ctx: Context) -> None:
    """两级插件库的发现与信任（§5.1）：全局库装载即信任，工作区库默认关、按文件批准。"""
    from app.config import (
        PLUGINS_DIR,
        PLUGINS_ENABLED,
        PLUGINS_WORKSPACE,
        PLUGIN_WATCH,
    )
    from app.plugins.discover import PluginDiscovery

    if not PLUGINS_ENABLED:
        logger.info("插件发现已关闭（PLUGINS_ENABLED=0）：只有内置插件参与装配")
        return

    discovery = PluginDiscovery(
        ctx,
        global_dir=PLUGINS_DIR,
        workspace=PLUGINS_WORKSPACE or None,
        watch=PLUGIN_WATCH,
    )
    report = await discovery.start()
    logger.info(
        "插件发现：全局库 %s；工作区库 %s（%s）；热重载 %s——%s",
        PLUGINS_DIR or "（未配置）",
        PLUGINS_WORKSPACE or "未启用（默认关）",
        f"授权清单 {PLUGINS_WORKSPACE}/.my_agent/plugins.json" if PLUGINS_WORKSPACE else "不扫",
        "开（内容指纹轮询，回合边界生效）" if PLUGIN_WATCH else "关（只扫启动那一次）",
        report.render(),
    )


async def startup() -> None:
    """
    构建数据访问层与业务层。

    顺序有意义：先把历史工作区迁进工作区表（要读 thread_index 上的旧列），
    再回填索引，最后清理旧版留下的空会话行。
    """
    global _context
    if _context is not None:
        return

    from app.config import SQLITE_PATH

    # 内核锚点钉在项目根：从 PyCharm 之类的入口启动时 cwd 不是项目根
    set_root(PROJECT_ROOT)
    ctx = Context()
    _install_kernel_logging(ctx)

    await _load_plugins(ctx)

    runner = await _build_runner(ctx)
    _provide(ctx, "runner", runner)
    checkpointer = getattr(runner, "checkpointer", None)
    if checkpointer is not None:
        _provide(ctx, "checkpointer", checkpointer)

    index = ThreadIndexDao(SQLITE_PATH)
    await index.open()
    _exit_stack.push_async_callback(index.close)
    _provide(ctx, "index", index)

    workspaces = WorkspaceDao(SQLITE_PATH)
    await workspaces.open()
    _exit_stack.push_async_callback(workspaces.close)
    _provide(ctx, "workspaces", workspaces)

    service = ChatService(runner, index, workspaces, ctx.get("protocol"))
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
