# -*- coding: utf-8 -*-
"""把 manager / API / 日志桥接组装成一个可运行的后端进程。

两种运行形态：

* ``python -m dylr``          —— 后端 + 控制台状态面板（等价于旧的 ``main.py``）
* ``python -m dylr --no-console`` —— 只跑后端与接口，给 Electron 用

无论哪种形态，Electron 都可以连到同一个 HTTP 接口上；CLI 与桌面端不是两套实现。
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Any

from src import paths
from src.utils import logger

from . import ffmpeg as ff
from .api import ApiServer, make_token
from .config import AppConfig
from .events import CONTROL, LOG, STATS, TASK, EventBus, bridge_loguru
from .manager import PROJECT_URL, VERSION, RecorderManager
from .models import TaskState
from .platforms import PLATFORMS
from .stage import stage
from .urlstore import TaskStore

BANNER = r"""
 ____              _       _     _      ____
|  _ \  ___  _   _(_)_ __ (_)___| |    |  _ \ ___  ___ ___  _ __ ___  ___ _ __
| | | |/ _ \| | | | | '_ \| / __| |    | |_) / _ \/ __/ _ \| '__/ _ \/ _ \ '__|
| |_| | (_) | |_| | | | | | \__ \ |___ |  _ <  __/ (_| (_) | | |  __/  __/ |
|____/ \___/ \__,_|_|_| |_|_|___/_____||_| \_\___|\___\___/|_|  \___|\___|_|
"""

_CLEAR = "cls" if os.name == "nt" else "clear"

PARENT_CHECK_INTERVAL = 5.0


class ParentWatch:
    """监视父进程是否还活着。父进程消失 → 自己也该收尾退出。

    为什么必须有这个东西：Electron 异常退出（GPU FATAL、任务管理器强杀、断电）
    时不会执行优雅停止，本后端就变成**孤儿**继续录制。实测过最坏情况——
    上一轮调试留下的几个孤儿后端同时录同一个直播间，四份并发写盘，磁盘按倍数
    被吃满，而且没有任何界面在提示这件事。

    父进程是外部进程，它的退出不会给自己发信号，所以只能主动轮询。
    Windows 上用 ``OpenProcess`` + ``WaitForSingleObject``：句柄**只开一次**，
    这样即便 PID 后来被系统复用也不会误判成「父进程还在」。
    POSIX 上 ``kill(pid, 0)`` 就够了。
    """

    def __init__(self, pid: int | None) -> None:
        self.pid = int(pid) if pid else None
        self._handle = None
        self._posix = os.name != "nt"
        if self.pid and not self._posix:
            self._handle = self._open_windows(self.pid)

    @staticmethod
    def _open_windows(pid: int):
        import ctypes
        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return None
        # 已经退出的进程句柄也是「已信号」状态，所以这里不能直接判死，
        # 交给 alive() 用 WaitForSingleObject 判断。
        return handle

    def alive(self) -> bool:
        """父进程是否仍在运行。拿不到进程信息时按「还活着」处理，宁可不杀。"""
        if not self.pid:
            return True
        if self._posix:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True
        if self._handle is None:
            # 一开始就没打开成功：说明父进程当时就不在了
            return False
        import ctypes
        WAIT_TIMEOUT = 0x00000102
        return ctypes.windll.kernel32.WaitForSingleObject(self._handle, 0) == WAIT_TIMEOUT

    def close(self) -> None:
        if self._handle is not None and not self._posix:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._handle)
            self._handle = None


class Service:
    """后端服务门面。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, token: str = "",
                 autostart: bool = True, console: bool = True,
                 use_api: bool = True, config: AppConfig | None = None,
                 parent_pid: int | None = None) -> None:
        self.bus = EventBus()
        self.cfg = config or AppConfig()
        self.manager = RecorderManager(cfg=self.cfg, bus=self.bus, store=TaskStore())
        # 先建停止信号：ApiServer 的 /api/quit 回调要直接指向它
        self._stopping = threading.Event()
        self._stopped = False
        self._stop_lock = threading.Lock()
        self.api = ApiServer(self.manager, self.bus, token=token, host=host, port=port,
                            on_quit=self._stopping.set) if use_api else None
        self.token = token
        self.host = host
        self.port = port
        self.autostart = autostart
        self.console = console
        self.parent = ParentWatch(parent_pid)
        self._log_sink = bridge_loguru(self.bus, level="INFO")

    # ================================================================== 等待
    def wait_until_stopped(self) -> None:
        """主等待循环：收到退出信号，或父进程消失，都会返回。"""
        last_check = 0.0
        while not self._stopping.is_set():
            time.sleep(0.5)
            now = time.monotonic()
            if now - last_check < PARENT_CHECK_INTERVAL:
                continue
            last_check = now
            if not self.parent.alive():
                # 注意 loguru 用 {} 占位（不是 %s），写成 %s 会原样打印出来
                logger.warning("父进程（pid={}）已消失，后端随之退出，避免变成孤儿继续录制",
                               self.parent.pid)
                self.stop()
                return
        self.parent.close()

    # ================================================================== 启动
    def start_api(self) -> None:
        """只启动 HTTP 接口。与 manager 分开是为了让启动进度可分段上报。"""
        if self.api is None:
            return
        self.port = self.api.start()

    def start_manager(self) -> None:
        """只启动录制管理器（载入任务、拉起后台循环）。"""
        self.manager.start(autostart=self.autostart)
        self.bus.publish(CONTROL, {"running": self.manager.running})

    def start(self) -> dict[str, Any]:
        info: dict[str, Any] = {"root": str(paths.project_root())}
        self.start_api()
        if self.api is not None:
            info["port"] = self.port
            info["base_url"] = self.api.base_url
        self.start_manager()
        return info

    def environment_problems(self) -> list[str]:
        """启动时的硬性依赖自检。

        原来只在 ``_print_banner`` 里检查 ffmpeg，而 ``--print-port``（Electron）
        这条路根本不走 banner，于是「启动时自检过了」是句空话——录制开始才发现
        缺 ffmpeg。这里把它挪成一条独立、两条路径都会执行的自检。
        """
        env = self.manager.environment()
        problems: list[str] = []
        if not env.get("ffmpeg_ok"):
            problems.append("未检测到 ffmpeg，录制会失败；请把 ffmpeg 加入 PATH 或放到项目的 ffmpeg/ 目录")
        if not env.get("node"):
            problems.append("未检测到 Node.js，部分平台（需签名脚本）将无法取流")
        return problems

    def run_forever(self) -> int:
        """阻塞运行，直到收到 Ctrl+C、SIGTERM 或 ``/api/quit``。"""
        self._print_banner()
        info = self.start()
        if self.api is not None:
            print(f"  控制接口：{self.api.base_url}")
        print("  提示：按 Ctrl+C 停止（会先让 ffmpeg 正常收尾，不会留下孤儿进程）\n")
        self._install_signal_handlers()
        if self.console:
            threading.Thread(target=self._console_loop, name="console", daemon=True).start()
        try:
            self.wait_until_stopped()
        except KeyboardInterrupt:
            print("\n收到退出信号，正在停止录制…")
        finally:
            self.stop()
        _ = info
        return 0

    def _install_signal_handlers(self) -> None:
        """SIGTERM（``taskkill`` 不带 /F）也要走优雅退出，让 ffmpeg 正常收尾。"""
        import signal as signal_module

        def _handler(_signum, _frame):
            self._stopping.set()

        for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal_module, name, None)
            if sig is None:
                continue
            try:
                signal_module.signal(sig, _handler)
            except (ValueError, OSError):
                pass

    def stop(self) -> None:
        """停止录制与接口。可重复调用，只有第一次真正执行收尾。

        注意不能拿 ``_stopping`` 当「已经停过」的判据：``/api/quit`` 与父进程
        看门狗都会先把 ``_stopping`` 置位（好让等待循环醒过来），如果这里见到它
        就 return，真正的收尾（让 ffmpeg 结束、关掉监听 socket）就永远不执行了 ——
        进程会挂在那儿，端口还在 LISTENING。所以另用一个 ``_stopped`` 标记。
        """
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        self._stopping.set()
        try:
            self.manager.shutdown()
        finally:
            if self.api is not None:
                self.api.stop()

    # ================================================================== 控制台
    def _print_banner(self) -> None:
        print(BANNER)
        print(f"  版本 {VERSION}   上游项目 {PROJECT_URL}")
        print("  " + "-" * 66)
        print("  支持的平台：")
        from .platforms import PLATFORMS
        domestic = [p.name for p in PLATFORMS if p.group != "海外"]
        overseas = [p.name for p in PLATFORMS if p.group == "海外"]
        print(f"    国内：{'、'.join(domestic)}")
        print(f"    海外：{'、'.join(overseas)}")
        print("  " + "-" * 66)
        if not ff.ensure_ffmpeg():
            logger.error("缺少 ffmpeg，无法录制，程序退出")
            sys.exit(1)
        print()

    def _console_loop(self) -> None:
        """终端状态面板（等价上游 display_info，但数据来自事件总线）。"""
        time.sleep(1)
        prompt_file = paths.url_config_file()
        if prompt_file.exists() and not prompt_file.read_text(encoding="utf-8-sig").strip():
            try:
                answer = input("请输入要录制的主播直播间网址（多个用换行分隔，直接回车跳过）:\n").strip()
                if answer:
                    os.makedirs(prompt_file.parent, exist_ok=True)
                    prompt_file.write_text(answer, encoding="utf-8-sig")
                    self.manager.reload_tasks()
                    self.manager.start_all()
            except (EOFError, KeyboardInterrupt):
                pass

        last_log: str = ""
        while not self._stopping.is_set():
            try:
                stats = self.manager.stats()
                tasks = self.manager.snapshot_tasks()
                os.system(_CLEAR)
                print(f"|  DouyinLiveRecorder {VERSION}  ——  Ctrl+C 退出")
                print("-" * 78)
                print(f"  监测任务 {stats['total']} 个（运行中 {stats['enabled']}）| "
                      f"正在录制 {stats['recording']} 个 | 等待开播 {stats['waiting']} 个")
                print(f"  并发请求数 {stats['max_request']} | 瞬时错误 {stats['error_count']} | "
                      f"全局代理 {'有' if stats['global_proxy'] else '无'} | "
                      f"磁盘剩余 {stats['disk_free'] if stats['disk_free'] is not None else '--'} GB")
                print(f"  已运行 {_format_duration(stats['uptime'])}"
                      + (f" | 已写入 {stats['recording_bytes'] / 1024 / 1024:.1f} MB"
                         if stats["recording_bytes"] else ""))
                print("-" * 78)
                if not tasks:
                    print("  暂无任务：把直播间地址填进 config/URL_config.ini，或用桌面端添加")
                for task in tasks:
                    flag = "▶" if task.state is TaskState.RECORDING else (
                        "·" if task.enabled else "⏸")
                    name = task.display_name or task.url
                    extra = f"{task.quality} | {task.platform or '待识别'}"
                    line = f"  {flag} {name[:22]:24s} {task.state.label:6s} {extra}"
                    if task.state is TaskState.RECORDING and task.elapsed:
                        line += f"  {_format_duration(task.elapsed)}  {task.recorded_bytes / 1024 / 1024:.1f} MB"
                    elif task.message:
                        line += f"  {task.message[:26]}"
                    print(line)
                print("-" * 78)
                if last_log:
                    print(f"  最近日志：{last_log[:110]}")
                print()
                sys.stdout.flush()
                time.sleep(5)
            except Exception as exc:                   # noqa: BLE001 - 面板不能拖垮主流程
                print(f"面板渲染异常: {exc}")
                time.sleep(5)

    def log_tail(self, limit: int = 1) -> list[str]:
        return [e.data.get("text", "") for e in self.bus.history(limit, types=[LOG])]


