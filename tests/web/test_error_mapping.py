"""全局异常映射：参数不合法走 400（预期内），其余才是 500（故障）。

真跑里被 agent 试接口边界时翻出来的：`POST /plugins/reload {"file":"nope.py"}` 走的是通用
500 出口——一整段 traceback + uvicorn 的 "Exception in ASGI application"，而客户端拿到的
其实是"库里没有这个文件"。手滑被记成故障，真故障就淹了。
"""

from types import SimpleNamespace

import pytest

from app.common.exceptions import InvalidInput, SandboxDenied
from app.web.handler import invalid_input, unhandled_exception

pytestmark = pytest.mark.anyio


class FakeRequest:
    method = 'POST'
    url = SimpleNamespace(path='/plugins/reload')


def test_main_maps_expected_errors_to_400():
    import app.main as main

    handlers = main.app.exception_handlers
    assert handlers.get(InvalidInput) is invalid_input
    assert handlers.get(SandboxDenied) is invalid_input
    assert handlers.get(Exception) is unhandled_exception


async def test_invalid_input_response_shape():
    response = await invalid_input(FakeRequest(), InvalidInput('库里没有这个文件：nope.py'))

    assert response.status_code == 400
    assert b'nope.py' in response.body
