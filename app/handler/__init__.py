"""
全局异常处理。

**非流式接口的兜底出口**：任何没被接口自己处理的异常，都在这里落成一次 500 + `Result`，
进程继续服务下一个请求。流式那半边在 `app/event/stream.py`——事件流已经开始发帧时，
HTTP 状态码早发出去了，只能补一帧 `error`。

错误信息用异常原文，不做翻译：本项目开源、面向程序员，`str(exc)` 比一句客套话有用。
堆栈只进服务端日志，不返回给客户端。
"""

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from app.result.result import Result

logger = logging.getLogger(__name__)


async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
    """注册见 `app/main.py`（`add_exception_handler`），错误形状见 `ErrorResult`。"""
    logger.exception("未处理的异常：%s %s", request.method, request.url.path)
    return Result.fail(message=str(exc) or type(exc).__name__).to_json(500)
