"""阶段 F：agent 自改图——模型写声明、人点批准、**下一个回合边界**生效。

判据（计划 §8 F）：从"发现需要"到"能用"全程**无人改代码**（人只点审批）；
事后能回放"改前/改后/依据/怎么退"（审计表 + 对话 + 删文件）。
"""

import itertools
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.harness.inspect import GraphInfo
from app.harness.runtime import HarnessRuntime
from app.kernel import Context
from app.plugins.builtin import graph_tools
from app.plugins.builtin import loop as loop_plugin
from app.plugins.builtin import toolbox as toolbox_plugin
from app.plugins.builtin.protocol.mapping import translate_events
from app.plugins.discover import PluginLibraries, scope_of, trusted_files

pytestmark = pytest.mark.anyio

USAGE = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
DECLARATION = json.dumps(
    {
        'id': 'workspace/probe_notes',
        'anchor': 'pre_model',
        'chain': [{'name': 'probe_notes', 'interface': 'probe'}],
    },
    ensure_ascii=False,
)


async def probe_node(state: dict) -> dict:
    return {}


def make_model(script: list):
    cursor = itertools.count()

    class ScriptedModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return 'scripted'

        def bind_tools(self, tools, **kwargs):  # noqa: ARG002
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise NotImplementedError

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            index = min(next(cursor), len(script) - 1)
            return ChatResult(generations=[ChatGeneration(message=script[index])])

    return ScriptedModel()


def call(name: str, args: dict) -> AIMessage:
    return AIMessage(
        content=f'调用 {name}',
        tool_calls=[{'name': name, 'args': args, 'id': 'call_1', 'type': 'tool_call'}],
        usage_metadata=USAGE,
    )


async def make_wired(tmp_path, script: list, *, allow_install: bool = False):
    """真装配：工具箱 + 工作区插件库 + 自改图工具 + 只读图视图。"""
    workspace = tmp_path / 'ws'
    (workspace / '.my_agent' / 'plugins').mkdir(parents=True)

    ctx = Context()
    await ctx.plugin(toolbox_plugin.plugin, essential=True)
    ctx.provide('probe', probe_node)
    libraries = PluginLibraries(ctx, global_dir=None, workspace_enabled=True, watch=False)
    await libraries.start()
    ctx.provide('plugins', libraries)
    await ctx.plugin(graph_tools.plugin, {'allow_install_plugin': allow_install})

    runtime = HarnessRuntime(
        ctx,
        checkpointer=InMemorySaver(),
        model=make_model(list(script)),
        summary_model=make_model(list(script)),
        base=loop_plugin.plugin,
    )
    runtime.on_scope = libraries.ensure
    ctx.provide('graph', GraphInfo(runtime))
    await runtime.setup()
    return runtime, libraries, workspace


async def run_turn(runtime, workspace, thread_id: str, message: str = '', *, inputs=None):
    """跑一轮（`inputs=None` 就是新消息，给 `Command(resume=...)` 就是恢复审批）。"""
    config = {'configurable': {'thread_id': thread_id, 'workspace': str(workspace)}}
    frames = []
    async with runtime.lease(thread_id, scope=str(workspace)) as version:
        async for event in translate_events(
            version.graph,
            thread_id=thread_id,
            config=config,
            inputs=inputs if inputs is not None else {'messages': [HumanMessage(message)]},
        ):
            frames.append(event.model_dump(mode='json'))
        snapshot = await version.graph.aget_state(config)
    return frames, snapshot


async def begin_turn(runtime, workspace, thread_id: str = 'next') -> None:
    async with runtime.lease(thread_id, scope=str(workspace)):
        pass


def last_tool_text(snapshot) -> str:
    """最后一条**工具结果**的正文——模型随后那句收尾话不算（工具结果才是承重的）。"""
    for message in reversed(snapshot.values['messages']):
        if isinstance(message, ToolMessage):
            return str(message.content)
    raise AssertionError('这一轮没有工具结果')


def active_scope(runtime, workspace) -> str:
    """本工作区当前实际生效的作用域：还没自己的插件时是 `''`（共用全局那张图）。"""
    scope = scope_of(workspace)
    version = runtime.registry(scope).current
    return scope if version is not None else ''


def nodes_of(runtime, workspace) -> frozenset:
    return runtime.registry(active_scope(runtime, workspace)).current.node_names


async def test_model_writes_fragment_and_human_approves(tmp_path):
    """判据：模型写声明 → 人点批准 → 下一个回合边界图里多一条链（全程没人改代码）。"""
    script = [
        call('add_fragment', {'name': 'probe_notes', 'declaration': DECLARATION}),
        AIMessage(content='加好了', usage_metadata=USAGE),
    ]
    runtime, libraries, workspace = await make_wired(tmp_path, script)
    target = workspace / '.my_agent' / 'plugins' / 'probe_notes.json'

    frames, snapshot = await run_turn(runtime, workspace, 'self-edit', '给 pre_model 加一条链')
    assert snapshot.interrupts, '应该停在"请求批准"上（走的是现成审批卡）'
    assert target.is_file(), '声明已经写进工作区插件库'
    assert trusted_files(workspace) == set(), '没批准就不会进信任清单'
    assert 'probe_notes' not in nodes_of(runtime, workspace)

    frames, snapshot = await run_turn(
        runtime, workspace, 'self-edit', inputs=Command(resume='确认')
    )
    assert not snapshot.interrupts
    assert trusted_files(workspace) == {'probe_notes.json'}
    # 批准发生在这一轮里，但**图不换**：本轮仍跑旧图
    assert 'probe_notes' not in nodes_of(runtime, workspace)

    await begin_turn(runtime, workspace)  # 下一个回合边界
    assert 'probe_notes' in nodes_of(runtime, workspace)
    report = runtime.build_report(scope=active_scope(runtime, workspace))
    pre_model = next(item for item in report.mounts if item.anchor == 'pre_model')
    assert pre_model.chain == ('workspace/probe_notes',)


