"""插件管理接口（阶段 N）：列清单、启用停用、重读、删除。

三层里的位置：**api 只收参数、转发、把结果交回**——动作与校验全在 `web/service/plugin_admin.py`，
装卸与信任在插件库（`app/plugins/discover.py`）。这一层不认识 fiber，也不碰文件。

管理面**不受沙箱约束**（与 `browse` 一致）：沙箱限制的是 agent，不是用户。但服务层**只接受文件名**
（不接受路径、不接受子目录），越界在那里就被拒。
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.container import get_plugin_admin
from app.plugins.discover import SOURCE_GLOBAL, SOURCE_WORKSPACE
from app.web.result.result import Result

WHERE = {'global': SOURCE_GLOBAL, 'workspace': SOURCE_WORKSPACE}
"""接口用英文枚举（`global` / `workspace`），服务层用插件库那套中文来源名。"""

router = APIRouter(prefix='/plugins', tags=['plugins'])


class PluginFileRequest(BaseModel):
    where: Literal['global', 'workspace'] = Field(
        'workspace', description="global = 全局插件库（装载即信任）；workspace = 当前工作区的库（需批准）"
    )
    workspace: str = Field('', description='工作区路径；where=workspace 时必填')
    file: str = Field(..., min_length=1, description='库里的文件名（.py / .json）')


class PluginEnableRequest(PluginFileRequest):
    enabled: bool = Field(True, description='True 启用（工作区库顺带批准）；False 停用')


OVERVIEW_DOC = """
插件清单：两级库的目录 + 每个插件的 **来源 / 状态 / 原因**。

- 来源：`内置`（装配的一部分，只能列、不能动）、`全局`、`工作区`
- 状态：`ACTIVE` / `PENDING`（缺服务，原因里点名）/ `FAILED`（原因里是原文）/
  `待批准`（工作区库还没批准）/ `已停用`（文件名以 `_` 开头）
- 返回的 `plugins` 与 `inspect_graph` 工具给模型的是**同一份数据**——人和模型看到的一样
- 响应形状与 `/chat/*` 一致：`{code, message, data}`，数据在 `data` 里（`data.plugins`）

生效时机：结构类（片段）在**下一个回合边界**换图；能力类（工具 / 资产 / 服务）装载即生效。
管理面**不绕过**这条规则。
"""

RELOAD_DOC = """
强制重读某个插件文件：内容没变也重来。用途是"我改了却不生效"和"上次装载失败想再试一次"。
返回新清单 + 这次的装载报告。
"""

ENABLE_DOC = """
启用 / 停用（**只改文件名**：停用加 `_` 前缀，启用去掉它，工作区库顺带同步信任清单）。

不发明第三种状态：所以"停用"和"撤信任"是同一件事的两种说法，不会分叉。
对 `待批准` 的文件点"启用" = 批准它（第一次使用的入口）。
"""

REMOVE_DOC = """
删掉库里的文件（**危险动作**，界面负责二次确认）。

只接受文件名，不接受路径。删之前会先撤信任（工作区库），返回消息里写明"怎么回来"。
"""


@router.get('', summary='插件清单（含状态与原因）', description=OVERVIEW_DOC)
async def list_plugins(workspace: str = '') -> Result[dict]:
    return Result.success(data=await get_plugin_admin().overview(workspace))


@router.post('/enable', summary='启用 / 停用一个插件', description=ENABLE_DOC)
async def set_enabled(body: PluginEnableRequest) -> Result[dict]:
    payload = await get_plugin_admin().set_enabled(
        WHERE[body.where], body.workspace, body.file, body.enabled
    )
    return Result.success(data=payload)


@router.post('/reload', summary='强制重读一个插件文件', description=RELOAD_DOC)
async def reload_plugin(body: PluginFileRequest) -> Result[dict]:
    payload = await get_plugin_admin().reload(WHERE[body.where], body.workspace, body.file)
    return Result.success(data=payload)


@router.post('/remove', summary='删除库里的插件文件', description=REMOVE_DOC)
async def remove_plugin(body: PluginFileRequest) -> Result[dict]:
    payload = await get_plugin_admin().remove(WHERE[body.where], body.workspace, body.file)
    return Result.success(data=payload)
