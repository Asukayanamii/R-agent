"""工具输出的密钥遮蔽：`env` / `echo $KEY` 不该把密钥打进对话。

真跑里抓到过：模型一句 `env | grep -i plugin` 把 `LLM_API_KEY` 打进消息 → 网关以 400
拒掉后续请求、整轮对话当场断。`read`/`grep` 对 `.env` 有硬阻断，这条路没有，所以在**出口**兜。
"""

import pytest
from langchain_core.messages import ToolMessage

from app.agent.tool_calls import invoke_call
from app.agent.tools.common import redact_secrets

pytestmark = pytest.mark.anyio

SECRET = 'sk-live-abcdefghijklmnop'


def test_secret_value_is_masked(monkeypatch):
    monkeypatch.setenv('MY_TEST_API_KEY', SECRET)

    out = redact_secrets(f'LLM_API_KEY={SECRET}\n其他内容')

    assert SECRET not in out
    assert '«已隐去 MY_TEST_API_KEY»' in out
    assert '其他内容' in out


def test_unrelated_and_short_values_are_left_alone(monkeypatch):
    monkeypatch.setenv('MY_TEST_MODE', 'production')  # 名字不像密钥
    monkeypatch.setenv('MY_TEST_TOKEN', 'short')  # 太短，不当密钥

    text = 'MODE=production TOKEN=short'

    assert redact_secrets(text) == text


def test_empty_text_passes_through():
    assert redact_secrets('') == ''


class FakeTool:
    def __init__(self, output):
        self._output = output

    async def ainvoke(self, _call):
        return self._output


class FakeRegistry:
    def __init__(self, output):
        self._tool = FakeTool(output)

    def pre_execute(self, call) -> None:
        pass

    def get(self, name):
        return self._tool


async def test_invoke_call_redacts_both_shapes(monkeypatch):
    """两条返回路径都要遮蔽：`@tool` 自己造的 ToolMessage、以及工具返回的普通值。"""
    monkeypatch.setenv('MY_TEST_API_KEY', SECRET)
    call = {'id': 'c1', 'name': 'bash', 'args': {}}

    message = await invoke_call(call, {}, {}, FakeRegistry(f'KEY={SECRET}'))
    assert SECRET not in message.content

    wrapped = await invoke_call(
        call, {}, {}, FakeRegistry(ToolMessage(content=f'KEY={SECRET}', tool_call_id='c1'))
    )
    assert SECRET not in wrapped.content
