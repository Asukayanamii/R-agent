"""
工具共用的路径解析、越界防护与输出截断。

单独抽出来是因为六个文件工具都要用，且**越界防护是安全属性**——
模型给的路径可能来自不可信内容（读进来的文件、命令输出），
不拦 `../../` 就等于把整个磁盘交给它。
"""

import os
from pathlib import Path

from app.config import PROJECT_ROOT

MAX_BYTES = 50 * 1024
MAX_LINES = 2000

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


def resolve_path(raw: str) -> Path:
    """把参数路径解析到项目根之下，越界一律拒绝。"""
    candidate = Path(raw.strip() or ".").expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError(f"路径越出项目范围：{raw}")
    return resolved


def rel(path: Path) -> str:
    """
    展示用：尽量给相对项目根的路径，且统一用正斜杠。

    Windows 的反斜杠在模型输出里容易被当成转义符，也不跨平台，
    所以对外一律用 POSIX 分隔符；回传时 Path 两种都认。
    """
    try:
        return path.relative_to(PROJECT_ROOT).as_posix() or "."
    except ValueError:
        return path.as_posix()


def looks_binary(path: Path, sniff: int = 8192) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(sniff)
    except OSError:
        return True


def walk_files(root: Path, limit: int = 20000):
    """遍历文件，跳过噪音目录，目录名与文件名都排序以保证结果稳定。"""
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            seen += 1
            if seen > limit:
                return
            yield Path(dirpath) / name


def clip_bytes(text: str, limit: int = MAX_BYTES) -> tuple[str, int]:
    """按字节裁到上限，返回 (文本, 丢弃的字节数)。"""
    encoded = text.encode("utf-8", "ignore")
    if len(encoded) <= limit:
        return text, 0
    return encoded[:limit].decode("utf-8", "ignore"), len(encoded) - limit


def truncate_head(text: str, more: str = "") -> str:
    """保留开头（读文件用），并说明后面还有内容。"""
    lines = text.split("\n")
    dropped_lines = 0
    if len(lines) > MAX_LINES:
        dropped_lines = len(lines) - MAX_LINES
        lines = lines[:MAX_LINES]
    out = "\n".join(lines)

    out, dropped_bytes = clip_bytes(out)

    if not (dropped_lines or dropped_bytes):
        return out

    tail = f" {more}" if more else ""
    return f"{out}\n[已截断，后面还有内容。{tail}]"


def truncate_tail(text: str, already_dropped: int = 0) -> str:
    """保留结尾（命令输出用）——排错时最近的输出更有用。"""
    lines = text.split("\n")
    dropped_lines = 0
    if len(lines) > MAX_LINES:
        dropped_lines = len(lines) - MAX_LINES
        lines = lines[-MAX_LINES:]
    out = "\n".join(lines)

    dropped_total = already_dropped
    encoded = out.encode("utf-8", "ignore")
    if len(encoded) > MAX_BYTES:
        dropped_total += len(encoded) - MAX_BYTES
        out = encoded[-MAX_BYTES:].decode("utf-8", "ignore")

    if not (dropped_lines or dropped_total):
        return out

    dropped = " 与 ".join(
        part
        for part in (
            f"{dropped_lines} 行" if dropped_lines else "",
            f"{dropped_total} 字节" if dropped_total else "",
        )
        if part
    )
    return (
        f"[输出过大，已丢弃开头的 {dropped}，以下仅为末尾部分。"
        f"需要完整内容请把命令输出重定向到文件后再分段读取]\n{out}"
    )
