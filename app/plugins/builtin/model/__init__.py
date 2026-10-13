"""builtin/model：模型适配器——服务 ``model`` 与 ``summary_model``（provides ``model:v1``）。

**可替换、不可停用**：停用等于没有对话能力，所以进必需清单。没配 `LLM_API_KEY` 时它提供
`UnavailableModel`（一调就报错）：历史、待确认项、删除这些**读路径**都存在检查点里，跟模型
无关，换个空 runner 会把它们一起弄丢——打开旧会话一片空白。

**它的旋钮就是它的配置**（`Config`）：模型名 / 端点 / key / SDK 重试 / 停顿看门狗 / 摘要模型 /
上下文窗口；默认值引用总体配置（`.env` 的 `LLM_*`）——改一处仍然够用（§5.6）。

摘要是**另一个实例**（要带输出上限，见 `SummaryModel`），所以这个插件注册两个服务名。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult
from langchain_openai import ChatOpenAI

from app.agent.compaction.policy import summary_max_tokens
from app.common.exceptions import ModelUnavailable
from app.kernel import Schema

PLUGIN_ID = 'builtin/model'

NO_MODEL_REASON = (
    "未配置 LLM_API_KEY，无法对话。"
    "在项目根目录的 .env 里填上 key（可参考 .env.example），重启应用后即可使用。"
)


class UnavailableModel(BaseChatModel):
    """
    没配 `LLM_API_KEY` 时的占位模型：一调用就抛，不假装能回答。

    为什么只换模型、不换整个 runner：历史、待确认项、删除这些**读路径**都存在检查点里，
    跟模型没关系。换个空实现的 runner 会把它们一起弄丢——打开旧会话一片空白。
    """

    @property
    def _llm_type(self) -> str:
        return "unavailable"

    def bind_tools(self, tools: object, **kwargs: object) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> ChatResult:
        raise ModelUnavailable(NO_MODEL_REASON)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: object | None = None,
        **kwargs: object,
    ) -> ChatResult:
        raise ModelUnavailable(NO_MODEL_REASON)


class SummaryModel(ChatOpenAI):
    """
    摘要模型：把输出上限用 `max_tokens` 发出去。

    摘要的长度必须由外部卡住（接口参数），不能只写进提示词里——模型可以不理，
    而且那句"别太长"本身也占注意力。

    这版 langchain 会在 `ChatOpenAI._get_request_payload` 里把 `max_tokens` 改名成
    `max_completion_tokens`（跟着 OpenAI 的新命名走）。本项目面向的是**任意 OpenAI 兼容
    端点**，DeepSeek 这类只认 `max_tokens`：改名等于上限没设上，老端点还可能直接 400。
    所以这里把名字改回兼容的那一个（实测见 LESSONS）。
    """

    def _get_request_payload(
        self, input_: object, *, stop: list[str] | None = None, **kwargs: object
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload.pop("max_completion_tokens")
        return payload


@dataclass(frozen=True)
class ModelSettings:
    """模型的全部旋钮（由插件配置给出）。默认值 = 改造前行为。"""

    model: str = ''
    base_url: str = ''
    api_key: str = ''
    max_retries: int = 0
    stream_chunk_timeout: int = 300
    """流式停顿看门狗（秒）。**0 = 关掉**（交给内核级 TCP 超时兜底），与 .env 里的写法一致。"""
    summary_model: str = ''
    context_window: int = 64_000

    @classmethod
    def from_mapping(cls, mapping: dict | None) -> 'ModelSettings':
        options = dict(mapping or {})
        return cls(
            model=str(options.get('model') or ''),
            base_url=str(options.get('base_url') or ''),
            api_key=str(options.get('api_key') or ''),
            max_retries=int(options.get('max_retries', 0)),
            stream_chunk_timeout=int(options.get('stream_chunk_timeout', 300)),
            summary_model=str(options.get('summary_model') or ''),
            context_window=int(options.get('context_window', 64_000)),
        )

    @classmethod
    def from_app_config(cls) -> 'ModelSettings':
        """引用总体配置（.env）——默认值的来源。"""
        from app.common import config

        return cls(
            model=config.LLM_MODEL,
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY,
            max_retries=0,
            stream_chunk_timeout=config.LLM_STREAM_CHUNK_TIMEOUT or 0,
            summary_model=config.COMPACT_MODEL,
            context_window=config.LLM_CONTEXT_WINDOW,
        )


def build_model(
    settings: ModelSettings | None = None,
    *,
    name: str | None = None,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """没配 key 就用占位模型：读路径照常，只有真去调模型时才报错。

    `max_tokens` 只有摘要模型用：卡住摘要长度，别让一次压缩写回比原文还长的东西。
    """
    settings = settings or ModelSettings.from_app_config()
    if not settings.api_key:
        return UnavailableModel()
    model_class = SummaryModel if max_tokens else ChatOpenAI
    return model_class(
        model=name or settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        temperature=0,
        max_tokens=max_tokens,
        streaming=True,  # 关掉流式就没有 text_delta，前端只能等整段回复
        # SDK 自带的重试关掉：它不记日志、延迟不可控，还会和我们那层叠加次数
        # （3 × 2 次）。重试统一由 builtin/retry 负责，理由见那里。
        max_retries=settings.max_retries,
        # 流式停顿看门狗：SDK 默认 120s 太紧，网关一次静默停顿就会让这一路调用直接失败
        # （见 .env 的 LLM_STREAM_CHUNK_TIMEOUT）。0 = 关掉。
        stream_chunk_timeout=settings.stream_chunk_timeout or None,
    )


def build_summary_model(settings: ModelSettings | None = None) -> BaseChatModel:
    """摘要模型：`summary_model` 留空就是主模型，只是换个实例、带上输出上限。"""
    settings = settings or ModelSettings.from_app_config()
    return build_model(
        settings,
        name=settings.summary_model or None,
        max_tokens=summary_max_tokens(settings.context_window),
    )


class Config(Schema):
    model = Schema.string().default('').description('模型名，如 deepseek-chat')
    base_url = Schema.string().default('').description('OpenAI 兼容端点')
    api_key = Schema.string().default('').description('留空 = 提供"一调就报错"的占位模型')
    max_retries = Schema.integer().default(0).description('SDK 自带重试次数；关掉，重试归 builtin/retry')
    stream_chunk_timeout = Schema.integer().default(300).description('流式停顿看门狗（秒）；0 = 关掉')
    summary_model = Schema.string().default('').description('摘要用哪个模型；留空跟随主模型')
    context_window = Schema.integer().default(64_000).description('上下文窗口（决定摘要输出上限）')


def default_config() -> dict:
    """插件默认配置：引用总体配置（`.env`）。"""
    settings = ModelSettings.from_app_config()
    return {
        'model': settings.model,
        'base_url': settings.base_url,
        'api_key': settings.api_key,
        'max_retries': settings.max_retries,
        'stream_chunk_timeout': settings.stream_chunk_timeout,
        'summary_model': settings.summary_model,
        'context_window': settings.context_window,
    }


def apply(ctx: Any, config: Any = None) -> None:
    """两个服务名直接给出**模型对象本身**：`ctx.model` 要能 `bind_tools`，包一层服务就没法用了。

    （`ctx.provide` 的撤销挂在调用方 fiber 上——插件卸载时服务自动注销，与 Service 基类同一条路。）
    """
    settings = ModelSettings.from_mapping(config)
    ctx.provide('model', build_model(settings))
    ctx.provide('summary_model', build_summary_model(settings))
    if settings.api_key:
        ctx.logger.info(
            '模型已就绪：%s @ %s（摘要模型 %s）',
            settings.model,
            settings.base_url,
            settings.summary_model or f'跟随主模型（{settings.model}）',
        )
    else:
        ctx.logger.warn(
            '未配置 LLM_API_KEY：模型是"一调就报错"的占位实现（历史与列表照常可读）'
        )


MODEL_CONTRACT = 'model:v1'
"""对外契约名：换一份模型实现 = 换一个提供同名契约的插件。"""


plugin = {
    'apply': apply,
    'name': PLUGIN_ID,
    'provides': MODEL_CONTRACT,
    'Config': Config,
}
