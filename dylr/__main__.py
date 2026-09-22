# -*- coding: utf-8 -*-
"""``python -m dylr`` 入口。

入口文件本身很轻，慢的是下面那句 ``from .service import main``——它会连带导入
loguru / requests / 整个录制核心，冷启动要几秒。所以这里在最前面就先汇报一条
里程碑：界面立刻能把「Python 起来了」点亮，用户不必干看着转圈猜是不是卡死了。
"""
import sys

from .stage import stage

stage("已启动 Python 运行环境")

from .service import main  # noqa: E402  （必须在 stage() 之后导入，见上）

if __name__ == "__main__":
    sys.exit(main())
