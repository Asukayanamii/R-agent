"""builtin/protocol：必需插件——wire 形状、SSE 编码与出站接缝映射（provides ``protocol:v1``）。

职责（设计稿 §2.7 / §6）：把内核在**固定输出点**上产出的东西翻译成 wire 形状，再编码成
SSE 帧；`error` / `done` 是这条流唯一的出口（任何异常都在这里落地）。事件形状随**内核版本**
走、不随别的插件变化——否则前端会被"某个插件在不在"搞崩。

**可替换、不可停用**：换一份实现 = 换一个插件；停用协议 = 没有对话能力，所以它进必需清单。
`webui` 这类前端插件依赖的是这张形状表（`requires: protocol:v1`），不是内核内部结构。
"""

from __future__ import annotations

from typing import Any

from app.harness.events import (
    GraphUpdatedData,
    GraphUpdatedEvent,
    InterruptData,
    InterruptEvent,
    MessageEndData,
    MessageEndEvent,
    ThreadData,
    ThreadEvent,
    ToolsetUpdatedData,
    ToolsetUpdatedEvent,
)
from app.kernel import Service

from .encoding import SSE_HEADERS, SSE_MEDIA_TYPE, encode_sse, sse_stream
from .mapping import translate_events

PLUGIN_ID = 'builtin/protocol'
PROTOCOL_VERSION = 'protocol:v1'
"""契约版本：加字段/加事件类型不算破坏（不 bump）；改语义/删字段才 bump。"""


class ProtocolService(Service):
    """``ctx.protocol``：协议的唯一入口——构造 / 编码 / 映射。"""

    def __init__(self, ctx: Any) -> None:
        super().__init__(ctx, 'protocol')
        self.version = PROTOCOL_VERSION

    # ---------------------------------------------------------------- 构造
    def thread_event(self, thread_id: str) -> ThreadEvent:
        """回合开始：会话握手（必为第一帧）。"""
        return ThreadEvent(data=ThreadData(thread_id=thread_id))

    def interrupt_event(self, data: InterruptData) -> InterruptEvent:
        """待确认：把中断载荷包成 wire 事件。"""
        return InterruptEvent(data=data)

    def message_end_event(self, message_id: str) -> MessageEndEvent:
        """本条消息结束：不带 usage（usage 由映射层从模型输出累加）。"""
        return MessageEndEvent(data=MessageEndData(message_id=message_id))

    def graph_updated_event(self, version: Any, *, mermaid: str = '') -> GraphUpdatedEvent:
        """结构已更新：带本版本的 id/哈希与图源码（前端可折叠卡片）。"""
        return GraphUpdatedEvent(
            data=GraphUpdatedData(
                version=version.id,
                structure_hash=version.structure_hash,
                base=version.base_id,
                mermaid=mermaid,
            )
        )

    def toolset_updated_event(self, tools: list[str]) -> ToolsetUpdatedEvent:
        """能力集已更新：下一次模型调用就会带上新快照。"""
        return ToolsetUpdatedEvent(data=ToolsetUpdatedData(tools=list(tools)))

    # ---------------------------------------------------------------- 编码
    @property
    def media_type(self) -> str:
        return SSE_MEDIA_TYPE

    @property
    def headers(self) -> dict[str, str]:
        return dict(SSE_HEADERS)

    def encode(self, event: Any, seq: int) -> str:
        """编码单帧（供需要自己拼流的调用方使用）。"""
        return encode_sse(event, seq)

    def sse_stream(self, events: Any) -> Any:
        """包装业务事件流：补 done、异常补 error + done（流式半边唯一出口）。"""
        return sse_stream(events)

    # ---------------------------------------------------------------- 映射
    def translate(self, graph: Any, *, thread_id: str, config: dict, inputs: Any) -> Any:
        """把一次图执行翻译成统一事件流（出站接缝 → wire 形状）。"""
        return translate_events(graph, thread_id=thread_id, config=config, inputs=inputs)


def apply(ctx: Any) -> None:
    ProtocolService(ctx)
    ctx.logger.info('协议已就绪：%s', PROTOCOL_VERSION)


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'provides': PROTOCOL_VERSION,
}
