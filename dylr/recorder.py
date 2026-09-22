# -*- coding: utf-8 -*-
"""单个直播间的录制任务。

对应上游 ``main.py`` 的 ``start_record()``（约 1100 行）。重构要点：

1. **不再读写模块级全局变量**：配置在任务启动时快照成
   :class:`RuntimeSettings`，运行期状态挂在任务对象上；
2. **不再有 40 分支 if/elif**：交给 :mod:`dylr.platforms` 注册表；
3. **录制过程可观察**：状态、文件体积、下次检测时间通过事件总线推给界面；
4. **可优雅停止**：``stop()`` 先给 ffmpeg 发退出指令再等待，避免上游
   「强杀 Python 留下孤儿 ffmpeg 继续写盘」的问题。

录制参数（ffmpeg 命令、文件名模板、分段、转码时机）与上游逐项对齐，
仅把「按下标 insert 参数」改成显式构造（见 :mod:`dylr.ffmpeg`）。
"""
from __future__ import annotations

import datetime
import os
import random
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from src import paths, utils
from src.utils import logger

from . import ffmpeg as ff
from . import platforms
from .events import FILES, TASK, EventBus
from .models import FlowInfo, Task, TaskState

if TYPE_CHECKING:                                    # pragma: no cover
    from .manager import RecorderManager

#: 文件名非法字符（与上游一致）
RSTR = r"[\/\\\:\*\？?\"\<\>\|&#.。,， ~！· ]"

_EXTENSIONS = {"TS": "ts", "FLV": "flv", "MKV": "mkv", "MP4": "mp4"}


@dataclass(frozen=True)
class RuntimeSettings:
    """任务启动时的配置快照。

    上游的任务线程只在创建时读一次配置（``main.py`` 顶层 while 循环里读），
    之后改配置必须重启任务才生效。这里把这个语义显式化，界面据此提示
    「改动需重启任务」。
    """

    video_save_type: str
    default_quality: str
    use_proxy: bool
    proxy_addr: str
    delay_default: int
    show_url: bool
    split: bool
    split_time: str
    force_https: bool
    converts_to_mp4: bool
    converts_to_h264: bool
    delete_origin: bool
    create_time_file: bool
    custom_script: str | None
    folder_by_author: bool
    folder_by_time: bool
    folder_by_title: bool
    filename_by_title: bool
    clean_emoji: bool
    save_path: str
    only_notify: bool
    notify_interval: int

    @classmethod
    def from_config(cls, cfg, task: Task) -> "RuntimeSettings":
        script_enabled = cfg.flag("is_run_script")
        return cls(
            video_save_type=ff.normalize_save_type(cfg.text("video_save_type")),
            default_quality=cfg.text("video_record_quality") or "原画",
            use_proxy=cfg.flag("use_proxy"),
            proxy_addr=cfg.text("proxy_addr").strip(),
            delay_default=cfg.number("delay_default"),
            show_url=cfg.flag("show_url"),
            split=cfg.flag("split_video_by_time"),
            split_time=str(cfg.number("split_time")),
            force_https=cfg.flag("enable_https_recording"),
            converts_to_mp4=cfg.flag("converts_to_mp4"),
            converts_to_h264=cfg.flag("converts_to_h264"),
            delete_origin=cfg.flag("delete_origin_file"),
            create_time_file=cfg.flag("create_time_file"),
            custom_script=(cfg.text("custom_script").strip() or None) if script_enabled else None,
            folder_by_author=cfg.flag("folder_by_author"),
            folder_by_time=cfg.flag("folder_by_time"),
            folder_by_title=cfg.flag("folder_by_title"),
            filename_by_title=cfg.flag("filename_by_title"),
            clean_emoji=cfg.flag("clean_emoji"),
            save_path=cfg.text("video_save_path").strip(),
            only_notify=cfg.flag("disable_record"),
            notify_interval=max(10, cfg.number("push_check_seconds")),
        )


def clean_name(text: str, clean_emoji: bool = True) -> str:
    """清洗主播名/标题，用作路径与文件名。"""
    cleaned = re.sub(RSTR, "_", (text or "").strip()).strip("_")
    cleaned = cleaned.replace("（", "(").replace("）", ")")
    if clean_emoji:
        cleaned = utils.remove_emojis(cleaned, "_").strip("_")
    return cleaned or "空白昵称"


