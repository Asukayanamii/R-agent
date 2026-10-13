"""
环境配置。换模型服务只改 .env，不动代码。

代码直接放在包的 `__init__.py` 里，和 `app/common/exceptions/` 一致：对外只有一个
`from app.common.config import X` 的入口，不再套一层 `config/config.py` 的同名模块。

**`PROJECT_ROOT` 是按目录层级数出来的，挪文件必须同步改。**
这个文件在 `app/common/config/` 下，所以是往上第四层。算错不会报错，只会让相对路径的
配置静默写到别处去——曾经因此凭空多出一个 `app/data/checkpoints.db`，
用户那边表现为"会话丢了"。下面的断言让这类错误在启动时立刻暴露。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]

if not (PROJECT_ROOT / "requirements.txt").is_file():
    raise RuntimeError(f"PROJECT_ROOT 算错了，指向 {PROJECT_ROOT}")

CONFIG_DIR = ".my_agent"
"""应用在工作区/用户目录下用的隐藏目录名。

沙箱授权（`sandbox.json`）、技能库（`skills/`）、项目约定（`AGENTS.md`）都住在这个名字下，
所以它只能有一处定义——写两处迟早会分叉，而分叉的表现是"某个功能静默看不到文件"。
"""

# 显式指定 .env 位置，不用 load_dotenv() 的上溯查找：
# 上溯是按调用方文件位置找的，行为和路径解析不一致，容易踩坑。
load_dotenv(PROJECT_ROOT / ".env")

LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat").strip()

# 显式启用桩实现（复读 + 演练工具卡片 / 人工确认），用于没有模型时调前端交互。
# 它**不是**兜底：没配 key 时对话接口直接报错，见 app/agent/langgraph_runner.py 的 _UnavailableModel。
STUB_ENABLED = os.getenv("AGENT_STUB", "").strip() == "1"

# 日志级别。约定见 README「日志」：ERROR 是需要人处理的失败，WARNING 是降级与拒绝，
# INFO 是主线里程碑（每轮对话、工具调用、迁移），DEBUG 是每次写库这类细节。
# 排查问题时置 DEBUG 重启即可；级别名写错时退回 INFO。
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

CONFIG_WARNINGS: list[str] = []
"""取值非法的配置项记录（谁退回了默认值、原值是什么），启动时由组合根打出来。

