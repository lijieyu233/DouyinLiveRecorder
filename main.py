# -*- coding: utf-8 -*-

"""
DouyinLiveRecorder —— 命令行入口。

Author: Hmily
GitHub: https://github.com/ihmily
Copyright (c) 2023-2025 by Hmily, All Rights Reserved.

----------------------------------------------------------------------------
重构说明（v5）
----------------------------------------------------------------------------
本文件原本是一个 2154 行的单体脚本：模块级全局变量 + 顶层 ``while True`` 忙循环
+ 1100 行的 ``start_record()``。现在分成两层：

* ``dylr/``    —— 录制核心、配置、平台注册表、管理器、HTTP/SSE 接口
* ``main.py``  —— 只做「定位项目根目录 → 交给服务层」这一步

因此命令行的使用方式完全不变（``python main.py`` / ``启动.bat``），
而桌面端走的是同一套核心，不是另一份实现。
"""
import os
import sys
from pathlib import Path

# 先把项目根目录写进环境变量：src/logger、ffmpeg_install、i18n 等模块都据此定位
# logs/ node/ ffmpeg/ config/，这样无论从哪里启动都不会把产物写错地方。
ROOT = Path(__file__).resolve().parent
os.environ.setdefault("DYLR_HOME", str(ROOT))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dylr.service import main  # noqa: E402  （必须在设置 DYLR_HOME 之后导入）

if __name__ == "__main__":
    sys.exit(main())
