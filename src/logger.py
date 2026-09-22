# -*- coding: utf-8 -*-

import os
import sys
from loguru import logger

from . import paths

logger.remove()

custom_format = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> - <level>{message}</level>"

logger.add(
    sink=sys.stderr,
    format=custom_format,
    level="DEBUG",
    colorize=True,
    enqueue=True
)

#: 项目根目录（正斜杠字符串）。上游用 ``sys.argv[0]`` 推导，以 ``python -m``
#: 方式启动时会指到包内目录，日志会落到包里，这里统一走 src.paths。
script_path = paths.slash(paths.project_root())
logs_path = paths.ensure_dir(paths.logs_dir())


def _add_file_sink(filename: str, **kwargs) -> int | None:
    """添加文件日志。

    上游直接在模块顶层 ``logger.add(...)``：一旦 ``logs/`` 不可写（只读盘、
    被杀毒软件占用、文件被其它进程独占），``loguru`` 会抛 ``PermissionError``，
    整个程序连启动都做不到。这里退化为「换个文件名」→「只写终端」两级兜底，
    保证录制功能不被日志拖死。
    """
    target = os.path.join(logs_path, filename)
    try:
        return logger.add(target, **kwargs)
    except (PermissionError, OSError) as exc:
        stamped = filename.replace(".log", f"-{os.getpid()}.log")
        try:
            fallback = logger.add(os.path.join(logs_path, stamped), **kwargs)
            print(f"[logger] {filename} 不可写（{exc}），已改为 {stamped}", file=sys.stderr)
            return fallback
        except (PermissionError, OSError) as inner:
            print(f"[logger] 日志文件不可写，已关闭文件日志：{inner}", file=sys.stderr)
            return None


_add_file_sink(
    "streamget.log",
    level="DEBUG",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
    filter=lambda i: i["level"].name != "INFO",
    serialize=False,
    enqueue=True,
    retention=1,
    rotation="300 KB",
    encoding='utf-8'
)

_add_file_sink(
    "PlayURL.log",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}",
    filter=lambda i: i["level"].name == "INFO",
    serialize=False,
    enqueue=True,
    retention=1,
    rotation="300 KB",
    encoding='utf-8'
)
