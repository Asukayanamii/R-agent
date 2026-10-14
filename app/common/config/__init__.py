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
import sys
from pathlib import Path

from dotenv import load_dotenv

FROZEN = bool(getattr(sys, "frozen", False))
"""是不是打包后的可执行文件（PyInstaller 会设 ``sys.frozen``）。"""

APP_DIR = (
    Path(sys.executable).resolve().parent
    if FROZEN
    else Path(__file__).resolve().parents[3]
)
"""应用目录：打包后是可执行文件所在目录，源码运行是项目根。

相对路径配置（`SQLITE_PATH=./data/...`）都锚定到它——所以"从哪个目录启动"不影响落点。
"""

PROJECT_ROOT = APP_DIR

if not FROZEN and not (PROJECT_ROOT / "requirements.txt").is_file():
    raise RuntimeError(f"PROJECT_ROOT 算错了，指向 {PROJECT_ROOT}")

CONFIG_DIR = ".r-agent"
"""应用在工作区/用户目录下用的隐藏目录名。

沙箱授权（`sandbox.json`）、技能库（`skills/`）、项目约定（`AGENTS.md`）都住在这个名字下，
所以它只能有一处定义——写两处迟早会分叉，而分叉的表现是"某个功能静默看不到文件"。
"""

HOME_DIR = Path.home() / CONFIG_DIR
"""用户目录下的应用目录：插件库、技能库、会话库、用户配置的默认落点。

**打包后所有可写的东西都落在这里**（安装目录只读/不该被写）；源码运行也认它，
但默认值仍偏向工程目录下那份（`./data`、`~/.r-agent/plugins`）——老用户的位置不变。
"""

USER_ENV = HOME_DIR / ".env"
"""用户级配置：打包后用户改这里，不动安装目录（首次启动会自动生成一份带注释的模板）。"""

APP_ENV = PROJECT_ROOT / ".env"
"""应用级配置：源码运行就是仓库里那份 `.env`；打包后是 exe 旁边那份（便携部署用）。"""


def _load_env() -> None:
    """三层配置，优先级 **真环境变量 > 用户级 `.env` > 应用级 `.env`**。

    `load_dotenv` 默认不覆盖已有值，所以顺序是：先记下真环境变量 → 应用级 → 用户级（覆盖）
    → 把真环境变量盖回来。写死这个顺序，是因为"用户在 `~/.r-agent/.env` 里改了插件库位置，
    却被安装目录里那份默认值盖掉"是最难查的一类问题（表现是"改了没反应"）。
    """
    preset = dict(os.environ)
    load_dotenv(APP_ENV, override=False)
    load_dotenv(USER_ENV, override=True)
    os.environ.update(preset)


def _ensure_user_env() -> None:
    """打包后首次启动生成一份带注释的用户配置，把"能改什么"摆到用户面前。

    只在打包形态做：源码运行有仓库里的 `.env`，不需要在用户目录再放一份。
    """
    if not FROZEN or USER_ENV.exists():
        return
    try:
        USER_ENV.parent.mkdir(parents=True, exist_ok=True)
        USER_ENV.write_text(USER_ENV_TEMPLATE, encoding="utf-8")
    except OSError:
        # 用户目录不可写也不该拦启动：配置全都有默认值
        pass