def select_source_url(url: str, spec: platforms.PlatformSpec, info: FlowInfo) -> str:
    """选拉流地址：抖音/TikTok 优先 FLV（h265 除外）。"""
    if spec.flv_preferred and info.flv_url:
        if platforms.flv_usable(info.flv_url):
            return info.flv_url
        platforms.warn_h265(spec.name)
    return info.record_url


class RecorderTask:
    """一个直播间的监控 + 录制线程。"""

    def __init__(self, task: Task, manager: "RecorderManager") -> None:
        self.task = task
        self.mgr = manager
        self.cfg = manager.cfg
        self.bus: EventBus = manager.bus
        self.settings = RuntimeSettings.from_config(self.cfg, task)

        self.spec = platforms.resolve(task.url)
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.serial = task.order + 1
        self.anchor = ""
        self.record_name = ""
        self._start_pushed = False
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if self.is_alive:
            return
        self.thread = threading.Thread(target=self._run, name=f"rec-{self.task.id}", daemon=True)
        self.thread.start()

    def stop(self, reason: str = "已停止") -> None:
        """请求停止，并优雅结束正在运行的 ffmpeg。"""
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        self._terminate_process()
        self._set_state(TaskState.STOPPED, reason)

    @property
    def is_alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _terminate_process(self) -> None:
        with self._process_lock:
            process = self._process
        if not process or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                if process.stdin:
                    process.stdin.write(b"q")
                    process.stdin.close()
            else:
                process.send_signal(signal.SIGINT)
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()

    # ------------------------------------------------------------------ 主循环
    def _run(self) -> None:
        if self.spec is None:
            self._set_state(TaskState.ERROR, "不支持的平台地址，请检查链接")
            logger.error(f"{self.task.url} 不是已知平台的直播地址")
            self.mgr.on_task_finished(self)
            return

        self.task.platform = self.spec.name
        self._publish_task()
        if self.spec.needs_proxy and not self._has_proxy:
            self.bus.publish("notice", {
                "level": "warning",
                "text": f"{self.spec.name} 属于海外平台，当前未检测到可用代理",
            })

        while not self.stop_event.is_set():
            try:
                finished = self._cycle()
                self._wait_between_cycles(finished)
            except Exception as exc:                   # noqa: BLE001 - 与上游一致：单轮异常不结束任务
                self._on_error(exc)
                self._set_state(TaskState.ERROR, f"运行异常：{exc}")
                self.mgr.register_error()
                self._sleep(2)

        self._set_state(TaskState.STOPPED, "任务已退出")
        self.mgr.on_task_finished(self)

    # ------------------------------------------------------------------ 单轮检测
    def _cycle(self) -> bool:
        """检测一次并（必要时）录制；返回本轮是否刚录完一场。"""
        self.task.last_check = time.time()
        if not self.task.enabled:
            self._set_state(TaskState.DISABLED, "已在列表里暂停")
            self.stop_event.set()
            return False

        self._set_state(TaskState.CHECKING, "正在获取直播状态")
        info = self._probe()
        if info is None:
            return False

        anchor = info.anchor_name.strip()
        if not anchor:
            self.mgr.register_error()
            self.task.error_count += 1
            self.task.last_error = "获取直播间信息失败"
            self._set_state(TaskState.WAITING, "获取直播间信息失败，稍后重试")
            return False

        self.anchor = clean_name(anchor, self.settings.clean_emoji)
        self.task.anchor = self.anchor
        self.record_name = f"序号{self.serial} {self.anchor}"
        self.task.title = info.title or ""
        self.task.quality_effective = info.quality or self.task.quality

        if not self.task.enabled:
            self._set_state(TaskState.DISABLED, "已被暂停，任务退出")
            self.stop_event.set()
            return False

        # 首次拿到主播名后回填到 URL_config.ini（等价上游 need_update_line_list）
        if not self.task.label and self.mgr.remember_anchor(self.task.url, self.anchor):
            self.task.label = f"主播: {self.anchor}"

        self._handle_push(info)

        if self.settings.only_notify:
            self._set_state(TaskState.LIVE if info.is_live else TaskState.WAITING,
                            "仅推送模式，不录制")
            self._sleep(self.settings.notify_interval)
            return False

        if not info.is_live:
            self._set_state(TaskState.WAITING, "等待开播")
            return False

        real_url = select_source_url(self.task.url, self.spec, info)
        if not real_url:
            self._set_state(TaskState.WAITING, "未取到可用的拉流地址")
            return False

        return self._record(info, real_url)

    # ------------------------------------------------------------------ 探测
    @property
    def _has_proxy(self) -> bool:
        return bool(self.mgr.global_proxy or (self.settings.use_proxy and self.settings.proxy_addr))

    @property
    def proxy(self) -> str | None:
        """本任务实际使用的代理地址。"""
        if not self.settings.use_proxy:
            return None
        addr = self.settings.proxy_addr.strip()
        if not addr:
            return None
        if not addr.startswith("http"):
            addr = "http://" + addr
        # 上游：默认只给「走代理的平台」加代理，额外列表可追加
        if self.spec and any(p.strip() and p.strip() in self.task.url
                             for p in self.cfg.multi("enable_proxy_platform")):
            return addr
        extra = [p.strip() for p in
                 self.cfg.text("extra_enable_proxy_platform").replace("，", ",").split(",") if p.strip()]
        if extra and any(p in self.task.url for p in extra):
            return addr
        return None

    def _probe(self) -> FlowInfo | None:
        spec = self.spec
        assert spec is not None
        if spec.needs_proxy and not self._has_proxy:
            message = f"{spec.name} 需要代理，当前未开启或未填代理地址"
            self.task.last_error = message
            self._set_state(TaskState.WAITING, message)
            self.mgr.register_error()
            return None

        ctx = platforms.ProbeContext(
            url=self.task.url,
            quality_label=self.task.quality,
            quality=platforms.quality_code(self.task.quality),
            proxy=self.proxy,
            has_proxy=self._has_proxy,
            platform=spec,
            config=self.cfg,
        )
        try:
            with self.mgr.semaphore:
                data = spec.probe(ctx)
        except Exception as exc:                       # noqa: BLE001 - 平台接口千奇百怪
            self._on_error(exc)
            self.task.last_error = f"取流失败：{exc}"
            self.mgr.register_error()
            return None

        if not data or not data.get("anchor_name"):
            self.task.last_error = "平台未返回直播间信息"
            return FlowInfo()

        info = FlowInfo.from_probe(data)
        # shopee 会返回 uid，改写后可直接复用
        uid = info.extra.get("uid")
        if uid and spec.key == "shopee":
            self.task.url = self.task.url.split("?")[0] + f"?{uid}"
        return info

    # ------------------------------------------------------------------ 推送
    def _handle_push(self, info: FlowInfo) -> None:
        notifier = self.mgr.notifier
        if not notifier.enabled:
            self._start_pushed = False
            return
        if not info.is_live:
            if self._start_pushed:
                notifier.notify_over(self.record_name, self.task.url)
                self._start_pushed = False
        elif not self._start_pushed:
            notifier.notify_begin(self.record_name, self.task.url)
            self._start_pushed = True

    # ------------------------------------------------------------------ 录制
    def _record(self, info: FlowInfo, real_url: str) -> bool:
        settings = self.settings
        now = datetime.datetime.today().strftime("%Y-%m-%d_%H-%M-%S")
        live_title = clean_name(info.title, settings.clean_emoji) if info.title else ""
        title_in_name = f"{live_title}_" if (live_title and settings.filename_by_title) else ""

        full_path = self._output_dir(now, live_title)
        try:
            os.makedirs(full_path, exist_ok=True)
        except OSError as exc:
            logger.error(f"创建保存目录失败 {full_path}: {exc}")
            self._set_state(TaskState.ERROR, f"无法创建目录：{exc}")
            return False

        url = self._fix_scheme(real_url)
        if settings.show_url:
            shown = info.m3u8_url if (self.spec and self.spec.log_m3u8) else url
            name = self.spec.name if self.spec else ""
            logger.info(f"{name} | {self.anchor} | 拉流地址: {shown}")

        self.task.current_file = ""
        self.task.recorded_bytes = 0
        self.task.segments = []
        self.task.started_at = time.time()
        self._publish_task()

        if self.spec and self.spec.force_flv:
            logger.info(f"{self.spec.name} 使用直连下载 FLV 流")
            return self._download_flv(info.flv_url or info.record_url, full_path, now, title_in_name)

        save_type = settings.video_save_type
        audio_mode = bool(self.spec and self.spec.audio_only) or ff.is_audio_type(save_type)

        # 抖音/TikTok 的 FLV 若是 h265，需要退回 TS
        if self.spec and self.spec.flv_preferred and info.flv_url and not platforms.flv_usable(info.flv_url):
            save_type, audio_mode = "TS", False

        if audio_mode:
            return self._record_audio(full_path, now, title_in_name, url)
        return self._record_video(full_path, now, title_in_name, url, save_type)

    # -- 目录 ---------------------------------------------------------------
    def _output_dir(self, now: str, live_title: str) -> str:
        settings = self.settings
        platform_dir = self.spec.folder if self.spec else "未识别"
        base = settings.save_path
        if base:
            full_path = f"{base}{platform_dir}" if base.endswith(("/", "\\")) else f"{base}/{platform_dir}"
        else:
            full_path = f"{paths.downloads_dir()}/{platform_dir}"
        full_path = full_path.replace("\\", "/")
        if settings.folder_by_author:
            full_path = f"{full_path}/{self.anchor}"
        if settings.folder_by_time:
            full_path = f"{full_path}/{now[:10]}"
        if settings.folder_by_title and live_title:
            if settings.folder_by_time:
                full_path = f"{full_path}/{live_title}_{self.anchor}"
            else:
                full_path = f"{full_path}/{now[:10]}_{live_title}"
        return full_path

    def _fix_scheme(self, real_url: str) -> str:
        settings = self.settings
        if self.spec and self.spec.custom:
            return real_url
        if settings.force_https and real_url.startswith("http://"):
            real_url = real_url.replace("http://", "https://")
        # 上游行为：shopee 反而必须走 http
        if self.spec and self.spec.key == "shopee":
            real_url = real_url.replace("https://", "http://")
        return real_url

    def _headers(self) -> str:
        return self.spec.resolve_header(self.task.url) if self.spec else ""

    def _input_args(self, url: str) -> list[str]:
        return ff.input_args(url, proxy=self.proxy, headers=self._headers(),
                             overseas=bool(self.spec and self.spec.overseas))

    # -- 音频 ---------------------------------------------------------------
    def _record_audio(self, full_path: str, now: str, title_in_name: str, url: str) -> bool:
        settings = self.settings
        save_type = settings.video_save_type
        extension = "mp3" if "m4a" not in save_type.lower() else "m4a"
        name_format = "_%03d" if settings.split else ""
        path = f"{full_path}/{self.anchor}_{title_in_name}{now}{name_format}.{extension}"
        command = self._input_args(url) + ff.output_args(
            save_type, path, split=settings.split, split_time=settings.split_time, audio=True)
        logger.info(f"{self.anchor} 开始录制音频: {os.path.basename(path)}")
        interrupted = self._run_ffmpeg(command, path, "音频")
        return interrupted

    # -- 直连 FLV -----------------------------------------------------------
    def _download_flv(self, source_url: str, full_path: str, now: str,
                      title_in_name: str) -> bool:
        settings = self.settings
        path = f"{full_path}/{self.anchor}_{title_in_name}{now}.flv"
        process_name = f"序号{self.serial} {self.anchor}"
        self._mark_recording(path)

        headers: dict[str, str] = {}
        header_params = self._headers()
        if header_params:
            key, _, value = header_params.partition(":")
            headers[key.strip()] = value.strip()

        watcher = self._start_size_watcher(full_path, path)
        try:
            with open(path, "wb") as fh:
                with httpx.Client(timeout=None) as client:
                    with client.stream("GET", source_url, headers=headers,
                                       follow_redirects=True) as response:
                        if response.status_code != 200:
                            logger.error(f"请求直播流失败，状态码: {response.status_code}")
                            return False
                        for chunk in response.iter_bytes(1024 * 16):
                            if not self._keep_recording():
                                logger.info(f"[{self.anchor}] 收到停止请求，下载中断")
                                return False
                            if chunk:
                                fh.write(chunk)
            logger.info(f"{self.anchor} {time.strftime('%Y-%m-%d %H:%M:%S')} 直播录制完成")
            self.task.segments = self._collect_segments(full_path, os.path.basename(path))
            self.bus.publish(FILES, {"url": self.task.url, "segments": list(self.task.segments)})
            return True
        except Exception as exc:                       # noqa: BLE001
            self._on_error(exc)
            self.mgr.register_error()
            return False
        finally:
            if watcher:
                watcher.set()
            self.mgr.unregister_recording(process_name)
            self.task.started_at = None
            self.task.recorded_bytes = 0
            self._set_state(TaskState.WAITING, "本场录制结束，等待下次检测")

    # -- 视频 ---------------------------------------------------------------
    def _record_video(self, full_path: str, now: str, title_in_name: str,
                      url: str, save_type: str) -> bool:
        settings = self.settings
        extension = _EXTENSIONS.get(save_type, "ts")
        name_format = "_%03d" if settings.split else ""
        path = f"{full_path}/{self.anchor}_{title_in_name}{now}{name_format}.{extension}"

        logger.info(f"{self.anchor} 开始录制视频: {os.path.basename(path)}")
        command = self._input_args(url) + ff.output_args(
            save_type, path, split=settings.split, split_time=settings.split_time)
        interrupted = self._run_ffmpeg(command, path, save_type)

        if save_type == "FLV" and not interrupted:
            self._post_process_flv(path, full_path, now, title_in_name)
        return False

    def _post_process_flv(self, path: str, full_path: str, now: str, title_in_name: str) -> None:
        """FLV 的额外后处理（上游行为）。"""
        settings = self.settings
        if settings.converts_to_mp4:
            target = f"{full_path}/{self.anchor}_{title_in_name}{now}_%03d.mp4"
            if settings.split:
                ff.segment_video(path, target, segment_format="mp4",
                                 segment_time=settings.split_time,
                                 delete_origin=settings.delete_origin)
            else:
                threading.Thread(
                    target=ff.convert_to_mp4, args=(path,),
                    kwargs={"delete_origin": settings.delete_origin,
                            "to_h264": settings.converts_to_h264},
                    daemon=True).start()
        elif settings.split:
            target = f"{full_path}/{self.anchor}_{title_in_name}{now}_%03d.flv"
            ff.segment_video(path, target, segment_format="flv",
                             segment_time=settings.split_time,
                             delete_origin=settings.delete_origin)

    # -- ffmpeg 执行 ---------------------------------------------------------
    def _run_ffmpeg(self, command: list[str], path: str, save_type: str) -> bool:
        """拉起 ffmpeg 并监控。返回 True 表示被停止请求中断。"""
        settings = self.settings
        out_dir = os.path.dirname(path)
        process_name = f"序号{self.serial} {self.anchor}"
        self._mark_recording(path)

        if settings.create_time_file and not settings.split and save_type != "音频":
            ff.start_subtitle_thread(process_name, path.rsplit(".", maxsplit=1)[0],
                                     self._keep_recording)

        watcher = self._start_size_watcher(out_dir, path)
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       startupinfo=ff.get_startup_info())
        except FileNotFoundError:
            logger.error("未找到 ffmpeg，无法录制")
            self._set_state(TaskState.ERROR, "未找到 ffmpeg")
            if watcher:
                watcher.set()
            self.mgr.unregister_recording(process_name)
            return False

        with self._process_lock:
            self._process = process

        try:
            while process.poll() is None:
                if not self._keep_recording():
                    logger.info(f"[{self.anchor}] 收到停止请求，正在结束录制")
                    self._terminate_process()
                    return True
                time.sleep(1)
            return_code = process.returncode
        finally:
            with self._process_lock:
                self._process = None
            if watcher:
                watcher.set()

        if return_code == 0:
            self._complete_recording(path, save_type)
        else:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f"{self.anchor} {stamp} 录制出错，返回码: {return_code}")
            self.task.last_error = f"ffmpeg 返回码 {return_code}"
            self.task.error_count += 1
            self.mgr.register_error()
        self.mgr.unregister_recording(process_name)
        self.task.started_at = None
        self.task.recorded_bytes = 0
        self._set_state(TaskState.WAITING, "本场录制结束，等待下次检测")
        return False

    def _complete_recording(self, path: str, save_type: str) -> None:
        """录制正常结束后的收尾：统计产物、转 mp4、跑脚本。"""
        settings = self.settings
        logger.info(f"{self.anchor} {time.strftime('%Y-%m-%d %H:%M:%S')} 直播录制完成")
        self.task.segments = self._collect_segments(os.path.dirname(path), os.path.basename(path))
        self.bus.publish(FILES, {"url": self.task.url, "segments": list(self.task.segments)})

        if settings.converts_to_mp4 and save_type == "TS":
            if settings.split:
                prefix = os.path.basename(path).rsplit("_", maxsplit=1)[0]
                for candidate in utils.get_file_paths(os.path.dirname(path)):
                    if prefix in candidate:
                        threading.Thread(
                            target=ff.convert_to_mp4, args=(candidate,),
                            kwargs={"delete_origin": settings.delete_origin,
                                    "to_h264": settings.converts_to_h264},
                            daemon=True).start()
            else:
                threading.Thread(
                    target=ff.convert_to_mp4, args=(path,),
                    kwargs={"delete_origin": settings.delete_origin,
                            "to_h264": settings.converts_to_h264},
                    daemon=True).start()

        if settings.custom_script:
            self._run_custom_script(path, save_type)

    def _run_custom_script(self, path: str, save_type: str) -> None:
        script = (self.settings.custom_script or "").strip()
        if "python" in script:
            params = [
                f'--record_name "{self.record_name}"',
                f'--save_file_path "{path}"',
                f'--save_type {save_type}',
                f'--split_video_by_time {self.settings.split}',
                f'--converts_to_mp4 {self.settings.converts_to_mp4}',
            ]
        else:
            params = [
                f'"{self.anchor}"',
                f'"{path}"',
                save_type,
                f'split_video_by_time:{self.settings.split}',
                f'converts_to_mp4:{self.settings.converts_to_mp4}',
            ]
        logger.debug("开始执行自定义脚本")
        ff.run_shell(script + " " + " ".join(params))
        logger.debug("自定义脚本执行结束")

    # -- 进度 ---------------------------------------------------------------
    def _mark_recording(self, path: str = "") -> None:
        self.task.started_at = time.time()
        self.task.current_file = path or self.task.current_file
        self._set_state(TaskState.RECORDING, "录制中")
        self.mgr.register_recording(self.record_name)

    def _keep_recording(self) -> bool:
        return (not self.stop_event.is_set()) and self.task.enabled and not self.mgr.exit_recording

    def _start_size_watcher(self, directory: str, path: str) -> threading.Event | None:
        """每 2 秒统计输出体积，让界面看到「正在写入」而不是干等。"""
        if not directory or not os.path.isdir(directory):
            return None
        stop = threading.Event()
        prefix = os.path.basename(path).split("%")[0]

        def _watch() -> None:
            while not stop.wait(2):
                try:
                    total, newest, newest_ts = 0, "", 0.0
                    for entry in os.scandir(directory):
                        if not entry.is_file() or not entry.name.startswith(prefix):
                            continue
                        stat = entry.stat()
                        total += stat.st_size
                        if stat.st_mtime > newest_ts:
                            newest_ts, newest = stat.st_mtime, entry.path
                    self.task.recorded_bytes = total
                    if newest and newest != self.task.current_file:
                        self.task.current_file = newest.replace("\\", "/")
                        self.bus.publish(FILES, {"url": self.task.url, "file": self.task.current_file})
                    self._publish_task()
                except OSError:
                    return

        threading.Thread(target=_watch, name=f"size-{self.task.id}", daemon=True).start()
        return stop

    def _collect_segments(self, directory: str, pattern: str) -> list[str]:
        try:
            prefix = pattern.split("%")[0]
            return sorted(
                entry.path.replace("\\", "/")
                for entry in os.scandir(directory)
                if entry.is_file() and entry.name.startswith(prefix) and entry.stat().st_size > 0
            )
        except OSError:
            return []

    # ------------------------------------------------------------------ 状态
    def _set_state(self, state: TaskState, message: str = "") -> None:
        self.task.state = state
        self.task.message = message
        self._publish_task()

    def _publish_task(self) -> None:
        self.bus.publish(TASK, {"task": self.task.to_dict()})

    def _on_error(self, exc: Exception) -> None:
        line = getattr(getattr(exc, "__traceback__", None), "tb_lineno", "?")
        logger.error(f"错误信息: {exc} 发生错误的行数: {line}")

    def _sleep(self, seconds: float) -> None:
        """可被打断的等待。"""
        end = time.time() + max(0.0, seconds)
        while not self.stop_event.is_set():
            remaining = end - time.time()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

    def _wait_between_cycles(self, finished: bool) -> None:
        """上游的循环间隔策略：±5s 抖动，错误多则加 60s，刚录完先 30s 复查。"""
        if finished:
            delay = 30
        else:
            delay = max(0, random.randint(-5, 5) + self.settings.delay_default)
            if self.mgr.error_count > 20:
                delay += 60
                logger.warning("瞬时错误过多，本轮延迟加 60 秒")
        self.task.next_check_at = time.time() + delay
        self._publish_task()
        self._sleep(delay)
        self.task.next_check_at = None
        self._publish_task()
