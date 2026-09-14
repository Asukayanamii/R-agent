"""
自定义异常。

`SandboxDenied` 与 `InvalidInput` 都继承 `ValueError`：它们表达的都是"传进来的这个值不可接受"，
语义上本就是 ValueError；同时让调用方既能精确捕获具体类型，也能用 `except ValueError` 兜底。
`ModelUnavailable` 不同——那是"环境没配好"，跟传参无关，所以继承 `RuntimeError`。

错误信息一律**原文呈现**，不做面向用户的翻译：本项目开源、面向程序员，
`str(exc)` 比一句客套话有用。所以这里也不提供"把异常翻成人话"的函数。

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


class ModelUnavailable(RuntimeError):
    """
    没有可用的模型：没配 `LLM_API_KEY`。

    `langgraph_runner` 在没配 key 时会拿一个占位模型顶上，一调用就抛它；由 SSE 层收成
    `error` 事件（见 `app.event.stream`）。**不降级成桩实现**：复读一条像模像样的回复，
    比直接报错更糟——用户会以为模型在回话。
    """
