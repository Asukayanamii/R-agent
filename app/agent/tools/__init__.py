"""
工具注册表。新增工具只需在本文件里 import 并加入 TOOLS。

一个工具一个文件，因为工具往往不只是个函数——像 bash 还要带 shell 发现、
输出清洗与截断、进程树终止。混在注册表里会把这里撑成杂物间。
"""

from app.agent.tools.bash import bash

TOOLS = [bash]

APPROVAL_REQUIRED: set[str] = set()
"""
需要人工确认后才允许执行的工具名。

目前为空——把工具名加进来即可启用确认流程，例如 APPROVAL_REQUIRED = {"bash"}。
启用后每次调用都要在界面上点确认，编码场景下通常会很烦，所以默认关闭。
确认机制见 langgraph_runner 的 _run_tools。
"""
