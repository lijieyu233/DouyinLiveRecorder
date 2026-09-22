# -*- coding: utf-8 -*-
"""启动进度上报。

为什么单独一个模块：Electron 启动页要在**最早的时刻**就知道「Python 已经起来了」。
而 ``dylr.service`` 会连带导入 loguru / requests / 整个录制核心，冷启动要几秒；
如果把上报函数放在 service 里，第一条进度必然要等那几秒之后才发得出去。
本模块只依赖 sys，可以被入口文件安全地提前导入。

协议：``DYLR_STAGE <文本>`` 一行一条。父进程（electron/src/backend.js）识别该前缀，
把它当作结构化进度而不是普通日志。仅在 ``--print-port`` 模式下输出——
命令行模式有自己的 banner，不需要这些行。
"""
from __future__ import annotations

import sys

#: 协议前缀，与 electron/src/backend.js 的 STAGE_PATTERN 必须保持一致。
STAGE_PREFIX = "DYLR_STAGE "


def stage_enabled(argv: list[str] | None = None) -> bool:
    """只有在给父进程读 stdout 时才上报。"""
    return "--print-port" in (sys.argv if argv is None else argv)


def stage(text: str) -> None:
    """汇报一个启动里程碑（非 print-port 模式下是空操作）。"""
    if not stage_enabled():
        return
    print(f"{STAGE_PREFIX}{text}", flush=True)
