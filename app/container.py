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


async def _build_runner() -> AgentRunner:
    from app.config import (
        AGENTS_ANCESTORS,
        AGENTS_MD_ENABLED,
        COMPACT_AT,
        COMPACT_ENABLED,
        COMPACT_KEEP_TOKENS,
        COMPACT_MODEL,
        CONFIG_WARNINGS,
        CONTEXT_WINDOW_SOURCE,
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
        logger.info("上下文压缩已关闭（COMPACT_ENABLED=0）")

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

    return LangGraphRunner(checkpointer)


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

    runner = await _build_runner()
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

    service = ChatService(runner, index, workspaces)
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
