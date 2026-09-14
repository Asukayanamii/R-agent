"""
领域实体：层与层之间传递的东西（DAO → 业务层 → agent），不对外。

判据只有一条——**要不要跨 HTTP 边界**：

- 只在内部流转、不直接发给前端 → 放这里。dataclass，没有校验与序列化负担
- 前端能看见的形状（SSE 帧、接口回执）→ 放 `app/event/events.py`。pydantic，供 OpenAPI

所以业务层要把实体**映射**成协议形状（ThreadRecord + WorkspaceRecord → ThreadSummary），
而不是把实体直接当响应返回。本项目没有 ORM——SQL 是手写的，DAO 读出来的就是这里的数据类；
真接了 ORM 也一样：ORM 模型是存储的形状，这里是层间交换的形状，两者不合并。
"""

from dataclasses import dataclass


@dataclass(slots=True)
class ThreadRecord:
    """一个会话在索引里的快照。归属不在这里——它存在工作区表那侧。"""

    thread_id: str
    title: str = ""
    updated_at: str = ""
    pending: bool = False


@dataclass(slots=True)
class WorkspaceRecord:
    """一个工作区。path 是身份，path_key 只是判重用的归一化形式。"""

    path: str = ""
    path_key: str = ""
    name: str = ""
    created_at: str = ""