def _format_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    hours, rem = divmod(seconds, 3600)
    minutes, sec = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dylr",
        description="DouyinLiveRecorder 后端（直播监控与录制）",
    )
    parser.add_argument("--host", default=os.environ.get("DYLR_HOST", "127.0.0.1"),
                        help="接口监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("DYLR_PORT") or 0),
                        help="接口端口，0 表示自动分配")
    parser.add_argument("--token", default=os.environ.get("DYLR_TOKEN", ""),
                        help="接口访问令牌（桌面端会自动注入）")
    parser.add_argument("--parent-pid", type=int, default=None,
                        help="父进程 PID（桌面端注入）。父进程退出后自己也收尾退出，"
                             "避免它被强杀时留下孤儿后端继续录制")
    parser.add_argument("--no-console", action="store_true",
                        help="不渲染终端状态面板")
    parser.add_argument("--no-server", action="store_true",
                        help="不启动 HTTP 接口，只跑控制台")
    parser.add_argument("--no-autostart", action="store_true",
                        help="启动后不自动开始录制，等待外部指令")
    parser.add_argument("--print-port", action="store_true",
                        help="把实际监听端口打印到 stdout（供父进程读取）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths.ensure_dir(paths.downloads_dir())
    paths.ensure_dir(paths.logs_dir())
    service = Service(
        host=args.host,
        port=args.port,
        token=args.token or make_token(),
        autostart=not args.no_autostart,
        console=not args.no_console,
        use_api=not args.no_server,
        parent_pid=args.parent_pid,
    )
    if args.print_port and service.api is not None:
        # 分阶段上报进度，父进程据此渲染启动页。每一条都对应真实执行的动作，
        # 界面不需要、也不应该自己编步骤。（最早的一条「已启动 Python 运行环境」
        # 在 dylr/__main__.py 里就发了，因为慢的是模块导入，而不是这里。）
        stage("正在准备运行环境")
        ff.setup_path()
        paths.ensure_dir(paths.downloads_dir())
        paths.ensure_dir(paths.logs_dir())

        stage("正在检查 ffmpeg 与 Node.js")
        for problem in service.environment_problems():
            stage(f"注意：{problem}")

        stage("正在启动本地控制接口")
        service.start_api()

        stage("正在载入监控任务")
        service.start_manager()

        task_count = len(service.manager.tasks)
        stage(f"就绪：{len(PLATFORMS)} 个平台 · {task_count} 个监控任务")
        print(f"DYLR_READY port={service.port} token={service.token}", flush=True)
        try:
            # 同样走带父进程监视的等待：Electron 被强杀时这里会自己收尾退出
            service.wait_until_stopped()
        except KeyboardInterrupt:
            pass
        finally:
            # /api/quit 与看门狗都只是把等待循环叫醒，收尾必须在这里兜住，
            # 否则会留下「端口还在监听、ffmpeg 还在写盘」的半死进程
            service.stop()
        return 0
    return service.run_forever()


if __name__ == "__main__":                         # pragma: no cover
    sys.exit(main())
