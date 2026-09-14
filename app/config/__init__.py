"""
环境配置。换模型服务只改 .env，不动代码。

代码直接放在包的 `__init__.py` 里，和 `app/exceptions/` 一致：对外只有一个
`from app.config import X` 的入口，不再套一层 `config/config.py` 的同名模块。

**`PROJECT_ROOT` 是按目录层级数出来的，挪文件必须同步改。**
这个文件在 `app/config/` 下，所以是往上第三层。算错不会报错，只会让相对路径的
配置静默写到别处去——曾经因此凭空多出一个 `app/data/checkpoints.db`，
用户那边表现为"会话丢了"。下面的断言让这类错误在启动时立刻暴露。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

if not (PROJECT_ROOT / "requirements.txt").is_file():
    raise RuntimeError(f"PROJECT_ROOT 算错了，指向 {PROJECT_ROOT}")

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


def llm_configured() -> bool:
    """没配 key 时对话接口直接说明情况；列表、浏览、删除这些不依赖模型的接口照常可用。"""
    return bool(LLM_API_KEY)
