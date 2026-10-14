"""
用量解析：从 provider 响应里取出"这次输入有多少命中了前缀缓存"。

单独一块是因为**它决定了一套配置划不划算**：窗口填得大、压缩阈值跟着高（比如 1M 窗口配
80 万阈值），前提是端点会复用前缀缓存——命中时稳定前缀不必每轮重算，每轮只预填充新增的
那一小段；不命中就是每轮把整个历史重预填充一遍，那才是"任务越往后单轮越慢"的根因。

这个信号只有 provider 给，而 langchain 把它放在两处、名字还不一样（归一化后的形状与原文），
所以取值规则集中在这里，图节点（`nodes.py`）与协议映射（`mapping.py`）共用一份。

**取不到返回 0，不猜**：0 与"端点根本不报缓存"在数值上无法区分，所以日志里同时打输入总量
与消息条数，看的人自己判断是不是端点不给这个字段。
"""

from typing import Any


def _int(value: Any) -> int:
    """能转成 int 就转，否则 0——provider 偶发给 null 或字符串。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def cache_read_tokens(response: Any) -> int:
    """
    这次调用里有多少输入 tokens 命中了 provider 的前缀缓存。取不到返回 0。

    两种来源都认，先归一化的、再原文：

    - `usage_metadata["input_token_details"]["cache_read"]`——langchain-openai 归一化后的
      （OpenAI 形状）；
    - `response_metadata["token_usage"]` 的原文——DeepSeek 的 `prompt_cache_hit_tokens`、
      OpenAI 的 `prompt_tokens_details.cached_tokens`、Anthropic 形状的
      `cache_read_input_tokens`。兼容端点的字段名各家不同，这里一次认全，免得换个端点就静默失明。
    """
    meta = getattr(response, "usage_metadata", None) or {}
    details = meta.get("input_token_details") or {}
    hit = _int(details.get("cache_read"))
    if hit:
        return hit

    raw = (getattr(response, "response_metadata", None) or {}).get("token_usage") or {}
    return _int(
        raw.get("prompt_cache_hit_tokens")
        or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
        or raw.get("cache_read_input_tokens")
    )
