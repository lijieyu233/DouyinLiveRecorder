# -*- coding: utf-8 -*-
"""进程内事件总线：录制器 → 管理器 → API/UI 的单向数据流。

设计目标
--------
* 录制线程不关心谁在消费事件，只管 ``publish``；
* 订阅者用各自的 :class:`queue.Queue`，互不阻塞（慢消费者丢最旧事件，不拖垮录制）；
* 保留一段环形历史，界面刷新/新订阅者能立刻拿到上下文。

事件类型
--------
``log``      日志行（由 loguru sink 桥接而来）
``task``     单个任务状态变化（新增/更新/删除）
``control``  全局控制状态（运行/停止/暂停）
``stats``    周期统计（帧率、磁盘、瞬时错误数、网络并发）
``files``    录制产物变化
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# 事件类型常量，避免各处硬编码字符串拼错
LOG = "log"
TASK = "task"
CONTROL = "control"
STATS = "stats"
FILES = "files"
NOTICE = "notice"


@dataclass(frozen=True)
class Event:
    """一条不可变事件。"""

    type: str
    data: dict[str, Any]
    ts: float
    seq: int

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data, "ts": self.ts, "seq": self.seq}


class EventBus:
    """线程安全的发布/订阅总线，带环形历史。"""

    def __init__(self, history_size: int = 600) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[Event]] = []
        self._history: list[Event] = []
        self._history_size = history_size
        self._seq = itertools.count(1)

    # ---------------------------------------------------------------- 发布
    def publish(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        event = Event(
            type=event_type,
            data=data or {},
            ts=time.time(),
            seq=next(self._seq),
        )
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_size:
                del self._history[: len(self._history) - self._history_size]
            subscribers = list(self._subscribers)

        for q in subscribers:
            try:
                q.put_nowait(event)
            except queue.Full:
                # 慢消费者：丢一条最旧的，保证最新状态一定到达
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass
        return event

    # -------------------------------------------------------------- 订阅
    def subscribe(self, maxsize: int = 512) -> queue.Queue[Event]:
        q: queue.Queue[Event] = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue[Event]) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # -------------------------------------------------------------- 历史
    def history(
        self,
        limit: int = 200,
        types: Iterable[str] | None = None,
        since_seq: int | None = None,
    ) -> list[Event]:
        wanted = set(types) if types else None
        with self._lock:
            items = list(self._history)
        if wanted is not None:
            items = [e for e in items if e.type in wanted]
        if since_seq is not None:
            items = [e for e in items if e.seq > since_seq]
        return items[-limit:]


def bridge_loguru(bus: EventBus, level: str = "DEBUG") -> int:
    """把 loguru 的输出桥接到事件总线，供界面实时显示。

    返回 sink id，便于测试中移除。
    """
    from loguru import logger

    def _sink(message) -> None:  # pragma: no cover - 运行期回调
        record = message.record
        bus.publish(
            LOG,
            {
                "time": record["time"].strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "level": record["level"].name,
                "name": record["name"],
                "text": record["message"],
            },
        )

    return logger.add(_sink, level=level, format="{message}", enqueue=False)
