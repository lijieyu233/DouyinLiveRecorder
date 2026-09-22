# -*- coding: utf-8 -*-
"""``config/URL_config.ini`` 的解析与回写。

文件形态（沿用上游，兼容旧文件）::

    https://live.douyin.com/123
    超清,https://live.douyin.com/456
    #https://live.douyin.com/789,主播: 某某      ← 行首 # 表示暂停
    原画,https://live.bilibili.com/1,主播: 某某

上游把这段逻辑写在 ``main.py`` 里，与「主循环、线程创建、配置读取」混在同一个
``try`` 块中（约 200 行），很难单独测试。这里拆成 :class:`TaskStore`：
解析、去重、修正、回写各管一件事，并且把「为什么这行被跳过」作为结构化结果
返回，界面可以直接拿来提示用户，而不是只往终端打一行警告。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from src import paths
from src.utils import logger

from . import platforms
from .models import QUALITIES, Task

#: 少于这个长度的行直接忽略（上游行为，用于容纳标题、空行等杂项）
MIN_LINE_LENGTH = 18


@dataclass
class LineIssue:
    """一行地址被处理时的异常说明。"""

    line: str
    reason: str
    level: str = "warning"     # warning | info


class TaskStore:
    """URL 列表的读写门面。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else paths.url_config_file()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 读
    def load(self) -> tuple[list[Task], list[LineIssue]]:
        """解析文件并返回任务列表；必要时就地修正文件内容。"""
        with self._lock:
            raw_lines = self._read()
            tasks: list[Task] = []
            issues: list[LineIssue] = []
            seen_urls: set[str] = set()
            seen_lines: set[str] = set()
            line_fixes: dict[int, str] = {}

            for index, origin in enumerate(raw_lines):
                if origin in seen_lines:
                    line_fixes[index] = ""
                    continue
                seen_lines.add(origin)

                stripped = origin.strip()
                if len(stripped) < MIN_LINE_LENGTH:
                    continue

                enabled = not stripped.startswith("#")
                body = stripped.lstrip("#").strip()

                # 上游会用同一行的重复 "主播: " 做自我修正
                if body.count("主播:") > 1:
                    head, _, tail = body.partition("主播:")
                    body = f"{head.rstrip().rstrip(',，')}主播: {tail.strip()}"
                    line_fixes[index] = ("#" if not enabled else "") + body
                    issues.append(LineIssue(origin, "主播名重复，已自动修正", "info"))

                parts = re.split("[,，]", body)
                if len(parts) == 1:
                    url, quality, label = parts[0], None, ""
                elif len(parts) == 2:
                    if platforms.looks_like_url(parts[0]):
                        quality, url, label = None, parts[0], parts[1]
                    else:
                        quality, url, label = parts[0], parts[1], ""
                else:
                    quality, url, label = parts[0], parts[1], ",".join(parts[2:])

                explicit_quality = bool(quality) and quality.strip() in QUALITIES

                url = url.strip()
                if url in seen_urls:
                    line_fixes[index] = ""
                    issues.append(LineIssue(origin, "与前面的地址重复，已移除", "info"))
                    continue
                seen_urls.add(url)

                if not platforms.matches_supported_host(url):
                    if enabled:
                        line_fixes[index] = "#" + stripped
                    issues.append(LineIssue(origin, "无法识别的直播地址，已跳过", "warning"))
                    continue

                normalized = platforms.normalize_url(url)
                if normalized != url:
                    line_fixes[index] = self._render(
                        normalized, quality, label, enabled, explicit_quality)

                tasks.append(Task(
                    url=normalized,
                    quality=_clean_quality(quality) if explicit_quality else "",
                    quality_explicit=explicit_quality,
                    label=label.strip(),
                    enabled=enabled,
                    raw_line=origin,
                    order=len(tasks),
                ))

            if line_fixes:
                self._apply_fixes(raw_lines, line_fixes)
            return tasks, issues

    def _read(self) -> list[str]:
        if not self.path.exists():
            return []
        with open(self.path, "r", encoding="utf-8-sig", errors="ignore") as fh:
            return fh.read().splitlines()

    def _apply_fixes(self, raw_lines: list[str], fixes: dict[int, str]) -> None:
        out: list[str] = []
        for index, line in enumerate(raw_lines):
            replacement = fixes.get(index)
            if replacement is None:
                out.append(line)
            elif replacement == "":
                continue          # 删除该行（重复项）
            elif replacement != line:
                out.append(replacement)
            else:
                out.append(line)
        self._write(out)

    @staticmethod
    def _render(url: str, quality: str | None, label: str, enabled: bool,
                explicit_quality: bool = True) -> str:
        """产出文件里的一行。

        只有「用户显式设过画质」的房间才写画质前缀，其余跟随全局默认，
        这样回写不会把用户的 URL 文件改脏。
        """
        pieces = ([_clean_quality(quality)] if explicit_quality else []) + [url]
        line = ",".join(pieces)
        if label:
            line += f",{label}"
        return ("#" if not enabled else "") + line

    # ------------------------------------------------------------ 写
    def save(self, tasks: list[Task]) -> None:
        """全量写回（保持传入顺序）。"""
        with self._lock:
            lines = [
                self._render(t.url, t.quality, t.label, t.enabled, t.quality_explicit)
                for t in tasks
            ]
            self._write(lines)

    def _write(self, lines: list[str]) -> None:
        paths.ensure_dir(self.path.parent)
        text = "\n".join(line.rstrip("\r\n") for line in lines)
        if text and not text.endswith("\n"):
            text += "\n"
        with open(self.path, "w", encoding="utf-8-sig", newline="\n") as fh:
            fh.write(text)

    # ------------------------------------------------------------ 增删改
    def append(self, url: str, quality: str | None = None, label: str = "") -> tuple[list[Task], Task | None, str]:
        """追加一条地址。返回 (最新列表, 新任务, 提示语)。"""
        tasks, _ = self.load()
        normalized = platforms.normalize_url(url.strip())
        if any(t.url == normalized for t in tasks):
            return tasks, None, "该地址已在列表中"
        explicit = bool(quality) and str(quality).strip() in QUALITIES
        task = Task(url=normalized,
                    quality=_clean_quality(quality) if explicit else "",
                    quality_explicit=explicit,
                    label=label.strip(),
                    order=len(tasks))
        tasks.append(task)
        self.save(tasks)
        return tasks, task, ""

    def update(self, url: str, quality: str | None = None, label: str | None = None,
               enabled: bool | None = None) -> list[Task]:
        tasks, _ = self.load()
        for task in tasks:
            if task.url != url:
                continue
            if quality is not None:
                task.quality = _clean_quality(quality)
                task.quality_explicit = True
            if label is not None:
                task.label = label.strip()
            if enabled is not None:
                task.enabled = enabled
        self.save(tasks)
        return tasks

    def remove(self, url: str) -> list[Task]:
        tasks, _ = self.load()
        tasks = [t for t in tasks if t.url != url]
        self.save(tasks)
        return tasks

    def reorder(self, urls: list[str]) -> list[Task]:
        tasks, _ = self.load()
        index = {t.url: t for t in tasks}
        ordered = [index[u] for u in urls if u in index]
        ordered += [t for t in tasks if t.url not in set(urls)]
        for position, task in enumerate(ordered):
            task.order = position
        self.save(ordered)
        return ordered

    def remember_anchor(self, url: str, anchor_name: str) -> bool:
        """把首次探测到的主播名回填到文件（等价上游的 need_update_line_list）。

        返回是否真的改动了文件。上游只在「这行还没写主播名」时才回填，
        避免每轮循环都重写文件。
        """
        if not anchor_name:
            return False
        with self._lock:
            lines = self._read()
            changed = False
            for index, line in enumerate(lines):
                stripped = line.strip()
                if not stripped or len(stripped) < MIN_LINE_LENGTH:
                    continue
                enabled = not stripped.startswith("#")
                body = stripped.lstrip("#").strip()
                parts = re.split("[,，]", body)
                if len(parts) < 2 or parts[1].strip() != url:
                    continue
                if body.count("主播:") or (len(parts) >= 3 and parts[2].strip()):
                    return False
                explicit = parts[0].strip() in QUALITIES
                lines[index] = self._render(url, parts[0] if explicit else None,
                                            f"主播: {anchor_name}", enabled, explicit)
                changed = True
                break
            if changed:
                self._write(lines)
            return changed

    def count_enabled(self) -> int:
        tasks, _ = self.load()
        return sum(1 for t in tasks if t.enabled)


def _clean_quality(quality: str | None) -> str:
    if not quality:
        return QUALITIES[0]
    text = str(quality).strip()
    return text if text in QUALITIES else QUALITIES[0]
