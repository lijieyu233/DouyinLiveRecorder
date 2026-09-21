# coding=utf-8
"""抖音直播录制工具 —— 入口。

有终端时以命令行模式运行，无终端（双击运行）时以 GUI 模式运行。
"""

import sys

from dylr.core import app


def main() -> None:
    gui_mode = not (sys.stdin and sys.stdin.isatty())
    app.init(gui_mode=gui_mode)


if __name__ == '__main__':
    main()
