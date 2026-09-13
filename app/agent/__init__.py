"""Agent 执行器工厂与生命周期管理。"""

import logging
from contextlib import AsyncExitStack
from pathlib import Path

from app.agent.runner import AgentRunner, StubRunner

logger = logging.getLogger(__name__)

_runner: AgentRunner | None = None
_exit_stack = AsyncExitStack()


async def init_runner() -> None:
    """应用启动时构建执行器。SQLITE_PATH 为空则退化为进程内存。"""
    global _runner
    if _runner is not None:
        return

    from app.config import SQLITE_PATH, llm_configured

    if not llm_configured():
        logger.warning("未配置 LLM_API_KEY，/chat/stream 正在使用桩实现")
        _runner = StubRunner()
        return

    from app.agent.langgraph_runner import LangGraphRunner

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

    _runner = LangGraphRunner(checkpointer)


async def close_runner() -> None:
    await _exit_stack.aclose()


def get_runner() -> AgentRunner:
    if _runner is None:
        raise RuntimeError("执行器尚未初始化，请确认应用已走 lifespan 启动")
    return _runner


def set_runner(runner: AgentRunner | None) -> None:
    """替换执行器，用于测试。"""
    global _runner
    _runner = runner
