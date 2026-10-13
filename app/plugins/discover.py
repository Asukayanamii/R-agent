"""插件发现与信任：全局库（装载即信任）＋ 工作区库（默认关、首次需审批）。

来源与信任（§5.1）：

- 全局库 `PLUGINS_DIR`：`*.py`（可编程）/ `*.json`（纯声明片段），**装载即信任**；
- 工作区库 `<工作区>/.my_agent/plugins/`：只有配了 `PLUGINS_WORKSPACE` 才扫；每个文件还要
  出现在该工作区的授权清单（`<工作区>/.my_agent/plugins.json` 的 `trusted`）里才装载——
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
import json
import logging
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.harness.fragment import StructureError, fragment_from_dict
from app.kernel import Context

logger = logging.getLogger(__name__)

CONFIG_DIR = '.my_agent'
PLUGIN_SUBDIR = 'plugins'
TRUST_FILE = 'plugins.json'
SUFFIXES = ('.py', '.json')

SOURCE_GLOBAL = '全局'
SOURCE_WORKSPACE = '工作区'


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
        watch: bool = True,
        interval: float = 1.0,
    ) -> None:
        self.ctx = ctx
        self.global_dir = Path(global_dir) if global_dir else None
        self.workspace = Path(workspace) if workspace else None
        self.watch = watch
        self.interval = interval
        self._loaded: dict[Path, tuple[Any, str]] = {}
        self.last_report = DiscoveryReport()

    # ------------------------------------------------------------------ 对外

    async def start(self) -> DiscoveryReport:
        """首次扫描；`watch=True` 时挂上热重载轮询（归当前 fiber，卸载自动停）。"""
        report = await self.scan()
        if self.watch:
            self.ctx.spawn(self._watch_loop)
        return report

    async def scan(self) -> DiscoveryReport:
        """扫一遍：增/删/改都处理，没变就什么都不做。

        装载失败的文件不进 `_loaded`，所以**每次扫描都会重试**——它等的服务可能下一轮才到
        （比如实现来自同目录的可编程插件）。
        """
        report = DiscoveryReport()
        desired = self._desired(report)

        for path in list(self._loaded):
            if path not in desired:
                await self._unload(path, report)

        for path, (source, fingerprint) in desired.items():
            current = self._loaded.get(path)
            if current is not None and current[1] == fingerprint:
                continue
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
        path = self.workspace / CONFIG_DIR / TRUST_FILE if self.workspace else None
        if path is None:
            return set()
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

    # ------------------------------------------------------------------ 装载

    async def _load(
        self, path: Path, source: str, fingerprint: str, report: DiscoveryReport
    ) -> None:
        try:
            plugin, config = self._compile(path)
        except StructureError as error:
            report.failed.append((path.name, f'{error.code}: {error}'))
            logger.warning('插件未装载 %s（%s）', path.name, error)
            return
        except Exception as error:  # noqa: BLE001 - 一个坏文件不该拖垮发现器
            report.failed.append((path.name, f'{type(error).__name__}: {error}'))
            logger.warning('插件未装载 %s：%s', path.name, error)
            return

        # 先拿 fiber 引用再 await：`await ctx.plugin(...)` 等到的是**插件的返回值**，不是 fiber
        # （卸载要的是 fiber 本身——`fiber.dispose()`）。
        fiber = self.ctx.plugin(plugin, config)
        try:
            await fiber
        except Exception as error:  # noqa: BLE001 - 同上
            report.failed.append((path.name, f'{type(error).__name__}: {error}'))
            logger.warning('插件装载失败 %s：%s', path.name, error)
            return

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
        """读文件 → 插件对象。（`.py` 在这里被执行——所以工作区那份必须先批准。）"""
        if path.suffix == '.json':
            return self._json_plugin(path), None
        return self._python_plugin(path), None

    def _json_plugin(self, path: Path) -> dict:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as error:
            raise StructureError('DECL_JSON', f'{path.name} 不是合法 JSON：{error}') from error
        fragment = fragment_from_dict(payload, source=path.name)
        for node in fragment.nodes:
            if self.ctx.get(node.interface) is None and node.interface != 'stamp':
                raise StructureError(
                    'INTERFACE_MISSING',
                    f'抽象节点 {node.name!r} 的实现 {node.interface!r} 没有对应的服务'
                    f'——先挡在装载口，别让一个片段把整张图合成打回',
                )

        def apply(ctx: Context, config: Any = None, fragment=fragment) -> None:
            ctx.structure.contribute(fragment)

        return {'apply': apply, 'name': f'plugin-file/{path.name}'}

    def _python_plugin(self, path: Path) -> Any:
        """源码直读编译执行：不走 import 缓存，热重载才看得见改动（与内核 loader 同一手法）。"""
        source = path.read_text(encoding='utf-8')
        module = types.ModuleType(f'plugin_file_{path.stem}')
        module.__file__ = str(path)
        exec(compile(source, str(path), 'exec'), module.__dict__)  # noqa: S102 - 可编程插件就是要执行
        plugin = getattr(module, 'plugin', None)
        if plugin is None:
            raise StructureError('DECL_PLUGIN', f'{path.name} 里没有 plugin 对象')
        return plugin

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
