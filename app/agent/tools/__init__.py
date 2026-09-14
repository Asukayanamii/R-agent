"""
工具注册表。新增工具只需在本文件里 import 并加入 TOOLS。

一个工具一个文件：工具往往不只是一个函数——像 bash 还要带 shell 发现、
流式读取、进程树终止；edit 还要带宽松匹配。混进注册表会把这里撑成杂物间。

路径解析（含越界防护）与输出截断是共用的，放在 common.py。

约定：**返回值表示"做成了"**（包括"没有匹配"这类空结果），**"没做成"用 `common.fail()` 抛**
——那句话模型照旧收到，同时会带上 `status=error`，前端把卡片标成"失败"、历史恢复也还是"失败"。
"""

from app.agent.tools.bash import bash
from app.agent.tools.edit import edit
from app.agent.tools.find import find
from app.agent.tools.get_current_time import get_current_time
from app.agent.tools.grep import grep
from app.agent.tools.ls import ls
from app.agent.tools.read import read
from app.agent.tools.write import write

TOOLS = [read, write, edit, ls, find, grep, bash, get_current_time]

APPROVAL_REQUIRED: set[str] = set()
"""
需要人工确认后才允许执行的工具名。

目前为空——把工具名加进来即可启用确认流程，例如 {"write", "edit", "bash"}。
启用后每次调用都要在界面上点确认，编码场景下通常会很烦，所以默认关闭。
确认机制见 langgraph_runner 的 _run_tools。
"""
