"""builtin/store：存储提供方——服务 ``checkpointer`` / ``index`` / ``workspaces`` / ``graph_versions``
（provides ``store:v1``）。

**可替换、不可停用**：没有存储就没有会话。`sqlite_path` 留空 → 内存 checkpointer（重启即失）
+ WARNING（与改造前的分支一致）；索引表与审计表永远落同一个库（一个文件、一次备份）。

**配置就是它的旋钮**（`Config.sqlite_path`，默认值引用 `.env` 的 `SQLITE_PATH`，§5.6）。
连接走 `ctx.cleanup` 登记：插件卸载（或热重载）时自动关掉，不留句柄——这也是
"换存储 = 换一个提供同名服务的插件"的落点。

迁移（把历史工作区迁进工作区表、回填索引……）**不在这里**：那是业务规则，留在
`app/web/service/chat_service.py`；这里只管"连接 + 表 + 读写"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.kernel import Schema

from .graph_version_dao import GraphVersionDao
from .graph_version_service import GraphVersionService
from .thread_index_dao import ThreadIndexDao
from .workspace_dao import WorkspaceDao

PLUGIN_ID = 'builtin/store'
STORE_CONTRACT = 'store:v1'
"""对外契约名：换一份存储实现 = 换一个提供同名契约的插件。"""


class Config(Schema):
    sqlite_path = Schema.string().default('').description('sqlite 文件路径；留空 = 会话只在内存里')


def default_config() -> dict:
    """插件默认配置：引用总体配置（`.env` 的 `SQLITE_PATH`）。"""
    from app.common import config

    return {'sqlite_path': config.SQLITE_PATH or ''}


async def _open_checkpointer(ctx: Any, path: str) -> Any:
    """有路径就落盘（sqlite），没有就内存。返回可直接用的 checkpointer。"""
    if not path:
        from langgraph.checkpoint.memory import InMemorySaver

        ctx.logger.warn('SQLITE_PATH 为空，会话状态仅存于内存，重启即失')
        return InMemorySaver()

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db_file = Path(path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    manager = AsyncSqliteSaver.from_conn_string(str(db_file))
    checkpointer = await manager.__aenter__()
    ctx.cleanup(lambda: manager.__aexit__(None, None, None), 'store.checkpointer')
    ctx.logger.info('会话状态落盘至 %s', db_file.resolve())
    return checkpointer


async def apply(ctx: Any, config: Any = None) -> None:
    """建连接、开表、提供四个服务。异步 apply：DAO 的 `open()` 要 await。"""
    options = dict(config or {})
    path = str(options.get('sqlite_path') or '')

    checkpointer = await _open_checkpointer(ctx, path)

    index = ThreadIndexDao(path)
    await index.open()
    ctx.cleanup(index.close, 'store.index')

    workspaces = WorkspaceDao(path)
    await workspaces.open()
    ctx.cleanup(workspaces.close, 'store.workspaces')

    audit_dao = GraphVersionDao(path)
    await audit_dao.open()
    ctx.cleanup(audit_dao.close, 'store.audit')

    ctx.provide('checkpointer', checkpointer)
    ctx.provide('index', index)
    ctx.provide('workspaces', workspaces)
    ctx.provide('graph_versions', GraphVersionService(audit_dao))
    ctx.logger.info('存储已就绪：%s（索引与审计同库）', path or '内存')


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'provides': STORE_CONTRACT,
    'Config': Config,
}