USER_ENV_TEMPLATE = """\
# r-agent 用户配置（打包后首次启动自动生成）
#
# 优先级：真环境变量 > 这个文件 > 安装目录旁的 .env > 代码默认值
# **每一行默认都是注释掉的**：不改这个文件 = 全用默认值；要改就把对应行取消注释。
# （自动生成一份"全部生效的空配置"会盖掉别处的设置，表现是"改了没反应"。）

# ---- 模型 ----
# LLM_API_KEY=
# LLM_BASE_URL=https://api.deepseek.com/v1
# LLM_MODEL=deepseek-chat
# 上下文窗口（tokens）：按你实际用的模型填，压缩阈值按它算
# LLM_CONTEXT_WINDOW=64000

# ---- 库的位置（都支持 ~ 与相对本文件的路径）----
# 全局插件库：`.py`（可编程）/ `.json`（纯片段声明），装载即信任
# PLUGINS_DIR=~/.r-agent/plugins
# 全局技能库（`SKILL.md` 形态）；工作区技能库固定在 <工作区>/.r-agent/skills
# SKILLS_DIR=~/.r-agent/skills
# 会话/审计库
# SQLITE_PATH=~/.r-agent/data/checkpoints.db

# ---- 插件开关 ----
# 工作区插件库（按会话工作区分区、每个文件要人工批准）
# PLUGINS_WORKSPACE=1
# 内容指纹轮询热重载
# PLUGIN_WATCH=1
# 允许 agent 用 install_plugin 写 .py（等于执行任意代码，仍要过确认卡）：默认关
# PLUGIN_AUTHOR_INSTALL=0

# ---- 日志 ----
# LOG_LEVEL=INFO
"""


_ensure_user_env()
_load_env()

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


def _default_sqlite() -> str:
    """会话库默认位置：打包后放用户目录（安装目录不该被写），源码运行维持原来的 `./data`。

    老用户的位置一个字节都不动——这是"默认值只在打包形态换"的原因。
    """
    return str(HOME_DIR / "data" / "checkpoints.db") if FROZEN else "./data/checkpoints.db"


SQLITE_PATH = _resolve(os.getenv("SQLITE_PATH", "").strip() or _default_sqlite())


def library_paths() -> dict[str, str]:
    """库的位置一览（启动日志与"我的库在哪"这类问题的唯一答案）。"""
    return {
        "应用目录": str(APP_DIR),
        "用户配置": str(USER_ENV),
        "全局插件库": PLUGINS_DIR,
        "全局技能库": SKILLS_DIR,
        "会话库": SQLITE_PATH,
    }

# ---- 技能（Skills）与项目约定（AGENTS.md） ----
# 技能是"索引常驻 + 正文按需读取"的能力包（`SKILL.md`），约定文件是 `AGENTS.md` / `CLAUDE.md`。
# 两者都只做**提示词注入**，没有任何检索：索引全量进系统提示词，匹不匹配交给模型判断。
SKILLS_ENABLED = os.getenv("SKILLS_ENABLED", "1").strip() != "0"

# 全局技能库。工作区技能库固定在 `<工作区>/.r-agent/skills`，不用配。
# 它同时是沙箱的"受信只读根"：模型读这里的正文不弹授权卡片（只读，写仍要授权）。
SKILLS_DIR = _resolve(os.getenv("SKILLS_DIR", "~/.r-agent/skills"))

AGENTS_MD_ENABLED = os.getenv("AGENTS_MD_ENABLED", "1").strip() != "0"

# 是否向上遍历祖先目录找约定文件（到含 .git 的目录为止）。关掉就只读全局与工作区根。
AGENTS_ANCESTORS = os.getenv("AGENTS_ANCESTORS", "1").strip() != "0"

# ---- 插件（阶段 E） ----
# 插件发现的总开关。关掉 = 只装配内置插件，两级插件库都不扫。
PLUGINS_ENABLED = os.getenv("PLUGINS_ENABLED", "1").strip() != "0"

# 全局插件库：`.py`（可编程）/ `.json`（纯片段声明）；**装载即信任**。
PLUGINS_DIR = _resolve(os.getenv("PLUGINS_DIR", "~/.r-agent/plugins"))

# 工作区插件库：**默认关**（§5.1）。置 1 = 每个会话按自己的工作区扫
# `<该工作区>/.r-agent/plugins/`，且每个文件要在该工作区的授权清单
# （`<该工作区>/.r-agent/plugins.json` 的 trusted）里才装载——"首次需审批、按工作区持久化"。
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
