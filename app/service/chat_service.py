"""
业务层：编排 Agent 执行与会话索引维护。

自己不碰 SQL（dao 的职责），也不碰图与 checkpointer（agent 的职责），
只负责"一轮对话之后要记录什么"这类业务规则。
"""

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
import sys
from uuid import uuid4

from pydantic import BaseModel

from app.agent.runner import AgentRunner
from app.config import PROJECT_ROOT
from app.dao.thread_index_dao import ThreadIndexDao
from app.dao.workspace_dao import WorkspaceDao
from app.exceptions import InvalidInput
from app.event.events import (
    BrowseEntry,
    BrowseResponse,
    ErrorData,
    ErrorEvent,
    HistoryMessage,
    InterruptData,
    InterruptEvent,
    MessageEndData,
    MessageEndEvent,
    ThreadSummary,
)
from app.models.entities import ThreadRecord, WorkspaceRecord

logger = logging.getLogger(__name__)


class ChatService:
    def __init__(
        self, runner: AgentRunner, index: ThreadIndexDao, workspaces: WorkspaceDao
    ) -> None:
        self._runner = runner
        self._index = index
        self._workspaces = workspaces

    async def _stuck_reason(self, thread_id: str) -> str | None:
        """会话是否卡着（用于索引里的 pending 标记）。"""
        if await self._runner.is_interrupted(thread_id):
            return "待确认"
        if await self._runner.has_dangling_tool_calls(thread_id):
            return "有悬空工具调用"
        return None

    async def stream(self, thread_id: str, message: str) -> AsyncIterator[BaseModel]:
        pending = await self._runner.pending_interrupts(thread_id)
        if pending:
            # 用户绕过了确认、直接又发了一条消息。这里不能只回一句"请先调用
            # /chat/resume"——他面对的是界面，没法自己构造请求，只会卡住。
            # 把待确认项重新推回去，前端会再渲染一张卡片，点一下即可。
            for item in pending:
                yield InterruptEvent(
                    data=item.model_copy(
                        update={"prompt": f"上一条消息未处理，请先确认：{item.prompt}"}
                    )
                )
            yield MessageEndEvent(data=MessageEndData(message_id=uuid4().hex[:8]))
            return

        if await self._runner.has_dangling_tool_calls(thread_id):
            yield ErrorEvent(
                data=ErrorData(
                    message="该会话有未完成的工具调用，无法继续，请点击「新对话」开始"
                )
            )
            return

        workspace = await self._workspace(thread_id)
        logger.info(
            "对话开始 thread=%s 消息=%d 字 工作区=%s",
            thread_id,
            len(message),
            workspace or "应用目录",
        )
        async for event in self._runner.stream(
            thread_id=thread_id, message=message, workspace=workspace
        ):
            yield event
        await self._record(thread_id, title=message)

    async def resume(self, thread_id: str, value: str) -> AsyncIterator[BaseModel]:
        workspace = await self._workspace(thread_id)
        logger.info(
            "继续对话 thread=%s 选择=%s 工作区=%s",
            thread_id,
            value,
            workspace or "应用目录",
        )
        async for event in self._runner.resume(
            thread_id=thread_id, value=value, workspace=workspace
        ):
            yield event
        await self._record(thread_id, title="")

    async def _workspace(self, thread_id: str) -> str | None:
        """该会话的工作区。没绑过则返回 None，由沙箱退回应用所在目录。"""
        return await self._workspaces.for_thread(thread_id) or None

    def browse(self, path: str) -> BrowseResponse:
        """
        列出目录，供前端挑选工作区。

        **这一步刻意不受沙箱约束**：沙箱限制的是 agent，不是用户。
        用户本来就能在自己机器上任意选目录，拦它没有意义。

        代价是它成为一个可以列举任意目录的接口。本地单用户部署没问题，
        但要是把 API 暴露出去，这个接口必须先加鉴权或去掉。
        """
        if not path.strip():
            return BrowseResponse(path="", parent=None, dirs=self._start_points())

        target = Path(path.strip()).expanduser()
        try:
            target = target.resolve()
        except OSError as exc:
            raise InvalidInput(f"无法解析路径：{exc}") from exc

        if not target.is_dir():
            raise InvalidInput(f"不是目录：{target.as_posix()}")

        entries = []
        try:
            for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                try:
                    if item.is_dir():
                        entries.append(BrowseEntry(name=item.name, path=item.as_posix()))
                except OSError:
                    continue
        except PermissionError as exc:
            raise InvalidInput(f"没有权限读取：{target.as_posix()}") from exc

        parent = target.parent
        return BrowseResponse(
            path=target.as_posix(),
            parent=None if parent == target else parent.as_posix(),
            dirs=entries,
        )

    @staticmethod
    def _start_points() -> list[BrowseEntry]:
        """没给路径时给出的起点：用户目录 + 各盘符（Windows）。"""
        points = [BrowseEntry(name=f"~ {Path.home().name}", path=Path.home().as_posix())]
        if sys.platform == "win32":
            for letter in "CDEFGH":
                drive = Path(f"{letter}:/")
                if drive.exists():
                    points.append(BrowseEntry(name=drive.as_posix(), path=drive.as_posix()))
        else:
            points.append(BrowseEntry(name="/", path="/"))
        return points

    async def set_workspace(self, thread_id: str, path: str) -> str:
        """
        把会话绑定到工作区——这是会话与工作区建立关系的唯一入口。

        工作区就是沙箱的信任边界，**只能由用户指定**——agent 若能改自己的边界，
        边界就不存在了。所以这个方法只应该被用户触发的接口调用，且只作用于刚诞生的会话：
        既有会话的归属不允许被别的动作顺手改掉（界面上选目录 = 开一条新对话，不是搬走手上这条）。
        """
        target = Path(path.strip() or ".").expanduser()
        if not target.is_absolute():
            target = PROJECT_ROOT / target
        target = target.resolve()

        if not target.exists():
            raise InvalidInput(f"路径不存在：{target.as_posix()}")
        if not target.is_dir():
            raise InvalidInput(f"不是目录：{target.as_posix()}")

        bound = await self._workspaces.bind(
            thread_id, target.as_posix(), datetime.now(timezone.utc).isoformat()
        )
        logger.info("绑定工作区 thread=%s -> %s", thread_id, bound)
        return bound

    async def _record(self, thread_id: str, title: str) -> None:
        """
        一轮对话结束后刷新索引。

        pending 取"是否还卡着"：正常中断时它等同于 is_interrupted，但也能覆盖
        "中断态被新消息破坏、留下悬空 tool_calls"的坏死会话——那种会话同样需要
        用户注意，只是不能再 resume，只能新建。
        """
        pending = await self._stuck_reason(thread_id) is not None
        logger.debug(
            "刷新索引 thread=%s 标题=%s pending=%s",
            thread_id,
            " ".join(title.split())[:60] or "-",
            pending,
        )
        await self._index.upsert(
            ThreadRecord(
                thread_id=thread_id,
                title=title,
                updated_at=datetime.now(timezone.utc).isoformat(),
                pending=pending,
            )
        )

    async def history(self, thread_id: str) -> list[HistoryMessage]:
        return await self._runner.history(thread_id)

    async def pending_interrupts(self, thread_id: str) -> list[InterruptData]:
        """
        该会话挂着的待确认项。

        前端打开历史会话时要拿到它才能渲染确认卡片——否则一个卡住的会话
        虽然侧边栏有红点，用户进去却无处可点。
        """
        return await self._runner.pending_interrupts(thread_id)

    async def threads(self, limit: int = 50) -> list[ThreadSummary]:
        records = await self._index.list_threads(limit)
        bound = await self._workspaces.paths_for(
            [record.thread_id for record in records]
        )
        logger.debug("列出会话 %d 条（其中 %d 条有归属）", len(records), len(bound))
        items = []
        for record in records:
            found = bound.get(record.thread_id)
            items.append(
                ThreadSummary(
                    thread_id=record.thread_id,
                    title=record.title or record.thread_id[:12],
                    updated_at=record.updated_at,
                    pending=record.pending,
                    workspace=found.path if found else "",
                    workspace_name=found.name if found else "",
                )
            )
        return items

    async def delete_thread(self, thread_id: str) -> None:
        """
        删除一个会话。不存在的会话静默通过。

        **顺序不能反**：先删 checkpoints，再删索引行。反过来的话，中间失败会留下
        "索引没了但检查点还在"的会话，下次索引重建又把它枚举回来。

        绑定行最后删：链接离开索引行不可见，中途失败也只会剩一条谁都不会读的孤立记录。
        工作区行本身留着——同工作区的其他会话还在用它。

        沙箱授权**不在这里清**——它按工作区归属，同工作区的其他会话还在用。
        跟着会话删授权，会把别的对话一起连坐。
        """
        logger.info("删除会话 thread=%s", thread_id)
        await self._runner.delete_thread(thread_id)
        await self._index.delete(thread_id)
        await self._workspaces.unbind(thread_id)

    async def ensure_index(self) -> int:
        """
        索引为空而存储里有会话时回填一次，用于首次启用索引或索引被删。

        工作区归属在 thread_workspace 那张表里，不参与这个回填，所以重建会话列表不丢归类。
        """
        if await self._index.count() > 0:
            return 0
        logger.debug("索引为空，从存储回填")
        records = await self._runner.scan_threads()
        for record in records:
            await self._index.upsert(record)
        return len(records)

    async def adopt_workspaces(self) -> tuple[int, bool]:
        """
        把历史工作区字符串收进工作区表，然后删掉那个已迁出的列（启动时跑一次，幂等）。

        老库里同一目录可能因为大小写写法不同留下多条字符串，经归一化后合并成同一行。
        **顺序不能反**：先迁后删，直接删列会把还没读出来的归属一起带走。
        返回 (迁入的会话数, 是否删掉了历史列)。
        """
        legacy = await self._index.rows_with_legacy_workspace()
        now = datetime.now(timezone.utc).isoformat()
        for thread_id, path in legacy:
            await self._workspaces.bind_if_absent(thread_id, path, now)
        dropped = await self._index.drop_legacy_workspace()
        return len(legacy), dropped

    async def default_workspace(self) -> WorkspaceRecord:
        """
        没选过工作区时新会话落在哪：应用所在目录。

        它和用户自己挑的工作区**一视同仁**——同一个实体、同一种分组、同一条绑定路径。
        沙箱在没有绑定时的兜底值也是这里，所以显式绑上它不改变任何权限行为。
        """
        path = PROJECT_ROOT.as_posix()
        found = await self._workspaces.for_path(path)
        if found is not None:
            return found
        return await self._workspaces.ensure(
            path, datetime.now(timezone.utc).isoformat()
        )

    async def adopt_default_workspace(self) -> int:
        """
        把还没有归属的会话统一绑到默认工作区（应用所在目录），启动时跑一次。

        归属是"每个会话都有且只有一个"，这样 UI 侧不必再为"没设工作区"单开一个分组。
        索引重建出来的会话也走这条路；已经绑过的绝不动（bind_many 是 INSERT OR IGNORE）。
        """
        now = datetime.now(timezone.utc).isoformat()
        default = await self._workspaces.ensure(PROJECT_ROOT.as_posix(), now)
        ids = await self._index.thread_ids()
        bound = await self._workspaces.paths_for(ids)
        missing = [thread_id for thread_id in ids if thread_id not in bound]
        await self._workspaces.bind_many(missing, default.path, now)
        return len(missing)

    async def purge_placeholders(self) -> int:
        """
        清理"只设过工作区、从没说过话"的空索引行。

        旧版 set_workspace 会提前物化这种行，在侧边栏表现为一条十六进制标题的空会话。
        存储里仍有状态的会话一律保留——宁可留着空行，也不能删掉有用的索引。
        """
        ghosts = await self._index.placeholder_ids()
        if not ghosts:
            return 0
        logger.debug("占位行候选 %d 条，逐个核对存储里是否还有状态", len(ghosts))
        alive = {record.thread_id for record in await self._runner.scan_threads()}
        victims = [thread_id for thread_id in ghosts if thread_id not in alive]
        if not victims:
            return 0
        # 绑定是迁到工作区表之后才写上的，索引行删掉后它就成了谁都不会读的孤立记录
        await self._workspaces.unbind_many(victims)
        return await self._index.delete_many(victims)
