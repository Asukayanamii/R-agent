"""插件配置（设计稿 §5.6）：配置项由插件自己声明与维护，默认值引用总体配置。

三条要钉住的行为：

1. `default_config()` 跟着总体配置（.env）走——"改一处"的习惯照旧；
2. 装配时传入的覆盖 dict 能改掉行为（压缩旋钮、上下文开关各一条）；
3. `Config` 校验不过 = 拒绝装载（不是静默退回默认值）。
"""

import itertools

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent.compaction.policy import CompactionSettings
from app.agent.compaction.runtime import compact_once
from app.kernel import Context, CordisError, ValidationError
from app.plugins.builtin import compaction as compaction_plugin
from app.plugins.builtin import context as context_plugin

pytestmark = pytest.mark.anyio


def make_summary_model(text: str = '摘要'):
    class Stub(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return 'stub'

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            raise NotImplementedError

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    return Stub()


# ------------------------------------------------------------------ 默认值来源


def test_default_config_reads_the_overall_config(monkeypatch):
    """插件的默认配置引用总体配置——所以改 .env 还是"改一处"。"""
    from app import config

    monkeypatch.setattr(config, 'COMPACT_AT', 0.5)
    monkeypatch.setattr(config, 'LLM_CONTEXT_WINDOW', 1234)
    monkeypatch.setattr(config, 'COMPACT_KEEP_TOKENS', 99)
    monkeypatch.setattr(config, 'COMPACT_ENABLED', False)

    assert compaction_plugin.default_config() == {
        'enabled': False,
        'trigger_ratio': 0.5,
        'keep_tokens': 99,
        'context_window': 1234,
    }


def test_context_default_config_reads_the_overall_config(monkeypatch):
    from app import config

    monkeypatch.setattr(config, 'SKILLS_ENABLED', False)
    monkeypatch.setattr(config, 'SKILLS_DIR', 'X:/skills')
    monkeypatch.setattr(config, 'AGENTS_MD_ENABLED', False)
    monkeypatch.setattr(config, 'AGENTS_ANCESTORS', False)

    assert context_plugin.default_config() == {
        'skills_enabled': False,
        'skills_dir': 'X:/skills',
        'agents_md_enabled': False,
        'ancestors': False,
    }


# ------------------------------------------------------------------ 覆盖 dict 改行为


async def test_compaction_plugin_config_drives_the_threshold():
    """覆盖压缩插件的配置：同一个会话越过新阈值就压，默认阈值下不压。"""
    ctx = Context()
    ctx.provide('summary_model', make_summary_model())
    await ctx.plugin(compaction_plugin.plugin, {
        'enabled': True,
        'trigger_ratio': 0.5,
        'keep_tokens': 20,
        'context_window': 200,
    })
    settings = ctx.get('compaction').settings
    assert isinstance(settings, CompactionSettings)
    assert settings.trigger_tokens == 100

    state = {
        'messages': [
            HumanMessage('任务'),
            HumanMessage('中段' * 200),
            HumanMessage('中段' * 200),
            HumanMessage('最近' * 200),
        ]
    }
    update = await compact_once(
        state, summary_model=make_summary_model(), thread_id='c1', settings=settings
    )
    assert update and update['compaction']['summary'] == '摘要'

    # 默认设置（64k 窗口）下同一份状态远未到线——对照组
    default_update = await compact_once(
        state, summary_model=make_summary_model(), thread_id='c2',
        settings=CompactionSettings(),
    )
    assert default_update == {}


async def test_context_plugin_config_disables_injection(tmp_path):
    workspace = tmp_path / 'ws'
    workspace.mkdir()
    (workspace / 'AGENTS.md').write_text('# 约定：用中文回答', encoding='utf-8')

    off = Context()
    await off.plugin(context_plugin.plugin, {
        'skills_enabled': False,
        'skills_dir': '',
        'agents_md_enabled': False,
        'ancestors': False,
    })
    assert off.get('context').static(str(workspace)) == ''

    on = Context()
    await on.plugin(context_plugin.plugin, {
        'skills_enabled': False,
        'skills_dir': '',
        'agents_md_enabled': True,
        'ancestors': False,
    })
    block = on.get('context').static(str(workspace))
    assert '约定：用中文回答' in block


async def test_context_ancestors_switch_is_respected(tmp_path):
    """ancestors=False 只读工作区根：父目录那份约定不进来。"""
    parent = tmp_path / 'parent'
    workspace = parent / 'ws'
    workspace.mkdir(parents=True)
    (parent / 'AGENTS.md').write_text('父目录约定', encoding='utf-8')
    (workspace / 'AGENTS.md').write_text('工作区约定', encoding='utf-8')

    ctx = Context()
    await ctx.plugin(context_plugin.plugin, {
        'skills_enabled': False,
        'skills_dir': '',
        'agents_md_enabled': True,
        'ancestors': False,
    })
    block = ctx.get('context').static(str(workspace))
    assert '工作区约定' in block
    assert '父目录约定' not in block


# ------------------------------------------------------------------ 校验不过 = 拒绝装载


async def test_invalid_config_refuses_to_load():
    ctx = Context()
    ctx.provide('summary_model', make_summary_model())

    fiber = ctx.plugin(compaction_plugin.plugin, {'context_window': '不是数字'})
    with pytest.raises(ValidationError):
        await fiber

    assert ctx.get('compaction') is None  # 没有半装载的服务留下
