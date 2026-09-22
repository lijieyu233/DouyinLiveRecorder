# -*- coding: utf-8 -*-
"""dylr —— DouyinLiveRecorder 的控制核心。

这一层把上游 ``main.py`` 里「模块级全局变量 + 顶层 while True + 1100 行 start_record」
的实现拆成分层结构，供 CLI 与 Electron 桌面端共用：

    config    配置元数据与读写（唯一真相源，同时驱动设置界面）
    models    任务 / 状态 / 流信息等数据模型
    platforms 平台注册表（取代 40 分支 if/elif 与 7 张散落列表）
    urlstore  URL_config.ini 的解析与回写
    ffmpeg    拉流命令构建、转码、切片
    notify    直播状态推送
    recorder  单个录制任务（线程）
    manager   任务编排、并发控制、统计
    api       无依赖 HTTP + SSE 接口
    service   把上面这些组装成可运行的进程

本包不引入任何新的第三方依赖，只用标准库 + 上游已有的 requests/httpx/loguru。
"""

__all__ = ["__version__"]

__version__ = "5.0.0"