静默退回默认是"改坏了不报错"的那类：窗口填成 "64k" 会被当成默认值用，
行为跟预期不同却没有任何提示。
"""


def _int_or(raw: str, default: int) -> int:
    """数值配置：写错、写非正数一律退回默认，并把这件事记下来。"""
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        CONFIG_WARNINGS.append(f"配置值不是整数，已退回默认：{raw!r} → {default}")
        return default
    if value <= 0:
        CONFIG_WARNINGS.append(f"配置值必须为正，已退回默认：{value} → {default}")
        return default
    return value


def _ratio_or(raw: str, default: float) -> float:
    """比例配置：只接受 (0, 1]；其余退回默认。"""
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        CONFIG_WARNINGS.append(f"配置值不是小数，已退回默认：{raw!r} → {default}")
        return default
    if not 0 < value <= 1:
        CONFIG_WARNINGS.append(f"配置值必须落在 (0, 1]，已退回默认：{value} → {default}")
        return default
    return value


# ---- 流式调用的停顿看门狗 ----
# langchain-openai 默认 120s：连续这么久收不到新分片就抛 StreamChunkTimeoutError。
# 实测网关会静默停顿超过 120s（TCP 连接还活着，只是不再往下发分片），太紧。
# 放宽到 300s；填 0/off/none 关掉看门狗，交给内核级 TCP 超时兜底。
_RAW_CHUNK_TIMEOUT = os.getenv("LLM_STREAM_CHUNK_TIMEOUT", "").strip().lower()
LLM_STREAM_CHUNK_TIMEOUT: int | None = (
    None
    if _RAW_CHUNK_TIMEOUT in {"0", "off", "none", "disable", "disabled"}
    else _int_or(_RAW_CHUNK_TIMEOUT, 300)
)


# ---- 上下文自动压缩 ----
# 窗口这个数只能由用户给：provider 一般不报（DeepSeek 的 /v1/models 只有 id/owned_by），
# 我们也学不来 Pi 那种"生成式模型目录"（没有构建步骤，base_url 可指向任意兼容端点）。
# 默认取保守的 64k——.env.example 里的 deepseek-chat 正好是 64K；换大窗口模型请显式调大，
# 启动日志会写明实际用的是哪个值、来自哪里。
_RAW_WINDOW = os.getenv("LLM_CONTEXT_WINDOW", "").strip()
LLM_CONTEXT_WINDOW = _int_or(_RAW_WINDOW, 64_000)
CONTEXT_WINDOW_SOURCE = ".env" if _RAW_WINDOW else "默认值（未配置 LLM_CONTEXT_WINDOW）"

# 关掉就完全回到没有压缩时的行为。
COMPACT_ENABLED = os.getenv("COMPACT_ENABLED", "1").strip() != "0"

# 触发线 = 窗口 × 该比例。研究共识是 85–90% 偏晚、95% 太晚；Hermes 的 in-loop 默认 50%
# （为省 token，偏早），0.8 是折中。
COMPACT_AT = _ratio_or(os.getenv("COMPACT_AT", "").strip(), 0.8)

# 保留的最近原文预算（Pi 的默认值），其余中段摘要掉。
COMPACT_KEEP_TOKENS = _int_or(os.getenv("COMPACT_KEEP_TOKENS", "").strip(), 20_000)

# 摘要用哪个模型：留空 = 用主模型。填了就单独建一个（小模型更快更便宜）。
COMPACT_MODEL = os.getenv("COMPACT_MODEL", "").strip()


def _resolve(raw: str) -> str:
    """
    把配置里的相对路径锚定到项目根。

    否则 ./data/checkpoints.db 会随启动目录漂移：从 app/ 启动就会写到 app/data/，
    凭空多出一个库，会话看起来像"丢了"。
    """
    if not raw:
        return ""
    path = Path(raw).expanduser()
    return str(path if path.is_absolute() else (PROJECT_ROOT / path).resolve())


SQLITE_PATH = _resolve(os.getenv("SQLITE_PATH", "./data/checkpoints.db"))

# ---- 技能（Skills）与项目约定（AGENTS.md） ----
# 技能是"索引常驻 + 正文按需读取"的能力包（`SKILL.md`），约定文件是 `AGENTS.md` / `CLAUDE.md`。
# 两者都只做**提示词注入**，没有任何检索：索引全量进系统提示词，匹不匹配交给模型判断。
SKILLS_ENABLED = os.getenv("SKILLS_ENABLED", "1").strip() != "0"

# 全局技能库。工作区技能库固定在 `<工作区>/.my_agent/skills`，不用配。
# 它同时是沙箱的"受信只读根"：模型读这里的正文不弹授权卡片（只读，写仍要授权）。
SKILLS_DIR = _resolve(os.getenv("SKILLS_DIR", "~/.my_agent/skills"))

AGENTS_MD_ENABLED = os.getenv("AGENTS_MD_ENABLED", "1").strip() != "0"

# 是否向上遍历祖先目录找约定文件（到含 .git 的目录为止）。关掉就只读全局与工作区根。
AGENTS_ANCESTORS = os.getenv("AGENTS_ANCESTORS", "1").strip() != "0"

# ---- 插件（阶段 E） ----
# 插件发现的总开关。关掉 = 只装配内置插件，两级插件库都不扫。
PLUGINS_ENABLED = os.getenv("PLUGINS_ENABLED", "1").strip() != "0"

# 全局插件库：`.py`（可编程）/ `.json`（纯片段声明）；**装载即信任**。
PLUGINS_DIR = _resolve(os.getenv("PLUGINS_DIR", "~/.my_agent/plugins"))

# 工作区插件库：**默认关**（§5.1）。置 1 = 每个会话按自己的工作区扫
# `<该工作区>/.my_agent/plugins/`，且每个文件要在该工作区的授权清单
# （`<该工作区>/.my_agent/plugins.json` 的 trusted）里才装载——"首次需审批、按工作区持久化"。
# 打开后每个工作区各自一套插件与图版本：A 批准的插件不影响 B 的会话。
PLUGINS_WORKSPACE = os.getenv("PLUGINS_WORKSPACE", "0").strip() in ("1", "true", "yes")

# 内容指纹轮询热重载（Windows 上不依赖 inotify；比 mtime 稳，绕开"同秒等长"）。
# 置 0 关掉：插件只在启动时扫一次。
PLUGIN_WATCH = os.getenv("PLUGIN_WATCH", "1").strip() != "0"

# 允许 agent 用 `install_plugin` 往工作区插件库写 `.py`（**等于执行任意代码**）：默认关。
# 开了之后它仍然要过人工确认卡，且只在工作区插件库启用（PLUGINS_WORKSPACE=1）时才有意义。
# 数据形态的片段（`add_fragment`）不受这个开关影响——那个不执行代码。
PLUGIN_AUTHOR_INSTALL = os.getenv("PLUGIN_AUTHOR_INSTALL", "0").strip().lower() in (
    "1",
    "true",
    "yes",
)

# 图版本账本保留几个历史版本（LRU）；无在飞租约、未超此数的旧版本可回收。
GRAPH_VERSIONS_KEEP = _int_or(os.getenv("GRAPH_VERSIONS_KEEP", "").strip(), 4)


def llm_configured() -> bool:
    """没配 key 时对话接口直接说明情况；列表、浏览、删除这些不依赖模型的接口照常可用。"""
    return bool(LLM_API_KEY)
