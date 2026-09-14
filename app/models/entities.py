"""领域实体。数据访问层与业务层都用它传递数据，避免下层反向依赖上层的 DTO。"""

from dataclasses import dataclass


@dataclass(slots=True)
class ThreadRecord:
    """一个会话在索引里的快照。"""

    thread_id: str
    title: str = ""
    updated_at: str = ""
    pending: bool = False
    workspace: str = ""
