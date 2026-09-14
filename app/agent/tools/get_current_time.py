"""
获取当前时间，支持多种格式与命名时区。

规则：**值本身已含时区信息的格式不加后缀**（`iso` 带偏移、`rfc` 带偏移、
`timestamp` 是绝对时间），其余格式补一句 `（时区名 UTC偏移）`——
因为「09:30:00」脱离时区是没有意义的。

时区用 IANA 名称（`Asia/Shanghai`）。Windows 没有系统时区库，靠 `tzdata` 包，
所以它在 requirements 里是显式依赖，而不是碰巧被别的包带进来。
"""

from datetime import datetime, timezone as fixed_timezone
from email.utils import format_datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.tools import tool

from app.agent.tools.common import fail

WEEKDAYS_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")

FORMAT_NAMES = ("iso", "timestamp", "date", "time", "human", "cn", "rfc", "full")

SELF_DESCRIBING = {"iso", "timestamp", "rfc"}


def resolve_timezone(name: str):
    """省略时用本机时区，否则按 IANA 名称解析。"""
    cleaned = (name or "").strip()
    if not cleaned:
        return datetime.now().astimezone().tzinfo
    if cleaned.upper() in {"UTC", "GMT", "Z"}:
        return fixed_timezone.utc
    return ZoneInfo(cleaned)


def offset_text(moment: datetime) -> str:
    raw = moment.strftime("%z")
    return f"{raw[:3]}:{raw[3:]}" if len(raw) == 5 else raw


def render(moment: datetime) -> dict[str, str]:
    """只产出值本身，时区说明统一由调用处补，避免两处各拼一次。"""
    weekday = WEEKDAYS_CN[moment.weekday()]
    return {
        "iso": moment.isoformat(timespec="seconds"),
        "timestamp": str(int(moment.timestamp())),
        "date": moment.strftime("%Y-%m-%d"),
        "time": moment.strftime("%H:%M:%S"),
        "human": f"{moment.strftime('%Y-%m-%d %H:%M:%S')} {weekday}",
        "cn": f"{moment.strftime('%Y年%m月%d日 %H:%M:%S')} {weekday}",
        # %a/%b 受 locale 影响，中文环境下会输出中文，所以用 format_datetime 固定英文
        "rfc": format_datetime(moment),
    }


@tool
async def get_current_time(format: str = "iso", timezone: str = "") -> str:
    """
    获取当前时间。

    format 可选，默认 iso：
      iso        ISO 8601，自带时区偏移，可直接用于拼接
      timestamp  Unix 时间戳（秒）
      date       仅日期，如 2026-09-14
      time       仅时间，如 09:30:00
      human      年月日时分秒 + 星期
      cn         中文写法，如 2026年09月14日 09:30:00 星期一
      rfc        RFC 2822，邮件与 HTTP 头用的格式
      full       以上全部一次给齐，不确定要哪种时用它

    timezone 省略则用本机时区，也可给 IANA 名称，如 UTC、Asia/Shanghai。
    """
    key = (format or "iso").strip().lower()
    if key not in FORMAT_NAMES:
        fail(f"未知的 format：{format}。可选：{'、'.join(FORMAT_NAMES)}")

    try:
        zone = resolve_timezone(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        fail(
            f"无法识别的时区：{timezone}。"
            "请用 IANA 名称，例如 UTC、Asia/Shanghai、America/New_York。"
        )

    moment = datetime.now(zone)
    values = render(moment)
    label = zone.key if isinstance(zone, ZoneInfo) else (moment.tzname() or "本地时区")
    # UTC/GMT 这类名字本身已表明偏移，再补 "+00:00" 只是重复
    suffix = (
        f"（{label}）"
        if label.upper() in {"UTC", "GMT"}
        else f"（{label} UTC{offset_text(moment)}）"
    )

    if key == "full":
        rows = "\n".join(
            f"  {name:<10}{value}"
            + ("" if name in SELF_DESCRIBING else suffix)
            for name, value in values.items()
        )
        return f"时区：{label} UTC{offset_text(moment)}\n{rows}"

    if key in SELF_DESCRIBING:
        return values[key]

    return f"{values[key]}{suffix}"
