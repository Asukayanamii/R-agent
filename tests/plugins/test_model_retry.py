"""builtin/model 与 builtin/retry（阶段 K1）：服务、配置，以及那个既有的回调管理器缺陷。

缺陷回顾（设计稿 §1.3）：`_config_with_spy` 把**空 handlers 的 `AsyncCallbackManager`**
当成 handler 塞进回调列表——回调系统会去调它（`run_inline` 不存在），`astream`/`ainvoke`
路径下直接 AttributeError。产品路径走 `astream_events`（handlers 非空）所以一直没暴露。
"""

import pytest
from langchain_core.callbacks import AsyncCallbackManager, BaseCallbackHandler
from langchain_core.messages import AIMessage

from app.common.exceptions import ModelUnavailable
from app.kernel import Context
from app.plugins.builtin import model as model_plugin
from app.plugins.builtin import retry as retry_plugin

pytestmark = pytest.mark.anyio


class FakeModel:
    """只记下 ainvoke 收到的 config（retry 层只调这一个方法）。"""

    def __init__(self) -> None:
        self.calls = 0
        self.callbacks: list[list] = []

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls += 1
        self.callbacks.append(list((config or {}).get('callbacks') or []))
        return AIMessage(content='ok')


class FailingModel:
    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages, config=None, **kwargs):
        self.calls += 1
        raise self.error


# ------------------------------------------------------------------ 回调管理器缺陷


async def test_empty_manager_is_not_treated_as_a_handler():
    """空 handlers 的管理器不能被当 handler 塞进去（塞了就是 AttributeError）。"""
    manager = AsyncCallbackManager(handlers=[])
    model = FakeModel()

    await retry_plugin.ainvoke_with_retry(model, [], config={'callbacks': manager})

    assert model.calls == 1
    assert manager not in model.callbacks[0]
    assert len(model.callbacks[0]) == 1  # 只剩探针


async def test_manager_handlers_are_forwarded():
    """管理器的 handlers 仍要转发——它们才是真正收回调的那批。"""
    handler = BaseCallbackHandler()
    manager = AsyncCallbackManager(handlers=[handler])
    model = FakeModel()

    await retry_plugin.ainvoke_with_retry(model, [], config={'callbacks': manager})

    assert handler in model.callbacks[0]


async def test_single_handler_still_passes_through():
    """真·单个 handler（没有 handlers 属性）照旧带上——别把这一支一起修坏。"""
    handler = BaseCallbackHandler()
    model = FakeModel()

    await retry_plugin.ainvoke_with_retry(model, [], config={'callbacks': handler})

    assert handler in model.callbacks[0]


# ------------------------------------------------------------------ 两个插件


async def test_model_plugin_provides_both_services():
    ctx = Context()
    await ctx.plugin(model_plugin.plugin, {'model': 'm', 'base_url': 'u', 'api_key': ''})

    assert ctx.get('model') is not None
    assert ctx.get('summary_model') is not None
    with pytest.raises(ModelUnavailable):  # 没 key → 占位模型，一调就报错
        await ctx.get('model').ainvoke([])


async def test_retry_plugin_settings_drive_the_budget(monkeypatch):
    """覆盖配置能改掉重试预算：0 次 = 一次就抛（不重试）。"""
    monkeypatch.setattr(retry_plugin, '_sleep', _no_wait)
    ctx = Context()
    await ctx.plugin(retry_plugin.plugin, {'max_retries': 0, 'base_delay': 0.01, 'max_delay': 1})
    model = FailingModel(Exception('rate limit exceeded'))  # 可重试类错误

    with pytest.raises(Exception, match='rate limit'):
        await ctx.get('retry').ainvoke(model, [], thread_id='t')

    assert model.calls == 1


async def _no_wait(seconds: float) -> None:
    return None
