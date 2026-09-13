"""
桌面启动器：在原生窗口里跑这套前后端。

uvicorn 跑在后台线程，pywebview 占用主线程（它要求如此）。
Windows 上 pywebview 使用 EdgeWebView2，即 Chromium 内核，
因此 /chat/stream 的 fetch 流式读取可以正常工作。

    python -m app.desktop
"""

import socket
import sys
import threading
import time
import urllib.request

import uvicorn

from app.main import app

WINDOW_TITLE = "my-agent"
WINDOW_SIZE = (1180, 780)
WINDOW_MIN = (880, 600)

# /chat/stream 的流式渲染依赖 Chromium 的 fetch + ReadableStream，
# 因此 Windows 上固定用 EdgeWebView2，不交给 pywebview 自动挑选后端。
GUI_BACKEND = "edgechromium" if sys.platform == "win32" else None


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


def main() -> None:
    import webview

    port = _pick_port()
    base = f"http://127.0.0.1:{port}"

    server = _serve_in_background(port)
    _wait_ready(f"{base}/chat/history?thread_id=__boot__")

    webview.create_window(
        WINDOW_TITLE,
        f"{base}/ui/",
        width=WINDOW_SIZE[0],
        height=WINDOW_SIZE[1],
        min_size=WINDOW_MIN,
    )
    webview.start(gui=GUI_BACKEND)

    server.should_exit = True


if __name__ == "__main__":
    main()
