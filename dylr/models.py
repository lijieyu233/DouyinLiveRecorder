# -*- coding: utf-8 -*-
"""任务与流数据模型。"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    """单个直播间的状态机。"""

    IDLE = "idle"          # 刚创建，还没检查过
    CHECKING = "checking"  # 正在请求接口
    WAITING = "waiting"    # 已确认未开播，等待中
    LIVE = "live"          # 已开播（仅推送模式）
    RECORDING = "recording"
    DISABLED = "disabled"  # 用户在列表里注释掉了
    ERROR = "error"
    STOPPED = "stopped"    # 全局停止 / 线程退出

    @property
    def label(self) -> str:
        return _STATE_LABELS.get(self, self.value)


_STATE_LABELS = {
    TaskState.IDLE: "待检查",
    TaskState.CHECKING: "检测中",
    TaskState.WAITING: "等待开播",
    TaskState.LIVE: "直播中",
    TaskState.RECORDING: "录制中",
    TaskState.DISABLED: "已暂停",
    TaskState.ERROR: "异常",
    TaskState.STOPPED: "已停止",
}

#: 画质选项，顺序与上游 stream.get_quality_index 一致
QUALITIES = ("原画", "蓝光", "超清", "高清", "标清", "流畅")


def stable_id(url: str) -> str:
    """由 URL 派生稳定短 id，界面刷新时不会跳位。"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


@dataclass
class Task:
    """一个直播间监控任务。

    持久化形态来自 ``URL_config.ini`` 的一行：
    ``[#][画质,]URL[,主播: 名称]``
    """

    url: str
    quality: str = "原画"
    label: str = ""
    enabled: bool = True
    raw_line: str = ""
    #: 画质是否在 URL 文件里显式写过。没写过的房间跟随全局默认画质，
    #: 回写时也不主动加画质前缀，避免把用户文件改脏。
    quality_explicit: bool = False

    # ---- 运行时字段（不入库） ----
    platform: str = ""
    anchor: str = ""
    state: TaskState = TaskState.IDLE
    message: str = ""
    title: str = ""
    quality_effective: str = ""
    started_at: float | None = None
    last_check: float | None = None
    next_check_at: float | None = None
    last_error: str = ""
    error_count: int = 0
    recorded_bytes: int = 0
    current_file: str = ""
    segments: list[str] = field(default_factory=list)
    order: int = 0

    @property
    def id(self) -> str:
        return stable_id(self.url)

    @property
    def display_name(self) -> str:
        if self.anchor:
            return self.anchor
        if self.label:
            return self.label.replace("主播:", "").strip() or self.url
        return self.url

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return max(0.0, time.time() - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "quality": self.quality,
            "quality_explicit": self.quality_explicit,
            "label": self.label,
            "enabled": self.enabled,
            "order": self.order,
            "platform": self.platform,
            "anchor": self.anchor,
            "display_name": self.display_name,
            "state": self.state.value,
            "state_label": self.state.label,
            "message": self.message,
            "title": self.title,
            "quality_effective": self.quality_effective,
            "started_at": self.started_at,
            "last_check": self.last_check,
            "next_check_at": self.next_check_at,
            "last_error": self.last_error,
            "error_count": self.error_count,
            "recorded_bytes": self.recorded_bytes,
            "current_file": self.current_file,
            "segments": list(self.segments),
            "elapsed": round(self.elapsed, 1),
        }


@dataclass
class FlowInfo:
    """平台探测结果：一个直播间当前的可用拉流信息。"""

    anchor_name: str = ""
    is_live: bool = False
    record_url: str = ""
    m3u8_url: str = ""
    flv_url: str = ""
    title: str = ""
    quality: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_probe(cls, data: dict[str, Any] | None) -> "FlowInfo":
        data = data or {}
        known = {"anchor_name", "is_live", "record_url", "m3u8_url", "flv_url", "title", "quality"}
        return cls(
            anchor_name=str(data.get("anchor_name") or ""),
            is_live=bool(data.get("is_live")),
            record_url=str(data.get("record_url") or ""),
            m3u8_url=str(data.get("m3u8_url") or ""),
            flv_url=str(data.get("flv_url") or ""),
            title=str(data.get("title") or ""),
            quality=str(data.get("quality") or ""),
            extra={k: v for k, v in data.items() if k not in known},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchor_name": self.anchor_name,
            "is_live": self.is_live,
            "record_url": self.record_url,
            "m3u8_url": self.m3u8_url,
            "flv_url": self.flv_url,
            "title": self.title,
            "quality": self.quality,
        }
