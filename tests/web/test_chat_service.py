"""宿主层：`ChatService` 的工作区口径。

"没绑过工作区"不能是一个分类（别处——沙箱、索引——一律按应用所在目录兜底）：
交给 runner 的工作区必须显式落到应用目录，否则插件作用域与工具落点会是两个地方。
"""

import pytest

from app.common.config import PROJECT_ROOT
from app.web.service.chat_service import ChatService

pytestmark = pytest.mark.anyio


class FakeWorkspaces:
    def __init__(self, bound: str | None) -> None:
        self.bound = bound

    async def for_thread(self, thread_id: str) -> str | None:
        return self.bound


def make_service(workspaces) -> ChatService:
    return ChatService(runner=None, index=None, workspaces=workspaces, protocol=None)


async def test_unbound_thread_falls_back_to_app_dir():
    service = make_service(FakeWorkspaces(None))

    assert await service._scope_workspace('t1') == str(PROJECT_ROOT)


async def test_bound_thread_keeps_its_workspace():
    service = make_service(FakeWorkspaces('C:/some/ws'))

    assert await service._scope_workspace('t1') == 'C:/some/ws'
