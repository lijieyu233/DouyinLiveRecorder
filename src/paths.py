# -*- coding: utf-8 -*-
"""项目路径集中解析。

上游多个模块用 ``os.path.realpath(sys.argv[0])`` 反推项目根目录。这在以
模块方式启动时会算错——例如 ``python -m dylr``，``sys.argv[0]`` 指向包内的
``__main__.py``，于是 ``logs/``、``node/``、``ffmpeg/`` 全都会落到包里。

这里统一成单一入口：

1. 环境变量 ``DYLR_HOME``（入口处显式设置；Electron 拉起后端时会传入）
2. 否则按本文件位置推断（``<root>/src/paths.py`` → ``<root>``）

所有需要目录位置的新代码都应从这里取，不要再自行拼 ``sys.argv[0]``。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "DYLR_HOME"


def project_root() -> Path:
    """项目根目录。"""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在并返回它。"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sub(name: str) -> Path:
    return project_root() / name


def config_dir() -> Path:
    return _sub("config")


def config_file() -> Path:
    return config_dir() / "config.ini"


def url_config_file() -> Path:
    return config_dir() / "URL_config.ini"


def backup_dir() -> Path:
    return _sub("backup_config")


def downloads_dir() -> Path:
    return _sub("downloads")


def logs_dir() -> Path:
    return _sub("logs")


def node_dir() -> Path:
    return _sub("node")


def ffmpeg_dir() -> Path:
    return _sub("ffmpeg")


def slash(path: str | Path) -> str:
    """统一成正斜杠字符串。

    上游大量使用 ``f'{base}/{name}'`` 拼接路径并按 ``/`` 切分，Windows 下
    反斜杠会让这些逻辑失效，所以对外暴露的路径一律转正斜杠。
    """
    return str(path).replace("\\", "/")


# 兼容上游命名：这些常量在旧代码里被广泛引用
SCRIPT_PATH = slash(project_root())
