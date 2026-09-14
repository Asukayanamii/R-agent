"""
自定义异常。

两个都继承 `ValueError`：它们表达的都是"传进来的这个值不可接受"，语义上本就是
ValueError；同时让调用方既能精确捕获具体类型，也能用 `except ValueError` 兜底。

代码直接放在包的 `__init__.py` 里，而不是像 `app/result/result.py` 那样再套一层同名模块——
套了就成了 `from app.exceptions.exceptions import ...`，双重叠词。异常类只有几个，
包本身就是它的模块。
"""


class SandboxDenied(ValueError):
    """
    路径被沙箱拒绝。

    工作区之外的路径需要用户授权，绝对禁区则直接拒绝、不询问。
    实现见 `app.agent.sandbox`。
    """


class InvalidInput(ValueError):
    """
    调用方给的参数不合法：路径不存在、不是目录、没有读取权限等。

    业务层抛出，表现层捕获后转成 `Result.fail`。
    """
