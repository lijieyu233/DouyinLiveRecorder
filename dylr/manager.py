# -*- coding: utf-8 -*-
"""任务编排：取代 ``main.py`` 里的全局变量 + 顶层 ``while True``。

上游把所有运行期状态放在模块级全局变量里（``recording``、``error_count``、
``running_list``、``url_tuples_list``、``first_run``……），并且：

* 顶层 ``while True`` **没有休眠**，每轮都会用 ``read_config_value`` 把
  ``config.ini`` 完整读 100 多次、重新解析 URL 文件、重写 URL 文件，
  等于常驻一个高 IO 的忙循环；
* 全局代理探测（``urlopen('https://www.google.com', timeout=15)``）在模块
  导入阶段同步执行，没代理时启动会被卡住十几秒；
* 新任务靠 ``running_list`` 判断是否已启动，和录制状态耦合在一起。

:class:`RecorderManager` 把这些拆开：

* 状态集中在对象上，线程安全；
* 改为**按变更轮询**（默认 5 秒，且只有文件 mtime 变了才重读），空闲时几乎零开销；
* 代理探测丢到后台线程，启动不再被网络阻塞；
* 任务的启停/增删改都变成明确的方法，HTTP 接口直接调用。
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import Any

from src import paths
from src.proxy import ProxyDetector
from src.utils import logger

from . import ffmpeg as ff
from .config import AppConfig, RESTART_FIELDS
from .events import CONTROL, STATS, TASK, EventBus
from .models import QUALITIES, Task, TaskState
from .notify import Notifier
from .platforms import PLATFORMS
from .recorder import RecorderTask
from .urlstore import LineIssue, TaskStore

VERSION = "5.0.0"
PROJECT_URL = "https://github.com/ihmily/DouyinLiveRecorder"

#: URL / 配置文件轮询间隔（秒）
POLL_INTERVAL = 5
#: 统计与并发自适应间隔（秒）
TICK_INTERVAL = 5
#: 错误率滑动窗口
ERROR_WINDOW_SIZE = 10


class RecorderManager:
    """直播录制服务的中枢。"""

    def __init__(self, cfg: AppConfig | None = None, bus: EventBus | None = None,
                 store: TaskStore | None = None) -> None:
        self.cfg = cfg or AppConfig()
        self.bus = bus or EventBus()
        self.store = store or TaskStore()
        self.notifier = Notifier(self.cfg)

        self.tasks: dict[str, Task] = {}
        self.workers: dict[str, RecorderTask] = {}
        self.issues: list[LineIssue] = []

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        # ---- 运行状态（原全局变量） ----
        self.running = False
        self.exit_recording = False
        self.global_proxy = False
        self.proxy_info: dict[str, Any] = {}
        self.error_count = 0
        self.error_window: list[int] = []
        self.max_request = max(1, self.cfg.number("max_request"))
        self.request_preset = self.max_request
        self.semaphore = threading.Semaphore(self.max_request)
        self.recording: set[str] = set()
        self.started_at: float | None = None
        self._url_mtime: float = 0.0
        self._cfg_mtime: float = 0.0
        self._last_disk_check = 0.0
        self._disk_free: float | None = None

    # ================================================================== 启停
    def start(self, autostart: bool = True) -> None:
        """启动后台循环；``autostart`` 决定是否立刻开始监控任务。"""
        if self.running:
            return
        ff.setup_path()
        paths.ensure_dir(paths.downloads_dir())
        paths.ensure_dir(paths.logs_dir())
        self._backup_config()

        self.reload_tasks(initial=True)
        self.running = True
        self.started_at = self.started_at or time.time()
        self._stop.clear()

        for loop in (self._poll_loop, self._stats_loop, self._tuner_loop, self._proxy_loop):
            thread = threading.Thread(target=loop, name=loop.__name__, daemon=True)
            thread.start()
            self._threads.append(thread)

        if autostart:
            self.start_all()
        self._publish_control()
        self._publish_stats()

    def shutdown(self, timeout: float = 20.0) -> None:
        """停止所有任务并退出后台循环。"""
        self.stop_all(reason="服务停止")
        self._stop.set()
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout / max(1, len(threads)))
        self._threads.clear()
        self.running = False
        self._publish_control()

    def start_all(self) -> int:
        """启动全部已启用任务，返回启动数量。"""
        started = 0
        with self._lock:
            tasks = [t for t in self.tasks.values() if t.enabled]
        for task in tasks:
            if self._spawn(task):
                started += 1
                # 上游的「排队读取网址时间」，避免同时打接口
                delay = self.cfg.number("local_delay_default")
                if delay:
                    time.sleep(delay)
        self._publish_control()
        return started

    def stop_all(self, reason: str = "已暂停全部任务") -> None:
        with self._lock:
            workers = list(self.workers.values())
            self.workers.clear()
        for worker in workers:
            worker.stop(reason)
        for worker in workers:
            if worker.thread:
                worker.thread.join(timeout=15)
        self._publish_control()

    def set_running(self, running: bool) -> bool:
        """全局开关。"""
        if running:
            if not self.running:
                self.start()
            else:
                self.start_all()
        else:
            self.stop_all()
        return self.running

    # ================================================================== 任务
    def reload_tasks(self, initial: bool = False) -> list[Task]:
        """从 URL_config.ini 重新加载任务，保留已有运行期状态。"""
        loaded, issues = self.store.load()
        self.issues = issues
        default_quality = self.cfg.text("video_record_quality") or QUALITIES[0]
        added: list[Task] = []
        with self._lock:
            seen: set[str] = set()
            for incoming in loaded:
                if not incoming.quality:
                    incoming.quality = default_quality
                seen.add(incoming.url)
                existing = self.tasks.get(incoming.url)
                if existing is None:
                    self.tasks[incoming.url] = incoming
                    if incoming.enabled and self.running and not initial:
                        added.append(incoming)
                    continue
                # 保留运行期字段，只更新文件里的可变配置
                existing.quality = incoming.quality if incoming.quality_explicit else default_quality
                existing.quality_explicit = incoming.quality_explicit
                existing.label = incoming.label
                existing.enabled = incoming.enabled
                existing.order = incoming.order
            for url in list(self.tasks):
                if url not in seen:
                    worker = self.workers.pop(url, None)
                    if worker:
                        worker.stop("地址已从列表移除")
                    del self.tasks[url]

        for issue in issues:
            self.bus.publish("notice", {
                "level": issue.level,
                "text": f"{issue.reason}：{issue.line.strip()[:60]}",
            })
        if added:
            for task in added:
                self._spawn(task)
        self._publish_tasks()
        return added

    def add_task(self, url: str, quality: str | None = None, label: str = "",
                 start: bool = True) -> tuple[bool, str, Task | None]:
        """新增一个直播间。"""
        url = (url or "").strip()
        if not url:
            return False, "请输入直播间地址", None
        tasks, task, message = self.store.append(url, quality, label)
        if task is None:
            return False, message or "添加失败", None
        self.reload_tasks()
        stored = self.tasks.get(task.url)
        if stored is None:
            return False, "地址不在支持列表内", None
        if start and stored.enabled and self.running:
            self._spawn(stored)
        return True, "已添加", stored

    def update_task(self, url: str, quality: str | None = None, label: str | None = None,
                    enabled: bool | None = None, restart: bool = True) -> Task | None:
        """修改画质 / 主播备注 / 启用状态。"""
        self.store.update(url, quality=quality, label=label, enabled=enabled)
        self.reload_tasks()
        task = self.tasks.get(url)
        if task is None:
            return None
        if enabled is not None and self.running:
            if enabled:
                self._spawn(task, restart=restart)
            else:
                self._kill(url, "已暂停")
        elif quality is not None and self.running and restart:
            self._spawn(task, restart=True)
        return task

    def remove_task(self, url: str) -> bool:
        self._kill(url, "已删除")
        self.store.remove(url)
        with self._lock:
            self.tasks.pop(url, None)
        self._publish_tasks()
        return True

    def reorder_tasks(self, urls: list[str]) -> list[Task]:
        self.store.reorder(urls)
        self.reload_tasks()
        return list(self.tasks.values())

    def start_task(self, url: str) -> bool:
        task = self.tasks.get(url)
        if task is None or not task.enabled:
            return False
        self._spawn(task, restart=True)
        return True

    def stop_task(self, url: str) -> bool:
        return self._kill(url, "已手动停止")

    def restart_task(self, url: str) -> bool:
        task = self.tasks.get(url)
        if task is None or not task.enabled:
            return False
        self._spawn(task, restart=True)
        return True

    def _spawn(self, task: Task, restart: bool = False) -> bool:
        """创建（或重建）任务线程。"""
        with self._lock:
            existing = self.workers.get(task.url)
            if existing and existing.is_alive:
                if not restart:
                    return False
                existing.stop("配置变更，重启任务")
                if existing.thread:
                    existing.thread.join(timeout=20)
            task.state = TaskState.IDLE
            task.message = "任务已启动"
            task.last_error = ""
            worker = RecorderTask(task, self)
            self.workers[task.url] = worker
        worker.start()
        return True

    def _kill(self, url: str, reason: str) -> bool:
        with self._lock:
            worker = self.workers.pop(url, None)
        if not worker:
            return False
        worker.stop(reason)
        if worker.thread:
            worker.thread.join(timeout=20)
        return True

    def on_task_finished(self, worker: RecorderTask) -> None:
        """线程退出时回调，避免 ``workers`` 里留下死引用。"""
        with self._lock:
            current = self.workers.get(worker.task.url)
            if current is worker:
                self.workers.pop(worker.task.url, None)

    def remember_anchor(self, url: str, anchor: str) -> bool:
        """把首次探测到的主播名回填进 URL 文件。"""
        return self.store.remember_anchor(url, anchor)

    # ================================================================== 统计
    def register_error(self) -> None:
        with self._lock:
            self.error_count += 1

    def register_recording(self, name: str) -> None:
        with self._lock:
            self.recording.add(name)

    def unregister_recording(self, name: str) -> None:
        with self._lock:
            self.recording.discard(name)

    @property
    def recording_count(self) -> int:
        with self._lock:
            return len(self.recording)

    def stats(self) -> dict[str, Any]:
        tasks = list(self.tasks.values())
        return {
            "running": self.running,
            "total": len(tasks),
            "enabled": sum(1 for t in tasks if t.enabled),
            "recording": sum(1 for t in tasks if t.state is TaskState.RECORDING),
            "live": sum(1 for t in tasks if t.state in (TaskState.RECORDING, TaskState.LIVE)),
            "waiting": sum(1 for t in tasks if t.state is TaskState.WAITING),
            "error": sum(1 for t in tasks if t.state is TaskState.ERROR),
            "error_count": self.error_count,
            "max_request": self.max_request,
            "global_proxy": self.global_proxy,
            "proxy_info": dict(self.proxy_info),
            "disk_free": self._disk_free,
            "disk_threshold": self.cfg.decimal("disk_space_limit"),
            "uptime": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "recording_bytes": sum(t.recorded_bytes for t in tasks),
        }

    def environment(self) -> dict[str, Any]:
        """环境自检信息（关于页 / 首启动引导用）。"""
        import platform
        import sys

        node_version = ""
        node_dir = paths.node_dir()
        try:
            import subprocess
            result = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=10)
            node_version = (result.stdout or "").strip()
        except Exception:                              # noqa: BLE001
            pass
        return {
            "version": VERSION,
            "project_url": PROJECT_URL,
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "platform": f"{platform.system()} {platform.release()}",
            "ffmpeg": ff.ffmpeg_version(),
            "ffmpeg_ok": bool(ff.ffmpeg_version()),
            "node": node_version,
            "root": str(paths.project_root()),
            "downloads": str(paths.downloads_dir()),
            "logs": str(paths.logs_dir()),
            "node_dir_exists": node_dir.exists(),
            "config_file": str(paths.config_file()),
            "url_file": str(paths.url_config_file()),
            "platform_count": len(PLATFORMS),
        }

    # ================================================================== 内部循环
    def _poll_loop(self) -> None:
        """按变更轮询 URL 与配置文件，替代上游的忙循环。"""
        while not self._stop.wait(POLL_INTERVAL):
            try:
                if self._mtime(self.store.path) != self._url_mtime:
                    self._url_mtime = self._mtime(self.store.path)
                    self.reload_tasks()
                cfg_mtime = self._mtime(self.cfg.path)
                if cfg_mtime != self._cfg_mtime:
                    self._cfg_mtime = cfg_mtime
                    self.cfg.load()
                    self.notifier = Notifier(self.cfg)
                    self.bus.publish("notice", {"level": "info", "text": "检测到配置文件变更，已重新载入"})
                    self._publish_stats()
                self._guard_disk()
            except Exception as exc:                   # noqa: BLE001
                logger.error(f"轮询出错: {exc}")

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _guard_disk(self) -> None:
        """磁盘水位保护：低于阈值时停止录制（上游的同名逻辑）。"""
        now = time.time()
        if now - self._last_disk_check < 30:
            return
        self._last_disk_check = now
        target = self.cfg.text("video_save_path").strip() or str(paths.downloads_dir())
        try:
            usage = shutil.disk_usage(str(paths.project_root()) if not Path(target).exists() else target)
            self._disk_free = round(usage.free / (1024 ** 3), 2)
        except OSError:
            return
        limit = self.cfg.decimal("disk_space_limit")
        if limit and self._disk_free < limit:
            if not self.exit_recording:
                self.bus.publish("notice", {
                    "level": "error",
                    "text": f"磁盘剩余 {self._disk_free} GB，已低于阈值 {limit} GB，暂停录制",
                })
                logger.warning(f"磁盘剩余空间不足 {limit} GB，停止录制以避免写满磁盘")
                self.exit_recording = True
                self.stop_all("磁盘空间不足")
        else:
            self.exit_recording = False

    def _stats_loop(self) -> None:
        while not self._stop.wait(TICK_INTERVAL):
            self._publish_stats()

    def _tuner_loop(self) -> None:
        """并发自适应：错误率高就降并发，恢复后逐步抬回（上游 adjust_max_request）。"""
        while not self._stop.wait(TICK_INTERVAL):
            with self._lock:
                window = list(self.error_window)
                errors = self.error_count
                self.error_count = 0
            rate = sum(window) / len(window) if window else 0
            with self._lock:
                if rate > 5:
                    self.max_request = max(1, self.max_request - 1)
                elif rate < 2.5 and self.max_request < self.request_preset:
                    self.max_request += 1
                self.error_window.append(errors)
                if len(self.error_window) > ERROR_WINDOW_SIZE:
                    self.error_window.pop(0)
                if self.semaphore._value != self.max_request:      # noqa: SLF001 - 内部状态同步
                    self.semaphore = threading.Semaphore(self.max_request)

    def _proxy_loop(self) -> None:
        """后台探测全局代理（上游在导入阶段同步做，会卡启动）。"""
        if self.cfg.flag("skip_proxy_check"):
            self.global_proxy = True
            self.bus.publish("notice", {"level": "info", "text": "已按配置跳过代理检测，按「有代理」处理"})
            return
        try:
            urllib.request.urlopen("https://www.google.com/", timeout=8)
            self.global_proxy = True
            detector = ProxyDetector()
            if detector.is_proxy_enabled():
                info = detector.get_proxy_info()
                self.proxy_info = {"ip": getattr(info, "ip", ""), "port": getattr(info, "port", "")}
            logger.info("检测到全局/规则代理")
        except Exception:                              # noqa: BLE001 - 无代理是常见情况
            self.global_proxy = False
            self.bus.publish("notice", {
                "level": "warning",
                "text": "未检测到全局代理；录制海外平台需要在设置里填写代理地址",
            })
        self._publish_stats()

    # ================================================================== 事件
    def _publish_tasks(self) -> None:
        self.bus.publish(TASK, {"tasks": [t.to_dict() for t in self.snapshot_tasks()]})

    def snapshot_tasks(self) -> list[Task]:
        with self._lock:
            return sorted(self.tasks.values(), key=lambda t: t.order)

    def _publish_stats(self) -> None:
        self.bus.publish(STATS, self.stats())

    def _publish_control(self) -> None:
        self.bus.publish(CONTROL, {"running": self.running, "stats": self.stats()})

    def snapshot(self) -> dict[str, Any]:
        return {
            "stats": self.stats(),
            "tasks": [t.to_dict() for t in self.snapshot_tasks()],
            "issues": [{"line": i.line, "reason": i.reason, "level": i.level} for i in self.issues],
        }

    # ================================================================== 杂项
    def _backup_config(self) -> None:
        """启动时备份配置文件，保留最近 6 份（上游 backup_file_start）。"""
        try:
            backup_dir = paths.ensure_dir(paths.backup_dir())
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            for source in (paths.config_file(), paths.url_config_file()):
                if not source.exists():
                    continue
                target = backup_dir / f"{source.name}_{stamp}"
                target.write_bytes(source.read_bytes())
            backups = sorted(backup_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in backups[6:]:
                try:
                    os.remove(stale)
                except Exception as exc:               # noqa: BLE001 - 清理失败不影响启动
                    # 某些受限环境（安全软件/沙箱）会拒绝删除，逐个重试会拖慢启动几十秒，
                    # 因此第一次失败就放弃本轮清理。
                    logger.debug(f"清理旧配置备份失败，跳过本轮清理: {stale.name} - {exc}")
                    break
        except Exception as exc:                       # noqa: BLE001
            logger.warning(f"配置备份失败: {exc}")

    def config_changed(self, slugs: list[str]) -> list[str]:
        """返回需要重启任务才能生效的字段。"""
        return [s for s in slugs if s in RESTART_FIELDS]

    def start_paused_tasks(self) -> int:
        return self.start_all()
