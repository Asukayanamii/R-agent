"""
桌面端入口。

放在项目根目录而不是 app/ 里面：直接运行脚本时 Python 只把**脚本所在目录**
加进 sys.path，所以 `python app/desktop.py` 会因为找不到 app 包而失败。
这个文件在根目录，两种跑法都能work：

    python run_desktop.py
    python -m app.desktop
"""

from app.desktop import main

if __name__ == "__main__":
    main()
