"""事件词汇表：图的产出形状（与 `fragment.py` 的 `OUTPUT_SEAMS` 同侧，随**内核版本**走）。

流式部分是 type / data 两个字段的事件：前端不感知 LangGraph 的节点、metadata、run_id
等内部结构，后端重构 graph 时前端无需改动。非流式部分是各接口的回执，服务层把领域实体
映射成它们，表现层只负责包一层 `Result`。

**为什么不在协议插件里**：agent / service / api 都要静态构造这些类（节点产出、接口回执），
放在 `plugins/builtin/protocol/` 就等于"这些层都反向依赖一个插件"。词汇表是内核的产出契约
（接缝清单见 `fragment.OUTPUT_SEAMS`），协议插件只负责**映射**（`mapping.py`）与**编码**
（`encoding.py`）；换一份协议实现 = 换 mapping/encoding，事件名是前端契约、不随插件变。

诚实标注：这些类目前**同时**充当内部词汇表与 wire 形状（历史遗留，原本是 `app/event/events.py`）。
真要换一份协议实现时再把"内部词汇表"与"wire 模型"分成两套——那时才有第二个实现来验证边界。

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
    COMPACT = "compact"
    MESSAGE_END = "message_end"
    ERROR = "error"
    DONE = "done"
    # 结构/能力的变化与插件失败：加事件类型属于非破坏变更（前端未知类型静默忽略）
    GRAPH_UPDATED = "graph_updated"
    TOOLSET_UPDATED = "toolset_updated"
    PLUGIN_FAILED = "plugin_failed"


class ThreadData(BaseModel):
    """会话握手，永远是本次流的第一个事件。"""

    thread_id: str = Field(..., description="会话 ID，客户端保存后在后续请求中回传即可续聊")


class TextDeltaData(BaseModel):
    text: str = Field(..., description="本次增量文本片段，客户端按顺序累加即得完整回复")


class ToolStartData(BaseModel):
    id: str = Field(..., description="工具调用 ID，与 tool_end 配对")
    name: str = Field(..., description="工具名")
    args: dict = Field(default_factory=dict, description="调用参数")


class ToolDiffLine(BaseModel):
    """
    差异里的一行。

    行号的键名是 `old` / `new`：界面按"旧文件列 / 新文件列"渲染两列行号，名字对上才不容易接错。
    """

    kind: Literal["ctx", "del", "add", "skip"] = Field(
        ..., description="ctx 上下文 / del 删除 / add 新增 / skip 被折叠的未变化段"
    )
    text: str = Field("", description="行内容（含缩进），skip 行没有内容")
    old: int | None = Field(None, description="旧文件里的行号；新增行为 None")
    new: int | None = Field(None, description="新文件里的行号；删除行为 None")
    count: int = Field(0, description="仅 skip 行：这里折叠了几行未变化内容")


class ToolDiff(BaseModel):
    """
    一次文件改动的结构化差异，供界面按行渲染。

    **模型看不到它**：它走 `ToolMessage` 的 artifact，不进提示词——模型刚做完这次替换，
    让它在上下文里再读一遍改动没有收益，反而会长期占着历史。历史恢复时同样从这个 artifact
    取（检查点里存了），所以重开旧会话仍看得到当时的差异。
    """

    additions: int = Field(0, description="新增行数")
    deletions: int = Field(0, description="删除行数")
    lines: list[ToolDiffLine] = Field(
        default_factory=list, description="按文件顺序排列的差异行"
    )
    truncated: bool = Field(
        False, description="差异过大被截断；或文件太大，干脆没生成差异"
    )
    omitted: int = Field(0, description="被截掉、没能显示的差异行数")


class ToolEndData(BaseModel):
    id: str = Field(..., description="与 tool_start 相同的调用 ID")
    ok: bool = Field(True, description="工具是否执行成功")
    result: str | None = Field(None, description="工具返回值，失败时为 None")
    error: str | None = Field(None, description="失败原因，成功时为 None")
    diff: ToolDiff | None = Field(
        None, description="文件改动差异；目前只有 edit 会带，其他工具为 None"
    )


class InterruptData(BaseModel):
    """人工介入请求，前端需渲染确认 UI 并把用户选择回传。"""

    id: str = Field(..., description="介入请求 ID")
    prompt: str = Field(..., description="需要用户确认的内容")
    options: list[str] = Field(default_factory=list, description="候选选项，为空表示自由输入")


class CompactData(BaseModel):
    """
    一次上下文压缩。

    消息一条没删：这条事件只是告诉前端"从这条分界起，早前内容在发给模型时换成摘要了"。
    """

    tokens_before: int = Field(..., description="压缩前估算的提示词 tokens")
    tokens_after: int = Field(..., description="压缩后估算的 tokens")
    summary: str = Field(..., description="中段的摘要正文，供用户展开查看")
    count: int = Field(..., description="该会话累计压缩次数")


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = Field(
        0,
        description=(
            "输入里命中 provider 前缀缓存的部分。它决定'大窗口 + 晚压缩'这套配置花不花得来："
            "命中时稳定前缀不必每轮重算，长期贴着 0 就说明每轮都在重预填充整个历史。"
            "端点不报这个字段时恒为 0"
        ),
    )


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
# 请求体不在这一层：它是入站的 HTTP 校验，见 app/web/api/chat.py。


class HistoryToolCall(BaseModel):
    """
    历史里的一次工具调用。

    调用与结果分开存（`AIMessage.tool_calls` 与按 `tool_call_id` 配对的 `ToolMessage`），
    所以可能只有调用没有结果——停在待确认上、或者那一轮被中断了，这时 `state` 是 pending。
    """

    id: str = Field(..., description="调用 ID，与实时事件里的 tool_start 一致")
    name: str = Field(..., description="工具名")
    args: dict = Field(default_factory=dict, description="调用参数")
    state: Literal["ok", "failed", "pending"] = Field(
        "ok", description="完成 / 失败 / 未完成（可能停在待确认上）"
    )
    result: str | None = Field(None, description="成功时的返回值")
    error: str | None = Field(None, description="失败或未完成的原因")
    diff: ToolDiff | None = Field(
        None, description="文件改动差异；目前只有 edit 会带，其他工具为 None"
    )


class HistoryMessage(BaseModel):
    """历史消息，用于前端恢复既有会话。"""

    role: str = Field(..., description="user 或 assistant")
    content: str
    id: str | None = None
    tool_calls: list[HistoryToolCall] = Field(
        default_factory=list,
        description="assistant 消息在这轮里调过的工具，按发生顺序；渲染在正文上方",
    )


class CompactionInfo(BaseModel):
    """
    会话历史里的压缩分界。

    分隔线插在 `before` 处：那之前的消息在**发给模型时**已被摘要替代，
    但界面上照旧完整——原文一条都没删。
    """

    before: int = Field(..., description="插在 messages 列表第几条之前（服务端算好的展示位置）")
    summary: str = Field(..., description="摘要正文，供用户展开查看")
    tokens_before: int = 0
    tokens_after: int = 0
    count: int = Field(0, description="该会话累计压缩次数")
    at: str = Field("", description="最近一次压缩发生的时刻（ISO）")


class HistoryView(BaseModel):
    """读一次会话历史的结果：消息 + 压缩分界。"""

    messages: list[HistoryMessage] = Field(default_factory=list)
    compaction: CompactionInfo | None = None


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


class SkillInfo(BaseModel):
    """
    一个可用技能（`/skills` 的列表项）。

    这里只有**索引里的元数据**：正文不在协议里，由模型按需读文件（渐进披露的第一层）。
    `manual_only` 的技能不进系统提示词的索引，只能用 `/skill:<name>` 手动调用。
    """

    name: str
    description: str = Field(..., description="决定模型会不会去读正文，来自 frontmatter")
    scope: str = Field(..., description="来源：项目 / 全局")
    path: str = Field(..., description="SKILL.md 的绝对路径")
    manual_only: bool = Field(
        False, description="disable-model-invocation：不进索引，只能手动调用"
    )


class SkillListResponse(BaseModel):
    """`/skills` 的结果。技能来源随工作区变，所以把用的是哪个工作区一并给出。"""

    workspace: str = Field(..., description="按哪个工作区列的技能")
    skills: list[SkillInfo] = Field(default_factory=list)


class HistoryResponse(BaseModel):
    """打开旧会话时要恢复的东西：消息 + 还挂着的待确认项 + 压缩分界。"""

    thread_id: str
    messages: list[HistoryMessage]
    pending: list[InterruptData] = Field(
        default_factory=list,
        description="待确认项。前端据此在历史末尾补渲染确认卡片，否则卡住的会话进去无处可点",
    )
    compaction: CompactionInfo | None = Field(
        None, description="压缩分界；会话没压缩过就是 None"
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


class GraphUpdatedData(BaseModel):
    """结构已更新：这个会话上一次跑在旧结构上，这一轮换了新版本。"""

    version: int = Field(..., description="新的图版本号（进程内自增）")
    structure_hash: str = Field(..., description="结构哈希，与合成报告对账用")
    base: str = Field(..., description="骨架插件 id")
    mermaid: str = Field("", description="当前图的 mermaid 源码（前端可折叠卡片展示）")


class ToolsetUpdatedData(BaseModel):
    """能力集（工具）已更新：下一次模型调用就会带上新快照。"""

    tools: list[str] = Field(default_factory=list, description="当前工具名清单")


class PluginFailedData(BaseModel):
    """插件节点失败：这一步已跳过、会话继续；连续失败到阈值会停用整条片段。"""

    fragment: str = Field(..., description="片段 id")
    node: str = Field(..., description="节点名")
    error: str = Field(..., description="异常原文")


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


class CompactEvent(BaseModel):
    type: Literal[EventType.COMPACT] = EventType.COMPACT
    data: CompactData


class MessageEndEvent(BaseModel):
    type: Literal[EventType.MESSAGE_END] = EventType.MESSAGE_END
    data: MessageEndData


class ErrorEvent(BaseModel):
    type: Literal[EventType.ERROR] = EventType.ERROR
    data: ErrorData


class DoneEvent(BaseModel):
    type: Literal[EventType.DONE] = EventType.DONE
    data: DoneData = DoneData()


class GraphUpdatedEvent(BaseModel):
    type: Literal[EventType.GRAPH_UPDATED] = EventType.GRAPH_UPDATED
    data: GraphUpdatedData


class ToolsetUpdatedEvent(BaseModel):
    type: Literal[EventType.TOOLSET_UPDATED] = EventType.TOOLSET_UPDATED
    data: ToolsetUpdatedData


class PluginFailedEvent(BaseModel):
    type: Literal[EventType.PLUGIN_FAILED] = EventType.PLUGIN_FAILED
    data: PluginFailedData


AgentEvent = Annotated[
    Union[
        ThreadEvent,
        TextDeltaEvent,
        ToolStartEvent,
        ToolEndEvent,
        InterruptEvent,
        CompactEvent,
        MessageEndEvent,
        ErrorEvent,
        DoneEvent,
        GraphUpdatedEvent,
        ToolsetUpdatedEvent,
        PluginFailedEvent,
    ],
    Field(discriminator="type"),
]
"""全部事件的判别联合，可直接用于解析或生成 TypeScript 类型。"""
