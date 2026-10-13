"""用量解析：命中缓存这个数必须认得出来——它是"大窗口 + 晚压缩"划不划算的唯一判据。

形状是在真端点上量出来的（同一个 12000 字符前缀连发两次）：

| 第几次 | prompt_tokens | prompt_tokens_details.cached_tokens |
| --- | --- | --- |
| 1 | 6833 | 0 |
| 2 | 6833 | **6784** |

两点结论：端点确实复用前缀（否则第二次也该是 0）；但字段是 OpenAI 形状的
`prompt_tokens_details.cached_tokens`，**不是** DeepSeek 文档里的 `prompt_cache_hit_tokens`。
所以解析要两种都认——漏一种，日志里那一列就永远是 0，等于什么都没做。
"""

from langchain_core.messages import AIMessage

from app.agent.usage import cache_read_tokens


def test_normalized_input_token_details_wins():
    """langchain-openai 归一化后的形状优先。"""
    message = AIMessage(
        content='好',
        usage_metadata={
            'input_tokens': 100,
            'output_tokens': 5,
            'total_tokens': 105,
            'input_token_details': {'cache_read': 60},
        },
    )

    assert cache_read_tokens(message) == 60


def test_openai_shaped_raw_usage_is_read():
    """真端点给的就是这一种。"""
    message = AIMessage(
        content='好',
        response_metadata={
            'token_usage': {
                'prompt_tokens': 6833,
                'completion_tokens': 16,
                'prompt_tokens_details': {'cached_tokens': 6784},
            }
        },
    )

    assert cache_read_tokens(message) == 6784


def test_deepseek_native_field_is_read():
    message = AIMessage(
        content='好',
        response_metadata={'token_usage': {'prompt_cache_hit_tokens': 320}},
    )

    assert cache_read_tokens(message) == 320


def test_zero_hit_stays_zero():
    """命中 0 是**有效答案**（第一次调用就是 0），不能因为"看起来像没有"就去找别的字段。"""
    message = AIMessage(
        content='好',
        response_metadata={'token_usage': {'prompt_tokens_details': {'cached_tokens': 0}}},
    )

    assert cache_read_tokens(message) == 0


def test_missing_everywhere_is_zero_not_an_error():
    """端点不报这个字段时恒为 0：宁可少一个数，也不能让一行日志把整轮打挂。"""
    assert cache_read_tokens(AIMessage(content='好')) == 0
    assert cache_read_tokens(None) == 0
