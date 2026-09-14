"""
流式事件协议。

前端只依赖本文件定义的 type / data 两个字段，不感知 LangGraph 的节点、
metadata、run_id 等内部结构。后端重构 graph 时前端无需改动。
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


class HistoryMessage(BaseModel):
    """历史消息，用于前端恢复既有会话。"""

    role: str = Field(..., description="user 或 assistant")
    content: str
    id: str | None = None


class ThreadSummary(BaseModel):
    """会话列表项。由服务端从 checkpointer 推导，前端不自行维护会话清单。"""

    thread_id: str
    title: str = Field(..., description="取首条用户消息，为空时退化为 thread_id 前缀")
    updated_at: str = Field("", description="最近一次检查点时间戳")
    pending: bool = Field(False, description="是否有待人工确认的操作")
    workspace: str = Field("", description="该会话的工作区；为空表示用应用所在目录")


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
