"""插件管理：把插件库的能力收成一组**面向用户的动作**（阶段 N）。

架构上的一句话：**内核拥有生命周期，插件库拥有文件与信任，这一层只把两者翻译成用户动作**。
它不维护第二份状态：每个动作都落到已有原语上（`trust` / `untrust` / `reload` / 改文件名 / 删文件），
做完把**新的清单**交回去——界面渲染一次请求就够，不用自己拼"现在到底是什么状态"。

三条写死在这里的取舍：

1. **内置插件不在管理范围**：它们是"装配的一部分"（容器装载，缺了启动就失败）。要改它们的组合
   = 改装配清单；这里不假装能统一管理它们（清单里照旧列出来、带 `source: 内置`，只是没有动作）。
2. **"停用"只有一种**：文件名加 `_` 前缀（插件库本来就不扫这种文件）。启用 = 去掉前缀，
   工作区库顺带同步信任清单（停用移出、启用写入）。**不发明第三种状态**——否则"停用"与"撤信任"
   会分叉，最后没人说得清哪个算数。批准（`待批准` → 启用）是另一件事：那是"第一次用"。
3. **结构变更仍按回合边界生效**：管理面**不绕过**它。agent 改的要等一轮、你点的立刻生效，
   两套规则比一套慢规则更难解释。能力类（工具 / 资产）本来就是装载即生效。

危险动作只有一个：`remove()`（删文件）。它不做二次确认（那是界面的活），但**只接受库里的文件名**
（不接受路径、不接受子目录），并把"怎么回来"写进返回消息。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.common.exceptions import InvalidInput
from app.plugins.discover import SOURCE_GLOBAL, SOURCE_WORKSPACE, SUFFIXES

logger = logging.getLogger(__name__)

WHERE_GLOBAL = SOURCE_GLOBAL
WHERE_WORKSPACE = SOURCE_WORKSPACE


class PluginAdmin:
    """服务 ``plugin_admin``：列出 / 启用停用 / 重读 / 删除（都只动插件库里的文件）。

    `libraries` 是 `plugins` 服务（`PluginLibraries`）；`PLUGINS_ENABLED=0` 时它是 None——
    那时只有内置插件，管理面对外表现成"未启用"，而不是报错。
    """

    def __init__(self, libraries: Any) -> None:
        self._libraries = libraries

    @property
    def available(self) -> bool:
        return self._libraries is not None

    # ---------------------------------------------------------------- 读

    async def overview(self, workspace: str = '') -> dict[str, Any]:
        """清单 + 两级库目录：界面一次请求拿全（含每行的状态与原因）。

        **先 ensure 那个工作区库**：库是按会话工作区"按需登记"的，没登记就看不到它的文件——
        面板上会假装"这里什么都没有"（真跑里点开面板只见内置行的原因）。打开面板本身就是
        "我要看这个工作区的库"，登记它是这一层的责任。
        """
        if not self.available:
            return {'enabled': False, 'libraries': {}, 'plugins': []}
        target = workspace.strip() or None
        if target:
            try:
                await self._libraries.ensure(target)
            except Exception as error:  # noqa: BLE001 - 登记失败不该让清单整个打不开
                logger.warning('工作区插件库登记失败 %s：%s', target, error)
        return {
            'enabled': True,
            'libraries': self._libraries.library_dirs(target),
            'plugins': self._libraries.inventory(target),
        }

    # ---------------------------------------------------------------- 写

    async def set_enabled(
        self, where: str, workspace: str, filename: str, enabled: bool
    ) -> dict[str, Any]:
        """启用 / 停用：改文件名（`_` 前缀），工作区库顺带同步信任清单。"""
        path = self._existing(where, workspace, filename)
        scope = workspace.strip()

        if not enabled and where == WHERE_WORKSPACE and scope:
            # 先撤信任再改名：只改名而清单里还留着，会变成"启用时静默生效"的暗门
            await self._libraries.untrust(scope, path.name)
        changed = self._rename(path, enabled)
        # 每次动作后都扫一次：改名/删除发生在扫描之后，不扫的话清单里会留着旧状态
        # （"文件都删了还显示待批准"就是这么来的）
        if where == WHERE_WORKSPACE and scope:
            if enabled:
                # 新写入信任也是"改动了"——不然批准一个待批准的文件会回"本来就是这个状态"（说谎）
                changed = await self._libraries.trust(scope, _stripped(path.name)) or changed
            else:
                await self._libraries.rescan(scope)
        else:
            await self._libraries.rescan_global()

        action = '启用' if enabled else '停用'
        note = '' if changed else '（本来就是这个状态，没改动）'
        return self._result(await self.overview(workspace), f'{action} {filename}：完成{note}')

    async def reload(self, where: str, workspace: str, filename: str) -> dict[str, Any]:
        """强制重读：改了不生效、或上次装载失败想再试一次时用。"""
        path = self._existing(where, workspace, filename)
        scope = workspace.strip() if where == WHERE_WORKSPACE else None
        report = await self._libraries.reload(scope, path.name)
        detail = report.render()
        return self._result(await self.overview(workspace), f'已重读 {filename}：{detail}')

    async def remove(self, where: str, workspace: str, filename: str) -> dict[str, Any]:
        """删掉库里的文件（危险动作，界面负责二次确认）。

        删之前先撤信任（工作区库）：只删文件而清单里留着条目，会让这一行在"待批准 / 失败"
        之间来回跳，最难查。
        """
        path = self._existing(where, workspace, filename)
        scope = workspace.strip()
        if where == WHERE_WORKSPACE and scope:
            await self._libraries.untrust(scope, path.name)
        path.unlink()
        logger.info('插件管理：删除 %s', path)
        # 删完再扫一次：不然清单里还留着这一行（"文件没了却显示待批准"）
        if where == WHERE_WORKSPACE and scope:
            await self._libraries.rescan(scope)
        else:
            await self._libraries.rescan_global()
        return self._result(
            await self.overview(workspace),
            f'已删除 {filename}。想回来就再放一个同名文件（工作区库还要重新批准一次）。',
        )

    # ---------------------------------------------------------------- 内部

    def _result(self, overview: dict[str, Any], message: str) -> dict[str, Any]:
        """动作的返回值 = 新清单（调用方已经准备好了）+ 一句人话。"""
        payload = dict(overview)
        payload['message'] = message
        return payload

    def _library_dir(self, where: str, workspace: str) -> Path:
        directories = self._libraries.library_dirs(workspace.strip() or None)
        raw = directories.get(where)
        if not raw:
            raise InvalidInput(
                f'这个库现在不可用：{where}（工作区库需要 PLUGINS_WORKSPACE=1 且会话绑了工作区）'
            )
        return Path(raw)

    def _existing(self, where: str, workspace: str, filename: str) -> Path:
        """校验文件名并确认它就在库里（只收文件名——路径、子目录一律拒绝）。"""
        name = filename.strip()
        if not name or name != Path(name).name or Path(name).suffix not in SUFFIXES:
            raise InvalidInput(f'文件名不合法：{filename!r}（只能是库里的 .py / .json 文件名）')
        path = self._library_dir(where, workspace) / name
        if not path.is_file():
            raise InvalidInput(f'库里没有这个文件：{name}')
        return path

    def _rename(self, path: Path, enabled: bool) -> bool:
        """停用 = 加 `_` 前缀；启用 = 去掉。返回是否真的改过。"""
        name = path.name
        if enabled == (not name.startswith('_')):
            return False
        target = path.with_name(_stripped(name) if enabled else f'_{name}')
        if target.exists():
            raise InvalidInput(f'同名文件已存在，先处理它：{target.name}')
        path.rename(target)
        logger.info('插件管理：%s → %s', name, target.name)
        return True


def _stripped(name: str) -> str:
    """去掉停用前缀（`_panel.py` → `panel.py`）。"""
    return name.lstrip('_') or name
