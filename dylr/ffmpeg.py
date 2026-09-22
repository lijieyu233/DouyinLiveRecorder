# -*- coding: utf-8 -*-
"""ffmpeg 相关：命令构建、转码、切片、时间字幕。

上游把这些函数散落在 ``main.py`` 前 500 行，并且命令里大量使用
``ffmpeg_command.insert(11, "-headers")`` 这类「按下标插参数」的写法——一旦
基础参数列表调整顺序就会静默插错位置。这里改成显式分区构造：

* :func:`input_args`   输入侧参数（含 UA / 代理 / 请求头 / 超时）
* :func:`output_args`  输出侧参数（按保存格式与是否分段）
* :func:`record_command` = 拼起来

参数取值与上游逐项对齐，仅调整了排列顺序（ffmpeg 全局参数与顺序无关）。
"""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from src import paths
from src.utils import logger

from ffmpeg_install import check_ffmpeg, current_env_path, ffmpeg_path

TEXT_ENCODING = "utf-8-sig"

#: 浏览器 UA，部分平台会校验
USER_AGENT = ("Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 ("
              "KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile "
              "Safari/537.36")

_PROTOCOL_WHITELIST = "rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy"

#: 默认（国内）拉流参数
DEFAULT_TUNING = {
    "rw_timeout": "15000000",
    "analyzeduration": "20000000",
    "probesize": "10000000",
    "bufsize": "8000k",
    "max_muxing_queue_size": "1024",
}

#: 海外平台需要放宽超时与缓冲，否则直播源容易断
OVERSEAS_TUNING = {
    "rw_timeout": "50000000",
    "analyzeduration": "40000000",
    "probesize": "20000000",
    "bufsize": "15000k",
    "max_muxing_queue_size": "2048",
}

#: 保存格式归一化：界面/上游都有「MP3音频」这类写法
SAVE_TYPE_ALIASES = {
    "MP3音频": "MP3",
    "M4A音频": "M4A",
    "MP3": "MP3",
    "M4A": "M4A",
    "TS": "TS",
    "MKV": "MKV",
    "FLV": "FLV",
    "MP4": "MP4",
}


def normalize_save_type(value: str | None) -> str:
    text = (value or "").strip().upper()
    if text in SAVE_TYPE_ALIASES:
        return SAVE_TYPE_ALIASES[text]
    if "MP3" in text:
        return "MP3"
    if "M4A" in text:
        return "M4A"
    return "TS"


def is_audio_type(save_type: str) -> bool:
    return normalize_save_type(save_type) in ("MP3", "M4A")


# ---------------------------------------------------------------------------
# 运行环境
# ---------------------------------------------------------------------------

def setup_path() -> None:
    """把内置 ffmpeg / node 目录挂到 PATH 最前面（上游在模块加载时做同样的事）。"""
    prefix = ffmpeg_path + os.pathsep + (current_env_path or "")
    current = os.environ.get("PATH", "")
    if ffmpeg_path not in current:
        os.environ["PATH"] = prefix + os.pathsep + current


def get_startup_info():
    """Windows 下隐藏子进程控制台窗口。"""
    if os.name == "nt":
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return info
    return None


