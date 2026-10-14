"""
全局异常处理。

**非流式接口的兜底出口**：任何没被接口自己处理的异常，都在这里落成一次 500 + `Result`，
进程继续服务下一个请求。流式那半边在 `app/plugins/builtin/protocol/`（事件流已经开始发帧时，
HTTP 状态码早发出去了，只能补一帧 `error`）。

错误信息用异常原文，不做翻译：本项目开源、面向程序员，`str(exc)` 比一句客套话有用。
堆栈只进服务端日志，不返回给客户端。
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.web.result.result import Result

logger = logging.getLogger(__name__)


async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """注册见 `app/main.py`（`add_exception_handler`），错误形状见 `ErrorResult`。"""
    logger.exception("未处理的异常：%s %s", request.method, request.url.path)
    return Result.fail(message=str(exc) or type(exc).__name__).to_json(500)


async def invalid_input(request: Request, exc: Exception) -> JSONResponse:
    """参数不合法（`InvalidInput` / `SandboxDenied`）：**预期内**的错误，不是故障。

    返回 400 + 同样的 `Result` 形状，日志一行 INFO——**不打 traceback**。
    这些错误在"用户输入直接决定路径"的接口上很常见（`browse` / `skills` / 插件管理），
    以前它们走 `unhandled_exception`：每次手滑都一整段堆栈 + uvicorn 的
    "Exception in ASGI application"，把真故障淹在噪音里（真跑里是被 agent 试接口边界时翻出来的）。
    """
    logger.info("输入不合法：%s %s —— %s", request.method, request.url.path, exc)
    return Result.fail(message=str(exc) or type(exc).__name__).to_json(400)
