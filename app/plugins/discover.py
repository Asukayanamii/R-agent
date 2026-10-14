"""插件发现与信任：全局库（装载即信任）＋ 工作区库（默认关、首次需审批）。

来源与信任（§5.1）：

- 全局库 `PLUGINS_DIR`：`*.py`（可编程）/ `*.json`（纯声明片段），**装载即信任**；
- 工作区库 `<工作区>/.r-agent/plugins/`：只有配了 `PLUGINS_WORKSPACE` 才扫；每个文件还要
  出现在该工作区的授权清单（`<工作区>/.r-agent/plugins.json` 的 `trusted`）里才装载——
  没列出的点名报"待批准"，这就是"首次需审批、按工作区持久化"的落点。

热重载：**内容指纹**轮询（Windows 上不依赖 inotify；比 mtime 稳，绕开"同秒等长"坑）。
文件增/删/改只改**结构来源**：撤销旧贡献、装载新的，图在下一个回合边界才换（§5.4），
在飞回合照旧跑旧版本。

每个文件装载成一个**独立的 kernel 插件 fiber**——撤销就是 `fiber.dispose()`，
所以"删文件回到原样"不需要任何特判，与插件卸载走同一条路。

信任告警：工作区 `.py` 插件装载即执行任意代码，所以**只认**授权清单里的文件名；
清单本身在沙箱里是硬阻断区（`app/agent/sandbox.py`），agent 不能自己给自己授权。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import types
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from app.harness.composer import PLUGIN_SCOPE
from app.harness.fragment import StructureError, fragment_from_dict
from app.kernel import Context
from app.kernel.utils import call_plugin

logger = logging.getLogger(__name__)

CONFIG_DIR = '.r-agent'
PLUGIN_SUBDIR = 'plugins'
TRUST_FILE = 'plugins.json'
SUFFIXES = ('.py', '.json')

SOURCE_GLOBAL = '全局'
SOURCE_WORKSPACE = '工作区'

MAX_WORKSPACES = 8
"""同时保留几个工作区的插件库（LRU 卸载最久没用的那个）。与 `runtime.MAX_SCOPES` 同一数量级。"""

RETRY_COOLDOWN = 5.0
"""装载失败后、内容没变时的重试冷却（秒）。见 `_cooling`。"""


def trust_path(workspace: str | Path) -> Path:
    """工作区授权清单的位置（与 `app/agent/sandbox.py` 的硬阻断指向同一个文件）。"""
    return Path(workspace) / CONFIG_DIR / TRUST_FILE


def trusted_files(workspace: str | Path | None) -> set[str]:
    """授权清单里的文件名；清单不存在/读不了 = 一个都没批准（默认关）。"""
    if workspace is None:
        return set()
    path = trust_path(workspace)
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as error:
        logger.warning('工作区插件授权清单读不了，按"一个都没批准"处理 %s：%s', path, error)
        return set()
    trusted = payload.get('trusted') if isinstance(payload, dict) else None
    if not isinstance(trusted, list):
        logger.warning('工作区插件授权清单格式不对（要 {"trusted": [文件名, ...]}）：%s', path)
        return set()
    return {item for item in trusted if isinstance(item, str)}


def write_trust(workspace: str | Path, names: set[str]) -> None:
    """写授权清单（排序保证 diff 稳定）。**只有服务端能调**——那份文件在沙箱里是硬阻断区。"""
    path = trust_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'trusted': sorted(names)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def scope_of(workspace: str | Path) -> str:
    """作用域键 = 规范化的工作区路径（折叠大小写与分隔符；Windows 下同一目录只有一把钥匙）。"""
    path = Path(workspace).expanduser()
    try:
        path = path.resolve()
    except OSError:
        pass
    return os.path.normcase(str(path))


@dataclass
class DiscoveryReport:
    """一次扫描的结果：装载了谁、谁在等批准、谁失败、撤了谁。"""

    loaded: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.loaded or self.removed or self.failed)

    def render(self) -> str:
        parts: list[str] = []
        if self.loaded:
            parts.append(f'装载 {len(self.loaded)}：{"、".join(self.loaded)}')
        if self.removed:
            parts.append(f'撤销 {len(self.removed)}：{"、".join(self.removed)}')
        if self.pending:
            parts.append(f'待批准 {len(self.pending)}：{"、".join(self.pending)}')
        if self.failed:
            parts.append('失败：' + '；'.join(f'{name}（{reason}）' for name, reason in self.failed))
        return '；'.join(parts) or '无变化'


class PluginDiscovery:
    """扫描两级插件库 → 装载/撤销。`scan()` 幂等：没变就什么都不做。"""

    def __init__(
        self,
        ctx: Context,
        *,
        global_dir: str | Path | None = None,
        workspace: str | Path | None = None,
        scope: str = '',
        watch: bool = True,
        interval: float = 1.0,
    ) -> None:
        self.ctx = ctx
        self.global_dir = Path(global_dir) if global_dir else None
        self.workspace = Path(workspace) if workspace else None
        self.scope = scope
        """这批文件属于哪个作用域（空串 = 全局）。片段与插件名都带上它，两个工作区互不干扰。"""
        self.watch = watch
        self.interval = interval
        self._loaded: dict[Path, tuple[Any, str]] = {}
        self._failures: dict[Path, tuple[str, float]] = {}
        """最近一次失败：(内容指纹, 时间)。同一份内容在冷却期内不重试——坏文件不该每秒刷屏。"""
        self._lock = asyncio.Lock()
        """扫描互斥：`trust()`（批准即装载）与轮询任务会并发扫同一个目录，
        不加锁就会同一个新文件被装载两次（第二次撞 FRAGMENT_DUPLICATE，白噪音）。"""
        self.last_report = DiscoveryReport()

    @property
    def active(self) -> bool:
        """当前是否有装载着的插件（`ensure` 据此决定"要不要单独成图"）。"""
        return bool(self._loaded)

    # ------------------------------------------------------------------ 对外

    async def start(self) -> DiscoveryReport:
        """首次扫描；`watch=True` 时挂上热重载轮询（归当前 fiber，卸载自动停）。"""
        report = await self.scan()
        if self.watch:
            self.ctx.spawn(self._watch_loop)
        return report

    async def scan(self) -> DiscoveryReport:
        """扫一遍：增/删/改都处理，没变就什么都不做（**互斥**：并发扫描串行化）。

        装载失败的文件不进 `_loaded`，所以**每次扫描都会重试**——它等的服务可能下一轮才到
        （比如实现来自同目录的可编程插件）。
        """
        async with self._lock:
            return await self._scan()

    def fibers(self) -> list[Any]:
        """当前装着的那批 fiber（内部用：清单里把"库里的"与"内置的"分开）。"""
        return [fiber for fiber, _ in self._loaded.values()]

    def directory(self) -> Path | None:
        """这个库的目录：全局库就是 `global_dir`，工作区库是 `<工作区>/.r-agent/plugins`。"""
        return self.global_dir if self.global_dir is not None else self._workspace_dir()

    async def reload_file(self, path: Path) -> DiscoveryReport:
        """强制重读一个文件（内容指纹没变也重来）。

        用途是"我改了却不生效"和"上次装载失败想再试一次"。实现是**卸下再扫**——
        不新增生命周期原语，装载与卸载仍然只有 `_load` / `_unload` 两条路。
        """
        async with self._lock:
            if path in self._loaded:
                await self._unload(path, DiscoveryReport())
            return await self._scan()

    def inventory(self, source: str) -> list[dict[str, Any]]:
        """这一层插件库的清单：谁在、什么状态、为什么没生效。

        给模型看（`inspect_graph` 的 `plugins`）。数据全部来自已有的两个真相——
        `_loaded`（文件 → fiber，状态直接问 fiber）与 `last_report`（装载 / 待批准 / 失败原因），
        所以不用另扫一次盘，也不会和实际状态对不上。
        """
        rows: dict[str, dict[str, Any]] = {}

        for path, (fiber, _) in self._loaded.items():
            state = getattr(getattr(fiber, 'state', None), 'name', 'UNKNOWN')
            missing = list(fiber.missing_services()) if hasattr(fiber, 'missing_services') else []
            row: dict[str, Any] = {
                'name': getattr(fiber, 'name', path.stem),
                'source': source,
                'file': path.name,
                'state': state,
            }
            if missing:
                # PENDING 的常见原因是 `inject` 的服务没到——把缺的名字点出来，别让人猜
                row['reason'] = '等待服务：' + '、'.join(missing)
            rows[path.name] = row

        for name, reason in self.last_report.failed:
            rows[name] = {'name': name, 'source': source, 'file': name, 'state': 'FAILED', 'reason': reason}

        for name in self.last_report.pending:
            rows.setdefault(
                name,
                {'name': name, 'source': source, 'file': name, 'state': '待批准', 'reason': '未写入信任清单'},
            )

        # 停用的（`_` 开头）也要列出来：不然"停用"在界面上等于"消失"，用户没法再启用它
        directory = self.directory()
        if directory is not None and directory.is_dir():
            for path in sorted(directory.iterdir()):
                if not path.is_file() or not path.name.startswith('_'):
                    continue
                if path.suffix not in SUFFIXES:
                    continue
                rows.setdefault(
                    path.name,
                    {
                        'name': path.stem.lstrip('_'),
                        'source': source,
                        'file': path.name,
                        'state': '已停用',
                        'reason': '文件名以 _ 开头（插件库不扫这种文件）',
                    },
                )

        return [rows[key] for key in sorted(rows)]

    async def _scan(self) -> DiscoveryReport:
        report = DiscoveryReport()
        desired = self._desired(report)

        for path in list(self._loaded):
            if path not in desired:
                await self._unload(path, report)

        for path, (source, fingerprint) in desired.items():
            current = self._loaded.get(path)
            if current is not None and current[1] == fingerprint:
                continue
            if self._cooling(path, fingerprint):
                continue  # 同一份内容刚失败过：先放一放（改内容或过冷却期再试）
            if current is not None:
                await self._unload(path, report)  # 文件变了：先摘旧的再装新的
            await self._load(path, source, fingerprint, report)

        self.last_report = report
        return report

    # ------------------------------------------------------------------ 扫描

    def _desired(self, report: DiscoveryReport) -> dict[Path, tuple[str, str]]:
        desired: dict[Path, tuple[str, str]] = {}
        for path in self._files(self.global_dir):
            desired[path] = (SOURCE_GLOBAL, self._fingerprint(path))

        directory = self._workspace_dir()
        if directory is None:
            return desired
        trusted = self._trusted()
        for path in self._files(directory):
            if path.name not in trusted:
                report.pending.append(path.name)
                continue
            desired[path] = (SOURCE_WORKSPACE, self._fingerprint(path))
        return desired

    def _workspace_dir(self) -> Path | None:
        if self.workspace is None:
            return None
        return self.workspace / CONFIG_DIR / PLUGIN_SUBDIR

    @staticmethod
    def _files(directory: Path | None) -> list[Path]:
        """目录里的插件文件：**`.py` 先于 `.json`**——片段的实现（interface）很可能来自同目录的
        可编程插件，代码要先上；同后缀按名字排序。`_` 开头的是作者的辅助文件，不当插件。
        """
        if directory is None or not directory.is_dir():
            return []
        candidates = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix in SUFFIXES and not path.name.startswith('_')
        ]
        return sorted(candidates, key=lambda path: (path.suffix != '.py', path.name))

    @staticmethod
    def _fingerprint(path: Path) -> str:
        """内容指纹：改内容一定看得见，只 touch 不改内容不会白重载。"""
        return hashlib.sha1(path.read_bytes()).hexdigest()

    def _trusted(self) -> set[str]:
        """工作区授权清单里的文件名；清单不存在/读不了 = 一个都没批准（默认关）。"""
        return trusted_files(self.workspace) if self.workspace else set()

    # ------------------------------------------------------------------ 装载

    def _cooling(self, path: Path, fingerprint: str) -> bool:
        """这份内容刚失败过、还在冷却期里吗？

        重试是故意的（片段等的服务可能下一轮才到），但**不能每秒重试**：坏文件的异常会
        每秒刷一条 ERROR 堆栈。改内容（指纹变了）立刻重试；没改就等冷却期过。
        """
        failed = self._failures.get(path)
        if failed is None or failed[0] != fingerprint:
            return False
        return monotonic() - failed[1] < RETRY_COOLDOWN

    async def _load(
        self, path: Path, source: str, fingerprint: str, report: DiscoveryReport
    ) -> None:
        try:
            plugin, config = self._compile(path)
        except StructureError as error:
            self._failures[path] = (fingerprint, monotonic())
            report.failed.append((path.name, f'{error.code}: {error}'))
            logger.warning('插件未装载 %s（%s）', path.name, error)
            return
        except Exception as error:  # noqa: BLE001 - 一个坏文件不该拖垮发现器
            self._failures[path] = (fingerprint, monotonic())
            report.failed.append((path.name, f'{type(error).__name__}: {error}'))
            logger.warning('插件未装载 %s：%s', path.name, error)
            return

        # 先拿 fiber 引用再 await：`await ctx.plugin(...)` 等到的是**插件的返回值**，不是 fiber
        # （卸载要的是 fiber 本身——`fiber.dispose()`）。
        fiber = self.ctx.plugin(plugin, config)
        try:
            await fiber
        except Exception as error:  # noqa: BLE001 - 同上
            self._failures[path] = (fingerprint, monotonic())
            report.failed.append((path.name, f'{type(error).__name__}: {error}'))
            logger.warning('插件装载失败 %s：%s', path.name, error)
            return

        self._failures.pop(path, None)
        self._loaded[path] = (fiber, fingerprint)
        report.loaded.append(f'{path.name}（{source}）')
        logger.info('插件已装载 %s（%s）', path.name, source)

    async def _unload(self, path: Path, report: DiscoveryReport) -> None:
        fiber, _ = self._loaded.pop(path)
        try:
            result = fiber.dispose()
            if result is not None:
                await result
        except Exception as error:  # noqa: BLE001 - 卸载失败也要继续扫
            logger.warning('插件卸载失败 %s：%s', path.name, error)
        report.removed.append(path.name)

    def _compile(self, path: Path) -> tuple[Any, Any]:
        """读文件 → （插件对象, 默认配置）。

        `.py` 在这里被执行——所以工作区那份必须先批准。带 `default_config()` 的用它给的**基础配置**
        （与内置插件走容器时同一条约定：默认值通常引用 `.env`）；没有它就只吃 `Config` 里的 schema 默认值。
        """
        if path.suffix == '.json':
            return self._json_plugin(path), None
        return self._python_plugin(path)

    def _json_plugin(self, path: Path) -> dict:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as error:
            raise StructureError('DECL_JSON', f'{path.name} 不是合法 JSON：{error}') from error
        fragment = fragment_from_dict(payload, source=path.name, scope=self.scope)
        for node in fragment.nodes:
            if self.ctx.get(node.interface) is None and node.interface != 'stamp':
                raise StructureError(
                    'INTERFACE_MISSING',
                    f'抽象节点 {node.name!r} 的实现 {node.interface!r} 没有对应的服务'
                    f'——先挡在装载口，别让一个片段把整张图合成打回',
                )

        def apply(ctx: Context, config: Any = None, fragment=fragment) -> None:
            ctx.structure.contribute(fragment)

        return {'apply': apply, 'name': self._plugin_name(path)}

    def _plugin_name(self, path: Path) -> str:
        """插件名带上作用域：两个工作区里有同名文件也不冲突（内核按名字认插件）。"""
        return f'plugin-file/{self.scope or "global"}/{path.name}'

    def _python_plugin(self, path: Path) -> tuple[Any, Any]:
        """源码直读编译执行：不走 import 缓存，热重载才看得见改动（与内核 loader 同一手法）。

        `.apply` 外面包一层**作用域标记**（`PLUGIN_SCOPE`）：插件自己 `contribute(fragment)`
        时不必知道"我在哪个工作区"，由这里定。
        """
        source = path.read_text(encoding='utf-8')
        module = types.ModuleType(f'plugin_file_{path.stem}')
        module.__file__ = str(path)
        exec(compile(source, str(path), 'exec'), module.__dict__)  # noqa: S102 - 可编程插件就是要执行
        plugin = getattr(module, 'plugin', None)
        if plugin is None:
            raise StructureError('DECL_PLUGIN', f'{path.name} 里没有 plugin 对象')
        defaults = getattr(module, 'default_config', None)
        if callable(defaults):
            config = defaults()
        elif isinstance(plugin, dict) and plugin.get('Config') is not None:
            # 有 Config 却没给 default_config()：喂一个空配置，让 schema 的 default 生效、
            # 校验真的跑一遍（必填缺了就按契约**拒绝装载**，而不是把 None 塞给插件）。
            config = {}
        else:
            config = None
        if not self.scope or not isinstance(plugin, dict) or 'apply' not in plugin:
            return plugin, config
        return {**plugin, 'apply': self._scoped(plugin['apply'])}, config

    def _scoped(self, apply: Any) -> Any:
        """把插件的作用域在装载期标进 `PLUGIN_SCOPE`（同步/异步 apply 都覆盖）。"""
        scope = self.scope

        async def run_async(coro: Any) -> Any:
            token = PLUGIN_SCOPE.set(scope)
            try:
                return await coro
            finally:
                PLUGIN_SCOPE.reset(token)

        def wrapper(ctx: Context, config: Any = None) -> Any:
            token = PLUGIN_SCOPE.set(scope)
            try:
                # 走 call_plugin：`def apply(ctx)` 的作者不该因为包装层而被要求多收一个参数
                # （按签名传参是内核的约定，包装层不能绕过它）
                result = call_plugin(apply, ctx, config)
            except BaseException:
                PLUGIN_SCOPE.reset(token)
                raise
            if inspect.isawaitable(result):
                return run_async(result)
            PLUGIN_SCOPE.reset(token)
            return result

        return wrapper

    async def dispose(self) -> DiscoveryReport:
        """卸载全部（LRU 回收 / 停机）：与"删文件"走同一条路——撤销贡献、释放 fiber。"""
        report = DiscoveryReport()
        for path in list(self._loaded):
            await self._unload(path, report)
        self.last_report = report
        return report

    # ------------------------------------------------------------------ 热重载

    async def _watch_loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                report = await self.scan()
            except Exception as error:  # noqa: BLE001 - 看门狗不能死在一个坏文件上
                logger.warning('插件热重载扫描失败：%s', error)
                continue
            if report.changed:
                logger.info('插件热重载（下一回合边界生效）：%s', report.render())


class PluginLibraries:
    """按作用域装载插件库：全局库（作用域 `''`）+ 每个会话工作区各自的库（按需、LRU）。

    - **一个**轮询任务扫全部已登记的工作区（内容指纹，逻辑复用 `PluginDiscovery.scan`）；
    - `ensure(workspace)` 是回合边界上的入口：登记/取用该工作区的库，返回**规范化作用域**——
      空串表示"这个工作区没有自己的插件"，会话照旧共用全局那张图（省掉一个没人用的版本账本）；
    - 信任清单按工作区各自一份：A 批准的不等于 B 批准；`trust()` 是**服务端**写清单的唯一入口
      （那份文件在沙箱里是硬阻断区，agent 自己写不了）；
    - 超过 `max_workspaces` 时卸载最久没用过的那个工作区的库（撤销它的片段贡献与 fiber）。
    """

    def __init__(
        self,
        ctx: Context,
        *,
        global_dir: str | Path | None = None,
        workspace_enabled: bool = False,
        watch: bool = True,
        interval: float = 1.0,
        max_workspaces: int = MAX_WORKSPACES,
    ) -> None:
        self.ctx = ctx
        self.global_dir = Path(global_dir) if global_dir else None
        self.workspace_enabled = workspace_enabled
        self.watch = watch
        self.interval = interval
        self.max_workspaces = max(1, max_workspaces)
        self._global: PluginDiscovery | None = None
        self._workspaces: dict[str, PluginDiscovery] = {}
        self._order: list[str] = []
        """LRU：最近用过的排最后。"""
        self.last_report = DiscoveryReport()

    # ------------------------------------------------------------------ 对外

    async def start(self) -> DiscoveryReport:
        """装载全局库（有的话）；`watch=True` 时挂上唯一的轮询任务（归当前 fiber）。"""
        if self.global_dir is not None:
            self._global = PluginDiscovery(
                self.ctx, global_dir=self.global_dir, scope='', watch=False, interval=self.interval
            )
            self.last_report = await self._global.scan()
        if self.watch:
            self.ctx.spawn(self._scan_loop)
        return self.last_report

    async def ensure(self, workspace: str) -> str:
        """登记/取用该工作区的插件库；返回规范化作用域（`''` = 不用单独成图）。

        热路径：已登记的工作区只做一次字典查找（扫描由轮询任务负责，~1 秒的迟到与
        热重载本身的口径一致）；批准走 `trust()`，那边会**立刻**扫一次。
        """
        if not self.workspace_enabled or not workspace:
            return ''
        canonical = scope_of(workspace)
        discovery = self._workspaces.get(canonical)
        if discovery is None:
            discovery = await self._register(workspace, canonical)
        self._touch(canonical)
        self._evict()
        return canonical if discovery.active else ''

    async def _register(self, workspace: str, canonical: str) -> PluginDiscovery:
        discovery = PluginDiscovery(
            self.ctx,
            workspace=Path(workspace),
            scope=canonical,
            watch=False,
            interval=self.interval,
        )
        report = await discovery.scan()
        if report.changed:
            logger.info('工作区插件库（%s）：%s', canonical, report.render())
        self._workspaces[canonical] = discovery
        return discovery

    async def trust(self, workspace: str, filename: str) -> bool:
        """把某个文件写进该工作区的信任清单（保留已有条目）；返回是否新写入。

        服务端专用：`web/api`（或 `graph_tools` 插件）在人工确认之后调它。
        批准**立刻装载**（生效仍在下一个回合边界）——不然要等轮询那一秒，用时序换来的
        "差不多"是最难查的那种 bug。
        """
        names = trusted_files(workspace)
        if filename in names:
            return False
        write_trust(workspace, names | {filename})
        logger.info('工作区插件已批准：%s（%s）', filename, scope_of(workspace) or workspace)

        canonical = scope_of(workspace)
        discovery = self._workspaces.get(canonical) or await self._register(workspace, canonical)
        report = await discovery.scan()
        if report.changed:
            logger.info('工作区插件库（%s）：%s', canonical, report.render())
        self._touch(canonical)
        return True

    async def untrust(self, workspace: str, filename: str) -> bool:
        """把文件从信任清单里去掉（撤销批准）→ **立刻卸载**。返回是否真的去掉过。

        与 `trust()` 对称：写清单 + 立刻扫一次。**不删文件**——撤信任只是"不再装载"，
        文件还在、随时能再批准；"删除"是另一个动作（危险得多，由管理面显式确认）。
        """
        names = trusted_files(workspace)
        if filename not in names:
            return False
        write_trust(workspace, names - {filename})
        logger.info('工作区插件已撤信任：%s（%s）', filename, scope_of(workspace) or workspace)
        await self.rescan(workspace)
        return True

    async def rescan(self, workspace: str) -> DiscoveryReport:
        """立刻扫一次该工作区的库（不等轮询那一秒）。"""
        discovery = await self._discovery_for(workspace)
        if discovery is None:
            return DiscoveryReport()
        report = await discovery.scan()
        if report.changed:
            logger.info('工作区插件库（%s）：%s', scope_of(workspace), report.render())
        return report

    async def rescan_global(self) -> DiscoveryReport:
        """立刻扫一次全局库（管理面停用/启用后不用等轮询）。"""
        if self._global is None:
            return DiscoveryReport()
        report = await self._global.scan()
        if report.changed:
            logger.info('全局插件库：%s', report.render())
        return report

    async def reload(self, workspace: str | None, filename: str) -> DiscoveryReport:
        """强制重读某个文件（改了不生效 / 装载失败想再试一次时用）。

        `workspace` 为空 = 全局库。
        """
        discovery = self._global if not workspace else await self._discovery_for(workspace)
        if discovery is None or not filename:
            return DiscoveryReport()
        directory = discovery.directory()
        if directory is None:
            return DiscoveryReport()
        return await discovery.reload_file(directory / filename)

    async def _discovery_for(self, workspace: str) -> PluginDiscovery | None:
        if not self.workspace_enabled or not workspace:
            return None
        canonical = scope_of(workspace)
        discovery = self._workspaces.get(canonical)
        if discovery is None:
            discovery = await self._register(workspace, canonical)
        self._touch(canonical)
        return discovery

    def library_dirs(self, workspace: str | None = None) -> dict[str, str | None]:
        """两级插件库的目录（管理面用来展示"文件放哪"）。"""
        workspace_dir = (
            str(Path(workspace) / CONFIG_DIR / PLUGIN_SUBDIR) if workspace else None
        )
        return {
            SOURCE_GLOBAL: str(self._global.directory()) if self._global else None,
            SOURCE_WORKSPACE: workspace_dir,
        }

    def loaded_summary(self) -> str:
        """一行摘要（日志用）：全局装载情况 + 已登记的工作区数。"""
        parts: list[str] = []
        if self._global is not None:
            parts.append(f'全局库 {self._global.last_report.render()}')
        if self._workspaces:
            active = sum(1 for item in self._workspaces.values() if item.active)
            parts.append(f'工作区库已登记 {len(self._workspaces)} 个（其中 {active} 个有插件）')
        elif not self.workspace_enabled:
            parts.append('工作区库未启用（PLUGINS_WORKSPACE=0）')
        return '；'.join(parts)

    def inventory(self, workspace: str | None = None) -> list[dict[str, Any]]:
        """**模型可见的插件总清单**：库里的（全局 + 当前工作区）+ 内核注册表里的内置那批。

        与 `loaded_summary()` 的分工：那个是给人看的一行摘要（日志），这个回答模型那个问题——
        "我有哪些插件、谁没生效、为什么"。这正是 inspect_graph 的 `plugins` 字段，
        也是"只加工具 / 只挂资产的插件在图里看不见"的补位。
        """
        rows: list[dict[str, Any]] = []
        discovered: set[int] = set()

        if self._global is not None:
            rows.extend(self._global.inventory('全局'))
            discovered.update(id(fiber) for fiber in self._global.fibers())

        if workspace:
            discovery = self._workspaces.get(scope_of(workspace))
            if discovery is not None:
                rows.extend(discovery.inventory('工作区'))
                discovered.update(id(fiber) for fiber in discovery.fibers())

        rows.extend(self._builtin_rows(discovered))
        return rows

    def _builtin_rows(self, discovered: set[int]) -> list[dict[str, Any]]:
        """注册表里非插件库来源的那批（内置插件、装配根直接装载的）：名字 + 状态。

        没有 `registry` 就返回空：清单少一段，总好过让工具直接报错。
        """
        registry = getattr(self.ctx, 'registry', None)
        if registry is None:
            return []
        rows: list[dict[str, Any]] = []
        for _key, runtime in registry.entries():
            for fiber in runtime.fibers:
                if id(fiber) in discovered:
                    continue
                rows.append(
                    {'name': fiber.name, 'source': '内置', 'state': fiber.state.name}
                )
        return sorted(rows, key=lambda row: row['name'])

    # ------------------------------------------------------------------ 内部

    def _touch(self, canonical: str) -> None:
        if canonical in self._order:
            self._order.remove(canonical)
        self._order.append(canonical)

    def _evict(self) -> None:
        """超上限就回收最久没用过的那个工作区（卸载异步做，不卡住本次 ensure）。"""
        while len(self._workspaces) > self.max_workspaces and self._order:
            oldest = self._order.pop(0)
            discovery = self._workspaces.pop(oldest, None)
            if discovery is not None:
                self.ctx.spawn(self._drop(discovery, oldest))

    async def _drop(self, discovery: PluginDiscovery, canonical: str) -> None:
        try:
            report = await discovery.dispose()
        except Exception as error:  # noqa: BLE001 - 回收失败不该带走进程
            logger.warning('工作区插件库回收失败 %s：%s', canonical, error)
            return
        logger.info('工作区插件库已回收（LRU）：%s——%s', canonical, report.render())

    async def _scan_loop(self) -> None:
        """唯一的看门狗：全局库 + 全部已登记的工作区库各扫一遍。"""
        while True:
            await asyncio.sleep(self.interval)
            targets = [
                item
                for item in [self._global, *self._workspaces.values()]
                if item is not None
            ]
            for discovery in targets:
                try:
                    report = await discovery.scan()
                except Exception as error:  # noqa: BLE001 - 一个坏目录不该停掉看门狗
                    logger.warning('插件热重载扫描失败：%s', error)
                    continue
                if report.changed:
                    logger.info(
                        '插件热重载（下一回合边界生效）%s：%s',
                        discovery.scope or '全局',
                        report.render(),
                    )
