"""
协议层：前端能看到的一切形状。

流式部分是 type / data 两个字段的事件：前端不感知 LangGraph 的节点、metadata、run_id
等内部结构，后端重构 graph 时前端无需改动。非流式部分是各接口的回执，服务层把领域实体
映射成它们，表现层只负责包一层 `Result`。

请求体（`ChatRequest` / `ResumeRequest` / `WorkspaceRequest`）留在 api 层——那是入站的
HTTP 校验，前端拿不到它，服务层也不该知道。
"""

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """事件类型。前端 switch 这个字段分发渲染。"""

    THREAD = "thread"
    TEXT_DELTA = "text_delta"
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    INTERRUPT = "interrupt"
    MESSAGE_END = "message_end"
    ERROR = "error"
    DONE = "done"


class ThreadData(BaseModel):
    """会话握手，永远是本次流的第一个事件。"""

    thread_id: str = Field(..., description="会话 ID，客户端保存后在后续请求中回传即可续聊")


class TextDeltaData(BaseModel):
    text: str = Field(..., description="本次增量文本片段，客户端按顺序累加即得完整回复")


class ToolStartData(BaseModel):
    id: str = Field(..., description="工具调用 ID，与 tool_end 配对")
    name: str = Field(..., description="工具名")
    args: dict = Field(default_factory=dict, description="调用参数")


class ToolEndData(BaseModel):
    id: str = Field(..., description="与 tool_start 相同的调用 ID")
    ok: bool = Field(True, description="工具是否执行成功")
    result: str | None = Field(None, description="工具返回值，失败时为 None")
    error: str | None = Field(None, description="失败原因，成功时为 None")


class InterruptData(BaseModel):
    """人工介入请求，前端需渲染确认 UI 并把用户选择回传。"""

    id: str = Field(..., description="介入请求 ID")
    prompt: str = Field(..., description="需要用户确认的内容")
    options: list[str] = Field(default_factory=list, description="候选选项，为空表示自由输入")


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class MessageEndData(BaseModel):
    message_id: str = Field(..., description="本条回复的稳定 ID，便于前端做消息定位")
    finish_reason: str = Field("stop", description="stop / length / tool_calls 等")
    usage: Usage = Field(default_factory=Usage)


class ErrorData(BaseModel):
    code: int = Field(1, description="业务状态码，与 Result 的 code 语义保持一致")
    message: str = Field(..., description="错误信息")


class DoneData(BaseModel):
    """流结束标记，永远是本次流的最后一个事件。"""


# ---- 以下是非流式接口的形状：服务层直接产出，表现层只负责包 Result ----
# 请求体不在这一层：它是入站的 HTTP 校验，见 app/api/chat.py。


class HistoryMessage(BaseModel):
    """历史消息，用于前端恢复既有会话。"""

    role: str = Field(..., description="user 或 assistant")
    content: str
    id: str | None = None


class WorkspaceInfo(BaseModel):
    """工作区的协议形态。默认工作区（应用所在目录）也是这个样子，没有特殊形态。"""

    path: str = Field(..., description="规范路径")
    name: str = Field("", description="展示名，默认取目录名")


class ThreadSummary(BaseModel):
    """会话列表项。由服务端从 checkpointer 推导，前端不自行维护会话清单。"""

    thread_id: str
    title: str = Field(..., description="取首条用户消息，为空时退化为 thread_id 前缀")
    updated_at: str = Field("", description="最近一次检查点时间戳")
    pending: bool = Field(False, description="是否有待人工确认的操作")
    workspace: str = Field("", description="该会话绑定的工作区路径；为空表示用应用所在目录")
    workspace_name: str = Field(
        "", description="工作区展示名（默认目录名），供侧边栏分组标题用"
    )


class WorkspaceResponse(BaseModel):
    """绑定工作区的回执。"""

    thread_id: str
    workspace: str = Field(..., description="绑定后的规范路径")


class BrowseEntry(BaseModel):
    """目录浏览里的一行。"""

    name: str
    path: str


class BrowseResponse(BaseModel):
    """目录浏览的结果。path 为空表示还没进入任何目录，dirs 只给起点。"""

    path: str
    parent: str | None = Field(..., description="上一级目录；已经是根时为 None")
    dirs: list[BrowseEntry]


class HistoryResponse(BaseModel):
    """打开旧会话时要恢复的东西：消息 + 还挂着的待确认项。"""

    thread_id: str
    messages: list[HistoryMessage]
    pending: list[InterruptData] = Field(
        default_factory=list,
        description="待确认项。前端据此在历史末尾补渲染确认卡片，否则卡住的会话进去无处可点",
    )


class ThreadListResponse(BaseModel):
    """会话列表。顺带给出默认工作区，前端不必再为"没设过"单开一个分组。"""

    threads: list[ThreadSummary]
    default_workspace: WorkspaceInfo = Field(
        ...,
        description=(
            "没选过工作区时新会话落在哪。就是应用所在目录，"
            "它和用户自己挑的工作区一视同仁"
        ),
    )


class ThreadEvent(BaseModel):
    type: Literal[EventType.THREAD] = EventType.THREAD
    data: ThreadData


class TextDeltaEvent(BaseModel):
    type: Literal[EventType.TEXT_DELTA] = EventType.TEXT_DELTA
    data: TextDeltaData


class ToolStartEvent(BaseModel):
    type: Literal[EventType.TOOL_START] = EventType.TOOL_START
    data: ToolStartData


class ToolEndEvent(BaseModel):
    type: Literal[EventType.TOOL_END] = EventType.TOOL_END
    data: ToolEndData


class InterruptEvent(BaseModel):
    type: Literal[EventType.INTERRUPT] = EventType.INTERRUPT
    data: InterruptData


class MessageEndEvent(BaseModel):
    type: Literal[EventType.MESSAGE_END] = EventType.MESSAGE_END
    data: MessageEndData


class ErrorEvent(BaseModel):
    type: Literal[EventType.ERROR] = EventType.ERROR
    data: ErrorData


class DoneEvent(BaseModel):
    type: Literal[EventType.DONE] = EventType.DONE
    data: DoneData = DoneData()


AgentEvent = Annotated[
    Union[
        ThreadEvent,
        TextDeltaEvent,
        ToolStartEvent,
        ToolEndEvent,
        InterruptEvent,
        MessageEndEvent,
        ErrorEvent,
        DoneEvent,
    ],
    Field(discriminator="type"),
]
"""全部事件的判别联合，可直接用于解析或生成 TypeScript 类型。"""
