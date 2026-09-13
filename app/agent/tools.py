"""工具注册表。新增工具只需定义函数并加入 TOOLS。"""

from datetime import datetime

from langchain_core.tools import tool


@tool
def get_current_time() -> str:
    """获取当前的日期和时间，当用户询问现在几点、今天几号时使用。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@tool
def count_text(text: str) -> str:
    """统计一段文本的字符数与行数，当用户需要数字数、统计文本长度时使用。"""
    return f"字符数 {len(text)}，行数 {len(text.splitlines()) or 1}"


@tool
def purge_cache(scope: str) -> str:
    """清空指定范围内的缓存，scope 为环境名，例如 demo、staging。"""
    return f"已清空「{scope}」范围内的缓存"


TOOLS = [get_current_time, count_text, purge_cache]

APPROVAL_REQUIRED = {"purge_cache"}
"""需要人工确认后才允许执行的工具名。确认流程见 langgraph_runner 的 approve 节点。"""