def ffmpeg_version() -> str:
    """返回 ffmpeg 版本号，检测不到返回空串。"""
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout:
            return result.stdout.splitlines()[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def ensure_ffmpeg() -> bool:
    """确认 ffmpeg 可用（本机已装则直接通过，不会触发下载）。"""
    version = ffmpeg_version()
    if version:
        logger.debug(version)
        for line in version.splitlines()[:2]:
            print(line)
    return bool(check_ffmpeg())


# ---------------------------------------------------------------------------
# 命令构建
# ---------------------------------------------------------------------------

def input_args(real_url: str, *, proxy: str | None = None, headers: str = "",
               overseas: bool = False) -> list[str]:
    """输入侧参数。"""
    tuning = OVERSEAS_TUNING if overseas else DEFAULT_TUNING
    args = [
        "ffmpeg", "-y",
        "-v", "verbose",
        "-loglevel", "error",
        "-hide_banner",
    ]
    if proxy:
        args += ["-http_proxy", proxy]
    if headers:
        args += ["-headers", headers]
    args += [
        "-user_agent", USER_AGENT,
        "-protocol_whitelist", _PROTOCOL_WHITELIST,
        "-thread_queue_size", "1024",
        "-rw_timeout", tuning["rw_timeout"],
        "-analyzeduration", tuning["analyzeduration"],
        "-probesize", tuning["probesize"],
        "-fflags", "+discardcorrupt",
        "-re", "-i", real_url,
        "-bufsize", tuning["bufsize"],
        "-sn", "-dn",
        "-reconnect_delay_max", "60",
        "-reconnect_streamed", "-reconnect_at_eof",
        "-max_muxing_queue_size", tuning["max_muxing_queue_size"],
        "-correct_ts_overflow", "1",
        "-avoid_negative_ts", "1",
    ]
    return args


def output_args(save_type: str, path: str, *, split: bool, split_time: int | str,
                audio: bool = False) -> list[str]:
    """输出侧参数。逐分支与上游保持一致。"""
    kind = normalize_save_type(save_type)
    segment = ["-f", "segment", "-segment_time", str(split_time)] if split else []

    if audio:
        # 上游：只有「配置里写了 MP3」才用 lame，否则一律 aac
        if "MP3" in save_type.upper():
            if split:
                return ["-map", "0:a", "-c:a", "libmp3lame", "-ab", "320k",
                        *segment, "-reset_timestamps", "1", path]
            return ["-map", "0:a", "-c:a", "libmp3lame", "-ab", "320k", path]
        if split:
            return ["-map", "0:a", "-c:a", "aac", "-bsf:a", "aac_adtstoasc", "-ab", "320k",
                    *segment, "-segment_format", "mpegts", "-reset_timestamps", "1", path]
        return ["-map", "0:a", "-c:a", "aac", "-bsf:a", "aac_adtstoasc", "-ab", "320k",
                "-movflags", "+faststart", path]

    if kind == "FLV":
        if split:
            return ["-map", "0", "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
                    *segment, "-segment_format", "flv", "-reset_timestamps", "1", path]
        return ["-map", "0", "-c:v", "copy", "-c:a", "copy", "-bsf:a", "aac_adtstoasc",
                "-f", "flv", path]

    if kind == "MKV":
        if split:
            return ["-flags", "global_header", "-c:v", "copy", "-c:a", "aac", "-map", "0",
                    *segment, "-segment_format", "matroska", "-reset_timestamps", "1", path]
        return ["-flags", "global_header", "-map", "0", "-c:v", "copy", "-c:a", "copy",
                "-f", "matroska", path]

    if kind == "MP4":
        if split:
            return ["-c:v", "copy", "-c:a", "aac", "-map", "0",
                    *segment, "-segment_format", "mp4", "-reset_timestamps", "1",
                    "-movflags", "+frag_keyframe+empty_moov", path]
        return ["-map", "0", "-c:v", "copy", "-c:a", "copy", "-f", "mp4", path]

    if split:
        return ["-c:v", "copy", "-c:a", "copy", "-map", "0",
                *segment, "-segment_format", "mpegts", "-reset_timestamps", "1", path]
    return ["-c:v", "copy", "-c:a", "copy", "-map", "0", "-f", "mpegts", path]


def record_command(real_url: str, path: str, save_type: str, *, split: bool, split_time: int | str,
                   audio: bool = False, proxy: str | None = None, headers: str = "",
                   overseas: bool = False) -> list[str]:
    """组装完整录制命令。"""
    return input_args(real_url, proxy=proxy, headers=headers, overseas=overseas) + \
        output_args(save_type, path, split=split, split_time=split_time, audio=audio)


def probe_command(real_url: str, *, proxy: str | None = None, headers: str = "",
                  overseas: bool = False, timeout: int = 12) -> list[str]:
    """只探测能否拉到流（界面里的「测试地址」用）。"""
    return [
        "ffmpeg", "-v", "error", "-hide_banner",
        *( ["-http_proxy", proxy] if proxy else [] ),
        *( ["-headers", headers] if headers else [] ),
        "-user_agent", USER_AGENT,
        "-rw_timeout", (OVERSEAS_TUNING if overseas else DEFAULT_TUNING)["rw_timeout"],
        "-analyzeduration", "5000000",
        "-probesize", "5000000",
        "-i", real_url,
        "-t", str(timeout),
        "-f", "null", "-",
    ]


# ---------------------------------------------------------------------------
# 转码 / 切片 / 字幕
# ---------------------------------------------------------------------------

def _run(command: Sequence[str]) -> bool:
    try:
        subprocess.check_output(list(command), stderr=subprocess.STDOUT,
                                startupinfo=get_startup_info())
        return True
    except subprocess.CalledProcessError as exc:
        logger.error(f"命令执行失败: {exc}")
    except FileNotFoundError:
        logger.error("未找到 ffmpeg，请确认已安装并加入 PATH")
    except Exception as exc:                      # noqa: BLE001 - 上游同样是兜底
        logger.error(f"命令执行出现未知错误: {exc}")
    return False


def convert_to_mp4(file_path: str, *, delete_origin: bool = True, to_h264: bool = False) -> None:
    """把录制产物转成 mp4。"""
    try:
        if not (os.path.exists(file_path) and os.path.getsize(file_path) > 0):
            return
        target = file_path.rsplit(".", maxsplit=1)[0] + ".mp4"
        if to_h264:
            command = ["ffmpeg", "-i", file_path,
                       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                       "-vf", "format=yuv420p", "-c:a", "copy", "-f", "mp4", target]
        else:
            command = ["ffmpeg", "-i", file_path,
                       "-c:v", "copy", "-c:a", "copy", "-f", "mp4", target]
        if _run(command) and delete_origin:
            _safe_remove(file_path)
    except Exception as exc:                      # noqa: BLE001
        logger.error(f"转 MP4 失败: {exc}")


def convert_to_m4a(file_path: str, *, delete_origin: bool = True) -> None:
    """把录制产物抽成 m4a 音频。"""
    try:
        if not (os.path.exists(file_path) and os.path.getsize(file_path) > 0):
            return
        target = file_path.rsplit(".", maxsplit=1)[0] + ".m4a"
        command = ["ffmpeg", "-i", file_path, "-n", "-vn",
                   "-c:a", "aac", "-bsf:a", "aac_adtstoasc", "-ab", "320k", target]
        if _run(command) and delete_origin:
            _safe_remove(file_path)
    except Exception as exc:                      # noqa: BLE001
        logger.error(f"转 M4A 失败: {exc}")


def segment_video(source: str, target: str, *, segment_format: str, segment_time: int | str,
                  delete_origin: bool = True) -> None:
    """按时间切片（用户改了分段设置后对已有文件补切时使用）。"""
    try:
        if not (os.path.exists(source) and os.path.getsize(source) > 0):
            return
        command = [
            "ffmpeg", "-i", source,
            "-c:v", "copy", "-c:a", "copy", "-map", "0",
            "-f", "segment",
            "-segment_time", str(segment_time),
            "-segment_format", segment_format,
            "-reset_timestamps", "1",
            "-movflags", "+frag_keyframe+empty_moov",
            target,
        ]
        if _run(command) and delete_origin:
            _safe_remove(source)
    except Exception as exc:                      # noqa: BLE001
        logger.error(f"切片失败: {exc}")


def _safe_remove(path: str) -> None:
    """删除文件；被占用时重试一次。

    上游直接 ``os.remove``，Windows 上 ffmpeg 刚释放句柄时容易抛异常。
    """
    for attempt in range(2):
        try:
            time.sleep(1)
            if os.path.exists(path):
                os.remove(path)
            return
        except OSError as exc:
            if attempt:
                logger.warning(f"删除原文件失败（可能仍被占用）: {path} - {exc}")


def write_time_subtitle(record_name: str, base_path: str, should_continue: Callable[[], bool],
                        sub_format: str = "srt") -> None:
    """按秒写时间字幕，用于事后定位录制时间点。"""
    start = datetime.datetime.now()
    mark = start.strftime("%Y-%m-%d %H:%M:%S")
    index = 0
    target = f"{base_path}.{sub_format.lower()}"
    while True:
        index += 1
        txt = (f"{index}\n"
               f"{_hms(index)},000 --> {_hms(index + 1)},000\n"
               f"{mark}\n\n")
        with open(target, "a", encoding=TEXT_ENCODING) as fh:
            fh.write(txt)
        if not should_continue():
            return
        time.sleep(1)
        mark = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hms(seconds: int) -> str:
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def start_subtitle_thread(record_name: str, base_path: str, should_continue: Callable[[], bool]) -> None:
    thread = threading.Thread(
        target=write_time_subtitle,
        args=(record_name, base_path, should_continue),
        name=f"subs-{Path(base_path).name}",
        daemon=True,
    )
    thread.start()


def run_shell(command: str) -> None:
    """执行用户配置的自定义脚本。"""
    try:
        process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, startupinfo=get_startup_info())
        stdout, stderr = process.communicate()
        for stream in (stdout, stderr):
            text = (stream or b"").decode("utf-8", errors="ignore").strip()
            if text:
                print(text)
    except PermissionError:
        logger.error("脚本无执行权限；Linux 下请先执行 chmod +x <script>")
    except OSError as exc:
        logger.error(f"脚本执行失败: {exc}；bash 脚本请确认首行为 #!/bin/bash")


def media_summary(file_path: str) -> dict:
    """用 ffprobe 读一条媒体信息，给文件列表展示时长/分辨率。"""
    probe = "ffprobe"
    try:
        result = subprocess.run(
            [probe, "-v", "error", "-show_entries",
             "format=duration,size:stream=codec_type,width,height",
             "-of", "default=noprint_wrappers=1", file_path],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return {}
        info: dict[str, object] = {"streams": []}
        for line in result.stdout.splitlines():
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key in ("duration", "size"):
                info[key] = value
            elif key == "codec_type":
                info["streams"].append({"type": value})
            elif key in ("width", "height") and info["streams"]:
                info["streams"][-1][key] = value
        try:
            info["duration"] = round(float(info.get("duration", 0)), 1)
        except (TypeError, ValueError):
            info["duration"] = 0
        return info
    except Exception:                             # noqa: BLE001 - 探测失败不影响主流程
        return {}


def logs_dir() -> Path:
    return paths.logs_dir()


def project_python() -> str:
    return sys.executable
