"""
桌面启动器：在原生窗口里跑这套前后端。

uvicorn 跑在后台线程，pywebview 占用主线程（它要求如此）。
Windows 上 pywebview 使用 EdgeWebView2，即 Chromium 内核，
因此 /chat/stream 的 fetch 流式读取可以正常工作。

    python -m app.desktop
"""

import logging
import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from app.main import app

logger = logging.getLogger(__name__)

WINDOW_TITLE = "my-agent"
WINDOW_SIZE = (1180, 780)
WINDOW_MIN = (880, 600)

# /chat/stream 的流式渲染依赖 Chromium 的 fetch + ReadableStream，
# 因此 Windows 上固定用 EdgeWebView2，不交给 pywebview 自动挑选后端。
GUI_BACKEND = "edgechromium" if sys.platform == "win32" else None


class DesktopApi:
    """
    暴露给页面的原生能力，目前只有一个：调系统目录选择器。

    浏览器出于安全拿不到真实路径（showDirectoryPicker 只给目录名），
    所以这个能力只在桌面窗口里存在。页面据此判断走原生弹窗还是退回页内浏览，
    见 static/index.html 的 nativePicker()。
    """

    def pick_folder(self, current: str = "") -> str:
        import webview  # 延迟导入，让 app.desktop 在不装 pywebview 时也能被导入

        window = webview.active_window()
        if window is None:
            return ""
        # directory 不存在时 pywebview 自己会退成 ''，不用在这里判
        picked = window.create_file_dialog(
            webview.FileDialog.FOLDER, directory=current or ""
        )
        return picked[0] if picked else ""


def _pick_port() -> int:
    """让系统分配一个空闲端口，避免和已在跑的 uvicorn 抢 8000。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve_in_background(port: int) -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True, name="uvicorn").start()
    return server


def _wait_ready(url: str, timeout: float = 30.0) -> None:
    """
    等后端真正可用再开窗。

    用 /chat/history 而不是探端口：这个请求会走到 runner，
    因此成功即代表 lifespan 里的 init_runner 已完成。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"后端未能在 {timeout}s 内就绪：{url}")


def serve(host: str = "127.0.0.1", port: int | None = None) -> None:
    """只跑后端、不开窗口（打包后的服务器形态；桌面模式内部也是同一个后端）。

    `--serve` 用途：打包件在没装 pywebview 的机器上跑、丢到服务器上跑、
    以及"我就想要个地址用浏览器打开"。端口 0 = 让系统分配。
    """
    chosen = port or _pick_port()
    logger.info("后端启动：http://%s:%s/ui/（Ctrl+C 停止）", host, chosen)
    uvicorn.run(app, host=host, port=chosen, log_level="info")


def _arg_value(args: list[str], flag: str, default: int) -> int:
    """极简参数取值：`--port 8123` / `--port=8123` 两种写法都认。"""
    for index, item in enumerate(args):
        if item == flag and index + 1 < len(args):
            return int(args[index + 1])
        if item.startswith(f"{flag}="):
            return int(item.split("=", 1)[1])
    return default


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    port = _arg_value(args, "--port", 0) or None

    if "--serve" in args or "--no-window" in args:
        serve(port=port)
        return

    import webview

    port = port or _pick_port()
    base = f"http://127.0.0.1:{port}"
    logger.info("桌面端启动：后端 %s，窗口标题 %s", base, WINDOW_TITLE)

    server = _serve_in_background(port)
    _wait_ready(f"{base}/chat/history?thread_id=__boot__")
    logger.info("后端已就绪，打开窗口 %s/ui/", base)

    webview.create_window(
        WINDOW_TITLE,
        f"{base}/ui/",
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=WINDOW_MIN,
        js_api=DesktopApi(),
    )
    webview.start(gui=GUI_BACKEND)

    logger.info("窗口已关闭，正在停后端")
    server.should_exit = True


if __name__ == "__main__":
    main()
