"""
组装根：把三层拼起来，并管理它们的生命周期。

"有 key 走 LangGraph、没 key 直接报错、显式开了开关才用桩"以及连接何时开关，都是应用装配
的事情，不属于表现层、业务层或数据访问层中的任何一层，因此单独放在这里。
"""

import logging
from contextlib import AsyncExitStack
from pathlib import Path

from app.agent.runner import AgentRunner, StubRunner
from app.dao.thread_index_dao import ThreadIndexDao
from app.dao.workspace_dao import WorkspaceDao
from app.service.chat_service import ChatService

logger = logging.getLogger(__name__)

_exit_stack = AsyncExitStack()
_service: ChatService | None = None


async def _build_runner() -> AgentRunner:
    from app.config import (
        LLM_BASE_URL,
        LLM_MODEL,
        SQLITE_PATH,
        STUB_ENABLED,
        llm_configured,
    )

    if STUB_ENABLED:
        logger.warning("AGENT_STUB=1，对话走桩实现：复读消息、不调模型")
        return StubRunner()

    if not llm_configured():
        # 没配 key 只是"不能跑对话"：历史、列表、删除都读检查点，跟模型无关，照样可用
        logger.warning("未配置 LLM_API_KEY：对话会直接返回错误说明，历史与列表照常可读")

    from app.agent.langgraph_runner import LangGraphRunner

    if llm_configured():
        logger.info("对话走模型：model=%s base_url=%s", LLM_MODEL, LLM_BASE_URL)

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
    global _service
    if _service is not None:
        return

    from app.config import SQLITE_PATH

    runner = await _build_runner()

    index = ThreadIndexDao(SQLITE_PATH)
    await index.open()
    _exit_stack.push_async_callback(index.close)

    workspaces = WorkspaceDao(SQLITE_PATH)
    await workspaces.open()
    _exit_stack.push_async_callback(workspaces.close)

    service = ChatService(runner, index, workspaces)

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

    _service = service


async def shutdown() -> None:
    global _service
    await _exit_stack.aclose()
    _service = None
    logger.debug("服务已关闭，连接已释放")


def get_service() -> ChatService:
    if _service is None:
        raise RuntimeError("服务尚未初始化，请确认应用已走 lifespan 启动")
    return _service
