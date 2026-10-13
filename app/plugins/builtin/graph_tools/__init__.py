"""builtin/graph_tools：agent 自改图（可选插件）——三个工具 + 人工审批。

- `add_fragment`：把一条 `.json` 片段声明写进**当前工作区**的插件库，然后弹卡请人批准；
  批准 = 服务端把它写进信任清单，**下一个回合边界**生效（结构变化只在边界换图，§4.4）。
- `install_plugin`：同上，但 `.py` = 执行任意代码——由 `Config.allow_install_plugin`（默认 0）显式放行。
- `inspect_graph`：读当前合成报告、片段清单与最近几次审计（服务 `graph`）——"改前/改后/依据/怎么退"
  里的前三样都从它来。

**审批是唯一的门槛**：写文件只是"声明"，没进信任清单就不会被装载；信任清单在沙箱里是硬阻断区，
agent 自己写不了——只有这里（人点过"确认"之后）才写它。拒绝时声明文件保留为"待批准"，
用户可以稍后批准，也可以直接删掉（删文件 = 撤销声明，回合边界回到原样）。

配置：`enabled`（默认 1，装载即注册这三个工具）、`allow_install_plugin`（默认 0）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from app.agent.runtime import current_workspace
from app.agent.sandbox import WRITE, guard_path
from app.agent.tools.common import fail, rel
from app.common.exceptions import SandboxDenied
from app.harness.fragment import StructureError, fragment_from_dict
from app.kernel import Schema, Service

PLUGIN_ID = 'builtin/graph_tools'

APPROVE = '确认'
OPTIONS = (APPROVE, '取消')


class Config(Schema):
    enabled = Schema.boolean().default(True).description('关掉 = 不注册自改图工具')
    allow_install_plugin = Schema.boolean().default(False).description(
        '允许 install_plugin 写 .py（等于执行任意代码）；默认关'
    )


def plugins_dir() -> Path:
    """当前工作区的插件库目录（`<工作区>/.my_agent/plugins/`）。"""
    from app.common.config import CONFIG_DIR

    return current_workspace.get() / CONFIG_DIR / 'plugins'


def _ask(prompt: str) -> str:
    """弹确认卡；不在会话里（测试 / CLI 直调）就按"拒绝"处理，并说清原因。"""
    from langgraph.types import interrupt

    try:
        return interrupt({'prompt': prompt, 'options': list(OPTIONS)})
    except RuntimeError:
        fail('当前不在会话中，无法征求批准，已按未批准处理。')


def _write(target: Path, text: str) -> None:
    try:
        guard_path(str(target), WRITE)
    except SandboxDenied as exc:
        fail(str(exc))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8', newline='')


def _normalized(name: str, declaration: str, ctx: Any) -> str:
    """先按插件库那套规则验一遍（失败当场说清，别等人批准一个注定装不上的文件）。

    查三层：JSON 形状（`fragment_from_dict`）、锚点在不在骨架锚点表里（跨片段规则里
    唯一能在写之前查的那条）、每个 interface 有没有对应的服务。
    """
    try:
        payload = json.loads(declaration)
    except json.JSONDecodeError as exc:
        fail(f'声明不是合法 JSON：{exc}')
    try:
        fragment = fragment_from_dict(payload, source=f'{name}.json')
    except StructureError as exc:
        fail(f'声明不合法（{exc.code}）：{exc}')

    base = ctx.structure.base()
    if base is not None and fragment.anchor not in base.anchors:
        fail(
            f'锚点 {fragment.anchor!r} 不在骨架锚点表里；可用：{"、".join(base.anchors)}'
        )
    for node in fragment.nodes:
        if node.interface != 'stamp' and ctx.get(node.interface) is None:
            fail(
                f'抽象节点 {node.name!r} 的实现 {node.interface!r} 没有对应的服务：'
                f'先有一个提供它的插件，或换成已存在的服务名'
            )
    return json.dumps(payload, ensure_ascii=False, indent=2) + '\n'


def default_config() -> dict:
    """默认值引用总体配置（`.env`）：`PLUGIN_AUTHOR_INSTALL` 默认关。

    写 `.py` 等于执行任意代码，所以默认不许；数据形态的片段（`add_fragment`）不受它影响。
    """
    from app.common import config

    return {'enabled': True, 'allow_install_plugin': config.PLUGIN_AUTHOR_INSTALL}


class GraphTools(Service):
    """服务 ``graph_tools``：三个工具本体（装配时注册进服务 `tools`）。"""

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx, 'graph_tools')
        options = dict(config or {})
        self.enabled = bool(options.get('enabled', True))
        self.allow_install_plugin = bool(options.get('allow_install_plugin', False))

    # ---------------------------------------------------------------- 工具
    def tools(self) -> list[Any]:
        if not self.enabled:
            return []
        service = self

        @tool
        async def add_fragment(name: str, declaration: str) -> str:
            """把一条片段声明（JSON）写进当前工作区的插件库，并请求人工批准。

            - name：文件名（不含后缀），取能看懂的名字，如 research_notes
            - declaration：JSON 文本，形如
              {"id": "workspace/research", "anchor": "pre_model",
               "chain": [{"name": "research_notes", "interface": "research"}]}
            - anchor 必须是骨架锚点之一：entry / pre_model / post_model / post_tools / finish
            - 每个节点必须给 interface（**服务名**，实现由别的插件提供）
            - 批准后在**下一个回合边界**生效；拒绝时文件保留为"待批准"，删掉它就是撤销
            """
            return await service._submit('add_fragment', name, declaration)

        @tool
        async def install_plugin(name: str, source: str) -> str:
            """把一个可编程插件（Python 源码）写进当前工作区插件库，并请求人工批准。

            - source 必须是完整源码，里面有 plugin = {"apply": apply, "name": "..."}
            - 这等于让 agent 写能在进程里执行的代码：只有装配时显式放行才可用
            - 批准后在**下一个回合边界**生效；删文件 = 卸载
            """
            if not service.allow_install_plugin:
                fail(
                    'install_plugin 已关闭（allow_install_plugin=0）：写 .py 等于执行任意代码，'
                    '需要人在装配时显式放行。声明数据片段请用 add_fragment。'
                )
            return await service._submit('install_plugin', name, source)

        @tool
        async def inspect_graph() -> str:
            """读当前图：合成报告、运行中版本的片段清单、最近几次版本切换（审计）。

            想知道"图现在长什么样 / 上一次为什么换图 / 哪条链挂在哪个锚点上"就调它。
            """
            return await service._inspect()

        return [add_fragment, install_plugin, inspect_graph]

    # ---------------------------------------------------------------- 内部
    async def _inspect(self) -> str:
        from app.plugins.discover import scope_of

        workspace = str(current_workspace.get())
        graph = self._service_ctx.get('graph')
        payload: dict[str, Any] = {'workspace': workspace}
        if graph is None:
            payload['error'] = '没有 graph 服务（装配根没接上）'
        else:
            scope = scope_of(workspace)
            if scope not in graph.scopes():
                scope = ''
            payload['scope'] = scope
            payload['report'] = graph.report(scope)
            payload['spec'] = graph.spec(scope)
            payload['services'] = self._services()
            payload['recent'] = await graph.recent(scope, limit=10)
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _services(self) -> list[dict[str, Any]]:
        """当前有哪些服务名，可作为片段的 `interface`（可调用的才能直接当节点实现）。

        没有这份清单，模型写纯 `.json` 片段时只能靠猜服务名（`INTERFACE_MISSING` 只说"没有实现"，
        不说"有哪些"）。声明过 ≠ 此刻可用：这里只报**当前拿得到值**的。
        """
        reflect = getattr(self._service_ctx, 'reflect', None)
        if reflect is None:
            return []
        out = []
        for name in reflect.names():
            value = self._service_ctx.get(name, strict=False)
            if value is None:
                continue
            out.append({'name': name, 'callable': callable(value)})
        return out

    async def _submit(self, kind: str, name: str, text: str) -> str:
        libraries = self._service_ctx.get('plugins')
        if libraries is None:
            fail('没有插件库服务（PLUGINS_ENABLED=0 或服务 `plugins` 缺席），自改图不可用')

        suffix = '.py' if kind == 'install_plugin' else '.json'
        # 名字里已经带了后缀就别再加一个（真跑里模型传 "panel.py" → 落盘成 "panel.py.py"）
        stem = name[:-len(suffix)] if name.endswith(suffix) else name
        target = plugins_dir() / f'{stem}{suffix}'
        if kind == 'add_fragment':
            text = _normalized(stem, text, self._service_ctx)

        _write(target, text)
        decision = _ask(
            f'批准这个插件吗？\n{rel(target)}\n\n'
            f'批准后在**下一个回合边界**生效（当前这一轮仍跑旧图）。'
        )
        if decision != APPROVE:
            return (
                f'未获批准：{rel(target)} 已写入，但不会被装载（仍在"待批准"名单里）。'
                f'可以稍后让人批准，或删掉这个文件撤销。'
            )

        await libraries.trust(str(current_workspace.get()), target.name)
        return (
            f'已批准并装载：{rel(target)}。变更在**下一个回合边界**生效'
            f'（不会影响正在跑的这一轮）。'
        )


def apply(ctx: Any, config: Any = None) -> None:
    service = GraphTools(ctx, config)
    if not service.enabled:
        ctx.logger.info('自改图工具已关闭（enabled=false）')
        return
    for item in service.tools():
        ctx.tools.register(item)
    ctx.logger.info(
        '自改图工具已注册：add_fragment / inspect_graph%s',
        ' / install_plugin' if service.allow_install_plugin else '（install_plugin 关闭）',
    )


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'inject': ['tools'],
    'Config': Config,
}