async def test_rejection_keeps_it_pending(tmp_path):
    """拒绝：文件留着当"待批准"，图一动不动——"怎么退"就是删这个文件。"""
    script = [
        call('add_fragment', {'name': 'probe_notes', 'declaration': DECLARATION}),
        AIMessage(content='那我先不动它', usage_metadata=USAGE),
    ]
    runtime, libraries, workspace = await make_wired(tmp_path, script)
    target = workspace / '.my_agent' / 'plugins' / 'probe_notes.json'

    await run_turn(runtime, workspace, 'self-edit', '加一条链')
    await run_turn(runtime, workspace, 'self-edit', inputs=Command(resume='取消'))
    await begin_turn(runtime, workspace)

    assert target.is_file() and trusted_files(workspace) == set()
    assert 'probe_notes' not in nodes_of(runtime, workspace)


async def test_bad_declaration_is_refused_before_writing(tmp_path):
    """声明不合法（锚点不存在 / 接口没实现）当场拒绝：不落文件、不弹卡。"""
    script = [
        call(
            'add_fragment',
            {'name': 'broken', 'declaration': json.dumps({'id': 'x', 'anchor': 'nowhere', 'chain': [{'name': 'n', 'interface': 'probe'}]})},
        ),
        AIMessage(content='知道了', usage_metadata=USAGE),
    ]
    runtime, libraries, workspace = await make_wired(tmp_path, script)

    frames, snapshot = await run_turn(runtime, workspace, 'self-edit', '加一条链')

    assert not snapshot.interrupts
    assert not (workspace / '.my_agent' / 'plugins' / 'broken.json').exists()
    text = last_tool_text(snapshot)
    assert 'nowhere' in text and '不在骨架锚点表' in text


async def test_install_plugin_is_gated_by_config(tmp_path):
    """`install_plugin` 默认关：拒绝执行，不写文件、不弹卡；显式放行才可用。"""
    source = 'async def node(state):\n    return {}\n\n\ndef apply(ctx):\n    ctx.provide("probe2", node)\n\n\nplugin = {"apply": apply, "name": "ws/probe2"}\n'
    script = [
        call('install_plugin', {'name': 'probe2', 'source': source}),
        AIMessage(content='知道了', usage_metadata=USAGE),
    ]
    runtime, libraries, workspace = await make_wired(tmp_path, script)

    frames, snapshot = await run_turn(runtime, workspace, 'self-edit', '写个插件')

    assert not snapshot.interrupts
    assert not (workspace / '.my_agent' / 'plugins' / 'probe2.py').exists()
    assert 'allow_install_plugin' in last_tool_text(snapshot)

    # 放行之后：写文件 + 弹卡
    runtime2, _, workspace2 = await make_wired(
        tmp_path / 'second', script, allow_install=True
    )
    frames, snapshot = await run_turn(runtime2, workspace2, 'self-edit', '写个插件')
    assert snapshot.interrupts
    assert (workspace2 / '.my_agent' / 'plugins' / 'probe2.py').is_file()


async def test_inspect_graph_reads_the_current_picture(tmp_path):
    """`inspect_graph` 给模型一份只读的现状：报告 + 片段清单 + 最近审计。"""
    script = [
        call('inspect_graph', {}),
        AIMessage(content='看完了', usage_metadata=USAGE),
    ]
    runtime, libraries, workspace = await make_wired(tmp_path, script)

    frames, snapshot = await run_turn(runtime, workspace, 'self-edit', '现在图长什么样')

    payload = json.loads(last_tool_text(snapshot))
    assert payload['report']['base'] == 'builtin/loop'
    assert payload['report']['structure_hash']
    assert 'builtin/loop' in payload['report']['participants']
    assert payload['spec']['fragments'][0]['id'] == 'builtin/loop'
    assert isinstance(payload['recent'], list)
    # 服务清单：纯 `.json` 形态靠它发现"能接什么"（`callable` 标记：可调用的才能直接当节点实现）
    services = {item['name']: item['callable'] for item in payload['services']}
    assert 'tools' in services
    assert services['tools'] is False  # Service 实例本身不可调用（要当节点得实现 __call__）


async def test_resume_works_before_the_scope_exists(tmp_path):
    """真应用里抓到的 bug：中断发生在**全局作用域**（这个工作区还没有自己的插件库）时，
    resume 的兼容判定不能拿"原始工作区字符串"去查账本——那会命中一个刚建的空账本，
    把任何节点都判成"不在当前运行图里"。"""
    from app.plugins.builtin import protocol as protocol_plugin

    script = [
        call('add_fragment', {'name': 'probe_notes', 'declaration': DECLARATION}),
        AIMessage(content='加好了', usage_metadata=USAGE),
    ]
    runtime, libraries, workspace = await make_wired(tmp_path, script)
    await runtime.ctx.plugin(protocol_plugin.plugin, essential=True)

    from app.agent.langgraph_runner import LangGraphRunner

    runner = LangGraphRunner(runtime, runtime.ctx.get('protocol'))
    frames = [
        item
        async for item in runner.stream('t1', '加一条链', workspace=str(workspace))
    ]
    assert any(item.type == 'interrupt' for item in frames)

    frames = [
        item
        async for item in runner.resume('t1', '确认', workspace=str(workspace))
    ]

    assert not any(item.type == 'error' for item in frames), [item.model_dump() for item in frames]
    assert trusted_files(workspace) == {'probe_notes.json'}
