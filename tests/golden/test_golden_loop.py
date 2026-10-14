"""golden：新旧实现的事件序列逐帧一致（阶段 B 完成判据）。

- 旧实现 = `legacy_graph.py` 里冻结的改造前快照（含它自带的工具执行实现）；
- 新实现 = HarnessRuntime 合成（builtin/loop 骨架 + 5 个空锚点 + toolbox 服务）；
- 两边都走产品同一条路径（协议插件的 `translate_events`），对照：纯对话轮、一次工具调用、
  审批中断 + resume，外加"跑完后的历史等价"。

事件里的易变字段（run_id / message_id / interrupt id）比对前剥掉——要钉住的是**帧序列与内容**。

注意：**不测**"旧实现建的检查点在新实现下续跑"——旧 thread 兼容已按决定抛弃
（结构变更后旧会话不承诺可续），这条不再作为需求。
"""

import itertools

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import legacy_graph
from legacy_graph import legacy_build_graph

from app.agent.messages import build_history
from app.harness.runtime import HarnessRuntime
from app.kernel import Context
from app.plugins.builtin import approvals as approvals_plugin
from app.plugins.builtin import loop as loop_plugin
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.builtin.protocol.mapping import translate_events

pytestmark = pytest.mark.anyio

VOLATILE_KEYS = {'id', 'message_id'}
LEGACY_THREAD = 'golden-legacy'
COMPOSED_THREAD = 'golden-composed'


def make_scripted_model(script):
    """按脚本逐次返回消息的假模型；每次调用带 usage（让 message_end 有东西可对）。"""
    cursor = itertools.count()

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return 'scripted'

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise NotImplementedError('只走异步路径')

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            index = min(next(cursor), len(script) - 1)
            return ChatResult(generations=[ChatGeneration(message=script[index])])

    return ScriptedModel()


def ai(text: str, *, call: dict | None = None) -> AIMessage:
    return AIMessage(
        content=text,
        tool_calls=[call] if call else [],
        usage_metadata={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15},
    )


@tool
async def probe_echo(text: str) -> str:
    """回显文本。"""
    return f'echo:{text}'


@pytest.fixture
def scenario(monkeypatch):
    """把假工具同时接进两边：旧实现走它自己的模块级注册表，新实现走 toolbox 服务。"""

    def prepare(*, approve: bool = False) -> dict:
        probe_echo.name = 'echo'
        monkeypatch.setitem(legacy_graph.LEGACY_TOOL_MAP, 'echo', probe_echo)
        monkeypatch.setattr(
            legacy_graph, 'LEGACY_APPROVAL_REQUIRED', {'echo'} if approve else set()
        )
        return {
            'tools': (probe_echo,),
            'approvals': ('echo',) if approve else (),
        }

    return prepare


def strip(value):
    if isinstance(value, dict):
        return {key: strip(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip(item) for item in value]
    return value


async def collect(graph, thread_id: str, inputs) -> list[dict]:
    frames = []
    config = {'configurable': {'thread_id': thread_id}}
    async for event in translate_events(graph, thread_id=thread_id, config=config, inputs=inputs):
        frames.append(strip(event.model_dump(mode='json')))
    return frames


async def make_composed_graph(script: list, *, saver=None, tools=(), approvals=()):
    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    if approvals:
        await ctx.plugin(approvals_plugin.plugin, {'required': list(approvals)})
    for item in tools:
        ctx.get('tools').register(item)

    runtime = HarnessRuntime(
        ctx,
        checkpointer=saver or InMemorySaver(),
        model=make_scripted_model(list(script)),
        summary_model=make_scripted_model(list(script)),
        base=loop_plugin.plugin,
    )
    await runtime.setup()
    return runtime


async def run_pair(script: list, message: str, *, tools=(), approvals=(), saver=None):
    """旧实现与新实现各跑一轮；返回两边的图与帧序列。"""
    legacy = legacy_build_graph(
        saver or InMemorySaver(), make_scripted_model(list(script)), make_scripted_model(list(script))
    )
    legacy_frames = await collect(legacy, LEGACY_THREAD, {'messages': [HumanMessage(message)]})

    runtime = await make_composed_graph(script, saver=saver, tools=tools, approvals=approvals)
    composed = runtime.versions.current.graph
    composed_frames = await collect(composed, COMPOSED_THREAD, {'messages': [HumanMessage(message)]})
    return legacy, composed, legacy_frames, composed_frames


async def test_plain_turn_frames_identical(scenario):
    script = [ai('你好，我是假模型')]
    legacy, composed, legacy_frames, composed_frames = await run_pair(script, '你好', **scenario())

    assert composed_frames == legacy_frames
    assert composed_frames, '至少要有 message_end'
    assert composed_frames[-1]['type'] == 'message_end'


async def test_tool_turn_frames_identical(scenario):
    script = [
        ai('我来调用工具', call={'name': 'echo', 'args': {'text': 'hi'}, 'id': 'call_1', 'type': 'tool_call'}),
        ai('工具用完了'),
    ]
    legacy, composed, legacy_frames, composed_frames = await run_pair(
        script, '调一下 echo', **scenario()
    )

    assert composed_frames == legacy_frames
    kinds = [frame['type'] for frame in composed_frames]
    assert 'tool_start' in kinds and 'tool_end' in kinds and 'message_end' in kinds


async def test_interrupt_and_resume_frames_identical(scenario):
    script = [
        ai('我来调用工具', call={'name': 'echo', 'args': {'text': 'hi'}, 'id': 'call_1', 'type': 'tool_call'}),
        ai('好的'),
    ]
    legacy, composed, legacy_frames, composed_frames = await run_pair(
        script, '调一下 echo', **scenario(approve=True)
    )

    assert composed_frames == legacy_frames
    assert any(frame['type'] == 'interrupt' for frame in composed_frames)

    legacy_resume = await collect(legacy, LEGACY_THREAD, Command(resume='确认'))
    composed_resume = await collect(composed, COMPOSED_THREAD, Command(resume='确认'))
    assert composed_resume == legacy_resume
    assert composed_resume[-1]['type'] == 'message_end'


async def test_history_equivalent_after_run(scenario):
    script = [
        ai('我来调用工具', call={'name': 'echo', 'args': {'text': 'hi'}, 'id': 'call_1', 'type': 'tool_call'}),
        ai('工具用完了'),
    ]
    legacy, composed, _, _ = await run_pair(script, '调一下 echo', **scenario())

    legacy_snapshot = await legacy.aget_state({'configurable': {'thread_id': LEGACY_THREAD}})
    composed_snapshot = await composed.aget_state({'configurable': {'thread_id': COMPOSED_THREAD}})
    # 消息 id 是每次运行自动生成的，比对前剥掉——要钉的是内容与结构
    assert strip(build_history(composed_snapshot.values).model_dump()) == strip(
        build_history(legacy_snapshot.values).model_dump()
    )
