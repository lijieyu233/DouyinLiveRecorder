# -*- coding: utf-8 -*-
"""录制产物浏览：给桌面端的「录制文件」页提供数据与操作。

上游没有文件管理，用户只能自己去 ``downloads/`` 里翻。这里补上：

* 按「平台 / 主播」两级聚合，带体积与时间；
* 路径安全校验（所有请求都被限制在录制根目录内，杜绝 ``../`` 穿越）；
* 供 ``<video>`` 直接播放的 Range 分片读取。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from src import paths
from src.utils import logger

VIDEO_EXT = {".mp4", ".mkv", ".flv", ".ts", ".avi", ".mov", ".webm"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac"}
SUB_EXT = {".srt", ".ass", ".vtt"}
PLAYABLE_EXT = VIDEO_EXT | AUDIO_EXT

#: 这些后缀是转码中间产物，列表里也显示，但打上标记
_PARTIAL_SUFFIX = "_%03d"


def media_kind(suffix: str) -> str:
    suffix = suffix.lower()
    if suffix in VIDEO_EXT:
        return "video"
    if suffix in AUDIO_EXT:
        return "audio"
    if suffix in SUB_EXT:
        return "subtitle"
    return "other"


@dataclass
class FileEntry:
    name: str
    path: str
    rel: str
    size: int
    mtime: float
    kind: str
    platform: str
    author: str
    is_dir: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "rel": self.rel,
            "size": self.size,
            "size_text": human_size(self.size),
            "mtime": self.mtime,
            "kind": self.kind,
            "platform": self.platform,
            "author": self.author,
            "is_dir": self.is_dir,
            "playable": self.kind in ("video", "audio"),
        }


def human_size(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


class MediaLibrary:
    """录制目录的只读视图 + 少量操作（删除 / 在资源管理器里打开）。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else paths.downloads_dir()

    # ------------------------------------------------------------ 浏览
    def scan(self) -> dict:
        """扫描整个录制目录，按平台/主播聚合。"""
        if not self.root.exists():
            return {"root": str(self.root), "platforms": [], "total_size": 0, "count": 0,
                    "missing": True}

        platforms: dict[str, dict] = {}
        total_size = 0
        count = 0
        for path in self._walk():
            try:
                stat = path.stat()
            except OSError:
                continue
            rel = path.relative_to(self.root)
            parts = rel.parts
            platform = parts[0] if len(parts) > 1 else "未分类"
            author = parts[1] if len(parts) > 2 else ""
            entry = FileEntry(
                name=path.name,
                path=str(path),
                rel=str(rel).replace("\\", "/"),
                size=stat.st_size,
                mtime=stat.st_mtime,
                kind=media_kind(path.suffix),
                platform=platform,
                author=author,
            )
            bucket = platforms.setdefault(platform, {"name": platform, "authors": {}, "size": 0, "count": 0})
            bucket["size"] += stat.st_size
            bucket["count"] += 1
            bucket["authors"].setdefault(author or "（未分组）", []).append(entry)
            total_size += stat.st_size
            count += 1

        result = []
        for name, bucket in sorted(platforms.items()):
            authors = [
                {"name": author, "files": [f.to_dict() for f in sorted(files, key=lambda x: x.mtime, reverse=True)]}
                for author, files in sorted(bucket["authors"].items())
            ]
            result.append({
                "name": name,
                "size": bucket["size"],
                "size_text": human_size(bucket["size"]),
                "count": bucket["count"],
                "authors": authors,
            })
        return {
            "root": str(self.root).replace("\\", "/"),
            "platforms": result,
            "total_size": total_size,
            "total_size_text": human_size(total_size),
            "count": count,
            "missing": False,
        }

    def _walk(self) -> Iterator[Path]:
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                if name.startswith("."):
                    continue
                yield Path(dirpath) / name

    # ------------------------------------------------------------ 安全
    def resolve(self, path: str | Path) -> Path:
        """把请求路径限制在录制根目录内。"""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        root = self.root.resolve()
        if root != resolved and root not in resolved.parents:
            raise PermissionError("路径越界")
        return resolved

    # ------------------------------------------------------------ 操作
    def delete(self, paths_: list[str]) -> dict:
        """删除文件（不做递归删目录，避免误伤）。"""
        removed, failed = [], []
        for item in paths_:
            try:
                target = self.resolve(item)
                if target.is_dir():
                    failed.append({"path": item, "error": "暂不支持删除目录"})
                    continue
                size = target.stat().st_size
                target.unlink()
                removed.append({"path": item, "size": size})
            except Exception as exc:                   # noqa: BLE001
                logger.warning(f"删除失败 {item}: {exc}")
                failed.append({"path": item, "error": str(exc)})
        return {"removed": removed, "failed": failed}

    def reveal(self, path: str | None = None) -> bool:
        """在系统文件管理器里定位该文件/目录。"""
        try:
            target = self.resolve(path) if path else self.root
        except PermissionError:
            return False
        if not target.exists():
            target = self.root
        try:
            if sys.platform.startswith("win"):
                if target.is_file():
                    subprocess.Popen(["explorer", "/select,", str(target)])
                else:
                    os.startfile(str(target))          # noqa: S606 - 桌面端预期行为
            elif sys.platform == "darwin":
                args = ["open", "-R", str(target)] if target.is_file() else ["open", str(target)]
                subprocess.Popen(args)
            else:
                subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
            return True
        except Exception as exc:                       # noqa: BLE001
            logger.warning(f"打开文件位置失败: {exc}")
            return False

    # ------------------------------------------------------------ 流式播放
    def iter_range(self, path: str, start: int, end: int, chunk: int = 1024 * 256) -> Iterator[bytes]:
        """按 Range 读取，供 ``<video>`` 拖动进度条。"""
        target = self.resolve(path)
        with open(target, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                data = fh.read(min(chunk, remaining))
                if not data:
                    return
                remaining -= len(data)
                yield data

    def disk_usage(self) -> dict:
        try:
            usage = shutil.disk_usage(str(self.root if self.root.exists() else paths.project_root()))
            used_by_library = sum(f.stat().st_size for f in self._walk()) if self.root.exists() else 0
            return {
                "total": round(usage.total / 1024 ** 3, 2),
                "free": round(usage.free / 1024 ** 3, 2),
                "used": round(usage.used / 1024 ** 3, 2),
                "library": round(used_by_library / 1024 ** 3, 2),
            }
        except OSError:
            return {}
