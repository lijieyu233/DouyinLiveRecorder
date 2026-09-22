# -*- coding: utf-8 -*-
"""无依赖 HTTP + SSE 接口。

刻意只使用标准库 ``http.server``：

* 上游依赖里没有 web 框架，为了一个本地界面引入 FastAPI/uvicorn 会增加
  安装体积与版本冲突面（用户的运行环境是离线/受限网络，装包代价很高）；
* 单用户本地场景，``ThreadingHTTPServer`` 足够，SSE 推送天然适合「状态流」。

安全边界：

* 只监听 ``127.0.0.1``；
* 除 ``/api/health`` 外都需要 ``X-DYLR-Token`` 请求头或 ``?token=`` 查询串
  （``EventSource`` 无法自定义请求头，因此必须支持查询串）；
* 所有文件类接口都被 :class:`~dylr.files.MediaLibrary` 限制在录制目录内。
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from src.utils import logger

from . import platforms
from .events import EventBus
from .files import MediaLibrary
from .manager import RecorderManager

#: 免鉴权路径（Electron 启动阶段的探活）
PUBLIC_PATHS = {"/api/health", "/"}
SSE_RETRY_MS = 3000

_CONTENT_TYPES = {
    ".mp4": "video/mp4", ".mkv": "video/x-matroska", ".flv": "video/x-flv",
    ".ts": "video/mp2t", ".webm": "video/webm",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".srt": "text/plain; charset=utf-8", ".ass": "text/plain; charset=utf-8",
}


class Route:
    def __init__(self, method: str, pattern: str, handler: Callable) -> None:
        self.method = method
        self.regex = re.compile(f"^{pattern}$")
        self.handler = handler


class _Server(ThreadingHTTPServer):
    """带上服务引用的 HTTP 服务器。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: "ApiServer") -> None:
        super().__init__(address, handler)
        self.service = service


class ApiServer:
    """把 :class:`RecorderManager` 暴露成 HTTP 接口。"""

    def __init__(self, manager: RecorderManager, bus: EventBus | None = None,
                 token: str = "", host: str = "127.0.0.1", port: int = 0,
                 library: MediaLibrary | None = None) -> None:
        self.manager = manager
        self.bus = bus or manager.bus
        self.token = token
        self.host = host
        self.port = port
        self.library = library or MediaLibrary()
        self._httpd: _Server | None = None
        self._thread: threading.Thread | None = None
        self.routes: list[Route] = []
        self._register_routes()

    # ================================================================== 启动
    def start(self) -> int:
        self._httpd = _Server((self.host, self.port), _Handler, self)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="api-server", daemon=True)
        self._thread.start()
        logger.info(f"控制接口已就绪: http://{self.host}:{self.port}")
        return self.port

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    # ================================================================== 路由
    def route(self, method: str, pattern: str):
        def decorator(func: Callable) -> Callable:
            self.routes.append(Route(method, pattern, func))
            return func
        return decorator

    def _register_routes(self) -> None:
        self.route("GET", r"/api/health")(self._health)
        self.route("GET", r"/api/state")(self._state)
        self.route("GET", r"/api/env")(self._env)
        self.route("GET", r"/api/platforms")(self._platforms)
        self.route("GET", r"/api/logs")(self._logs)
        self.route("GET", r"/api/config")(self._get_config)
        self.route("PUT", r"/api/config")(self._put_config)
        self.route("POST", r"/api/config/test-push")(self._test_push)
        self.route("POST", r"/api/tasks")(self._add_task)
        self.route("POST", r"/api/tasks/reorder")(self._reorder_tasks)
        self.route("POST", r"/api/tasks/(?P<task_id>[\w-]+)/action")(self._task_action)
        self.route("PATCH", r"/api/tasks/(?P<task_id>[\w-]+)")(self._update_task)
        self.route("DELETE", r"/api/tasks/(?P<task_id>[\w-]+)")(self._delete_task)
        self.route("POST", r"/api/control")(self._control)
        self.route("POST", r"/api/probe")(self._probe)
        self.route("GET", r"/api/files")(self._files)
        self.route("POST", r"/api/files/delete")(self._delete_files)
        self.route("POST", r"/api/files/reveal")(self._reveal)
        self.route("GET", r"/api/disk")(self._disk)
        self.route("POST", r"/api/quit")(self._quit)

    def dispatch(self, method: str, path: str, query: dict, body: Any) -> tuple[int, Any]:
        """把请求派发到具体处理器（鉴权由 Handler 统一完成）。"""
        for route in self.routes:
            if route.method != method:
                continue
            match = route.regex.match(path)
            if not match:
                continue
            try:
                return route.handler(query=query, body=body, **match.groupdict())
            except ValueError as exc:
                return 400, {"error": str(exc)}
            except PermissionError as exc:
                return 403, {"error": str(exc)}
            except Exception as exc:                   # noqa: BLE001 - 兜底成 500 并记日志
                logger.error(f"接口 {method} {path} 出错: {exc}")
                return 500, {"error": str(exc)}
        return 404, {"error": f"未知接口 {method} {path}"}

    # ================================================================== 处理器
    def _health(self, query: dict, body: Any) -> tuple[int, Any]:
        return 200, {
            "ok": True,
            "name": "DouyinLiveRecorder",
            "version": self.manager.environment()["version"],
            "running": self.manager.running,
            "uptime": self.manager.stats()["uptime"],
        }

    def _state(self, query: dict, body: Any) -> tuple[int, Any]:
        snapshot = self.manager.snapshot()
        snapshot["env"] = self.manager.environment()
        snapshot["config_summary"] = self._config_summary()
        snapshot["push"] = {
            "channels": self.manager.notifier.channels,
            "enabled": self.manager.notifier.enabled,
        }
        return 200, snapshot

    def _env(self, query: dict, body: Any) -> tuple[int, Any]:
        return 200, self.manager.environment()

    def _platforms(self, query: dict, body: Any) -> tuple[int, Any]:
        return 200, {"items": platforms.platform_summary(),
                     "qualities": platforms.quality_options()}

    def _config_summary(self) -> dict[str, Any]:
        from src import paths
        cfg = self.manager.cfg
        return {
            "video_save_type": cfg.text("video_save_type"),
            "video_record_quality": cfg.text("video_record_quality"),
            "delay_default": cfg.number("delay_default"),
            "max_request": cfg.number("max_request"),
            "split_video_by_time": cfg.flag("split_video_by_time"),
            "split_time": cfg.number("split_time"),
            "use_proxy": cfg.flag("use_proxy"),
            "proxy_addr": cfg.text("proxy_addr"),
            "save_path": cfg.text("video_save_path") or str(paths.downloads_dir()).replace("\\", "/"),
            "only_notify": cfg.flag("disable_record"),
            "push_channels": cfg.multi("live_status_push"),
            "folder_by_author": cfg.flag("folder_by_author"),
            "folder_by_time": cfg.flag("folder_by_time"),
            "converts_to_mp4": cfg.flag("converts_to_mp4"),
        }

    def _logs(self, query: dict, body: Any) -> tuple[int, Any]:
        limit = int(query.get("limit", ["300"])[0])
        level = query.get("level", [""])[0].upper()
        events = self.bus.history(limit=2000, types=["log"])
        if level:
            events = [e for e in events if self._level_rank(e.data.get("level", "")) >=
                      self._level_rank(level)]
        return 200, {"items": [e.as_dict() for e in events[-limit:]]}

    @staticmethod
    def _level_rank(level: str) -> int:
        return {"DEBUG": 0, "INFO": 1, "SUCCESS": 2, "WARNING": 3, "ERROR": 4, "CRITICAL": 5}.get(
            level.upper(), 1)

    def _get_config(self, query: dict, body: Any) -> tuple[int, Any]:
        return 200, self.manager.cfg.snapshot()

    def _put_config(self, query: dict, body: Any) -> tuple[int, Any]:
        from .config import is_masked
        from .notify import Notifier
        patch = (body or {}).get("patch") or {}
        if not isinstance(patch, dict) or not patch:
            raise ValueError("patch 不能为空")
        # 脱敏值原样回传时不要覆盖真实密钥
        patch = {k: v for k, v in patch.items() if not (isinstance(v, str) and is_masked(v))}
        if not patch:
            return 200, {"changed": [], "restart_required": [], "values": {}}
        changed = self.manager.cfg.save(patch)
        restart = self.manager.config_changed(changed)
        if changed:
            self.manager.notifier = Notifier(self.manager.cfg)
            self.manager.reload_tasks()
            self.bus.publish("notice", {
                "level": "info",
                "text": f"配置已保存（{len(changed)} 项）" + ("，部分改动需重启任务生效" if restart else ""),
            })
        return 200, {
            "changed": changed,
            "restart_required": restart,
            "values": {slug: self.manager.cfg.get(slug) for slug in changed},
        }

    def _test_push(self, query: dict, body: Any) -> tuple[int, Any]:
        channel = (body or {}).get("channel", "")
        if not channel:
            raise ValueError("请指定推送渠道")
        result = self.manager.notifier.test(channel)
        return 200, {"channel": result.channel, "ok": result.ok, "message": result.message}

    # -- 任务 ---------------------------------------------------------------
    def _task_by_id(self, task_id: str):
        for task in self.manager.snapshot_tasks():
            if task.id == task_id:
                return task
        return None

    def _add_task(self, query: dict, body: Any) -> tuple[int, Any]:
        body = body or {}
        url = (body.get("url") or "").strip()
        quality = body.get("quality") or None
        ok, message, task = self.manager.add_task(url, quality)
        if not ok:
            return 400, {"error": message}
        return 200, {"task": task.to_dict() if task else None, "message": message}

    def _update_task(self, task_id: str, query: dict, body: Any) -> tuple[int, Any]:
        task = self._task_by_id(task_id)
        if task is None:
            return 404, {"error": "任务不存在"}
        body = body or {}
        updated = self.manager.update_task(
            task.url,
            quality=body.get("quality"),
            label=body.get("label"),
            enabled=body.get("enabled"),
        )
        return 200, {"task": updated.to_dict() if updated else None}

    def _delete_task(self, task_id: str, query: dict, body: Any) -> tuple[int, Any]:
        task = self._task_by_id(task_id)
        if task is None:
            return 404, {"error": "任务不存在"}
        self.manager.remove_task(task.url)
        return 200, {"ok": True}

    def _reorder_tasks(self, query: dict, body: Any) -> tuple[int, Any]:
        urls = (body or {}).get("urls") or []
        tasks = self.manager.reorder_tasks(urls)
        return 200, {"tasks": [t.to_dict() for t in tasks]}

    def _task_action(self, task_id: str, query: dict, body: Any) -> tuple[int, Any]:
        task = self._task_by_id(task_id)
        if task is None:
            return 404, {"error": "任务不存在"}
        action = (body or {}).get("action", "")
        if action == "start":
            self.manager.start_task(task.url)
        elif action == "stop":
            self.manager.stop_task(task.url)
        elif action == "restart":
            self.manager.restart_task(task.url)
        else:
            raise ValueError(f"未知操作 {action}")
        return 200, {"ok": True, "task": task.to_dict()}

    def _control(self, query: dict, body: Any) -> tuple[int, Any]:
        body = body or {}
        if "running" in body:
            self.manager.set_running(bool(body["running"]))
        elif body.get("action") == "restart_all":
            self.manager.stop_all("重启全部任务")
            self.manager.start_all()
        return 200, {"running": self.manager.running, "stats": self.manager.stats()}

    def _probe(self, query: dict, body: Any) -> tuple[int, Any]:
        """添加任务前的预检：识别平台、给出提示，不发起真实请求。"""
        url = ((body or {}).get("url") or "").strip()
        if not url:
            raise ValueError("请输入地址")
        normalized = platforms.normalize_url(url)
        spec = platforms.resolve(normalized)
        supported = platforms.matches_supported_host(normalized)
        cfg = self.manager.cfg
        warnings: list[str] = []
        if spec is None:
            warnings.append("无法识别该地址所属平台，添加后会被标记为异常")
        else:
            if spec.needs_proxy and not (self.manager.global_proxy or
                                         (cfg.flag("use_proxy") and cfg.text("proxy_addr").strip())):
                warnings.append(f"{spec.name} 需要代理，请先在设置里填写代理地址")
            if spec.cookie_key and not cfg.has_value(f"cookie_{spec.cookie_key}"):
                warnings.append(f"{spec.name} 建议填写 Cookie，否则可能取不到流")
        return 200, {
            "url": normalized,
            "supported": supported,
            "platform": spec.name if spec else "",
            "platform_key": spec.key if spec else "",
            "overseas": bool(spec and spec.overseas),
            "audio_only": bool(spec and spec.audio_only),
            "custom": bool(spec and spec.custom),
            "warnings": warnings,
            "exists": any(t.url == normalized for t in self.manager.snapshot_tasks()),
        }

    # -- 文件 ---------------------------------------------------------------
    def _files(self, query: dict, body: Any) -> tuple[int, Any]:
        return 200, self.library.scan()

    def _delete_files(self, query: dict, body: Any) -> tuple[int, Any]:
        items = (body or {}).get("paths") or []
        if not items:
            raise ValueError("请选择要删除的文件")
        result = self.library.delete(items)
        if result["removed"]:
            self.bus.publish("files", {"removed": result["removed"]})
        return 200, result

    def _reveal(self, query: dict, body: Any) -> tuple[int, Any]:
        path = (body or {}).get("path")
        return 200, {"ok": self.library.reveal(path)}

    def _disk(self, query: dict, body: Any) -> tuple[int, Any]:
        return 200, self.library.disk_usage()

    def _quit(self, query: dict, body: Any) -> tuple[int, Any]:
        threading.Thread(target=self._shutdown_soon, daemon=True).start()
        return 200, {"ok": True}

    def _shutdown_soon(self) -> None:
        time.sleep(0.3)
        self.manager.exit_recording = True
        self.manager.shutdown()
        if self._httpd:
            self._httpd.shutdown()

    # ================================================================== SSE
    def stream_snapshot(self, since: int = 0) -> list[dict]:
        """断线重连时回放的历史事件。"""
        return [e.as_dict() for e in self.bus.history(limit=80, since_seq=since or None)]


class _Handler(BaseHTTPRequestHandler):
    """请求处理：JSON 接口 + SSE 长连接 + 媒体分片。"""

    protocol_version = "HTTP/1.1"
    server_version = "dylr-api"

    # ------------------------------------------------------------ 入口
    def do_GET(self) -> None:                     # noqa: N802
        self._handle("GET")

    def do_POST(self) -> None:                    # noqa: N802
        self._handle("POST")

    def do_PUT(self) -> None:                     # noqa: N802
        self._handle("PUT")

    def do_PATCH(self) -> None:                   # noqa: N802
        self._handle("PATCH")

    def do_DELETE(self) -> None:                  # noqa: N802
        self._handle("DELETE")

    def do_OPTIONS(self) -> None:                 # noqa: N802
        self._cors()
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,X-DYLR-Token")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ------------------------------------------------------------ 通用处理
    def _handle(self, method: str) -> None:
        service: ApiServer = self.server.service          # type: ignore[attr-defined]
        parsed = urlparse(self.path)
        path = unquote(parsed.path.rstrip("/") or "/")
        query = parse_qs(parsed.query)
        if service.token and path not in PUBLIC_PATHS and not self._check_token(service, query):
            self._send_json(401, {"error": "缺少或错误的访问令牌"})
            return

        if path == "/api/events" and method == "GET":
            self._stream_events(service, query)
            return
        if path == "/api/media" and method == "GET":
            self._serve_media(service, query)
            return

        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        status, payload = service.dispatch(method, path, self._query_flat(query), body)
        self._send_json(status, payload)

    @staticmethod
    def _query_flat(query: dict) -> dict:
        return query

    def _check_token(self, service: ApiServer, query: dict) -> bool:
        header = self.headers.get("X-DYLR-Token") or ""
        candidate = header or (query.get("token", [""])[0])
        return bool(candidate) and candidate == service.token

    def _read_body(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON: {exc}") from exc

    # ------------------------------------------------------------ 响应
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")

    def _send_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------ SSE
    def _stream_events(self, service: ApiServer, query: dict) -> None:
        try:
            since = int(query.get("since", ["0"])[0])
        except ValueError:
            since = 0
        subscription = service.bus.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        try:
            self._sse_write(f"retry: {SSE_RETRY_MS}\n\n")
            # 回放断线期间的事件，界面重连后不会丢状态
            backlog = service.bus.history(limit=80, since_seq=since or None)
            for event in backlog:
                self._sse_event(event)
            if not backlog:
                self._sse_write(": ready\n\n")

            while True:
                try:
                    event = subscription.get(timeout=20)
                except queue.Empty:
                    self._sse_write(": ping\n\n")
                    continue
                self._sse_event(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            service.bus.unsubscribe(subscription)

    def _sse_event(self, event) -> None:
        payload = json.dumps(event.data, ensure_ascii=False, default=str)
        self._sse_write(f"id: {event.seq}\nevent: {event.type}\ndata: {payload}\n\n")

    def _sse_write(self, text: str) -> None:
        self.wfile.write(text.encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------ 媒体
    def _serve_media(self, service: ApiServer, query: dict) -> None:
        raw = query.get("path", [""])[0]
        if not raw:
            self._send_json(400, {"error": "缺少 path 参数"})
            return
        try:
            target = service.library.resolve(raw)
        except PermissionError:
            self._send_json(403, {"error": "路径越界"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "文件不存在"})
            return

        size = target.stat().st_size
        content_type = _CONTENT_TYPES.get(target.suffix.lower()) \
            or mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        start, end = 0, size - 1
        range_header = self.headers.get("Range") or ""
        partial = False
        if range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",")[0].strip()
            head, _, tail = spec.partition("-")
            try:
                if head:
                    start = int(head)
                    end = int(tail) if tail else size - 1
                elif tail:
                    start = max(0, size - int(tail))
                end = min(end, size - 1)
                partial = True
            except ValueError:
                start, end, partial = 0, size - 1, False

        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self._cors()
        self.end_headers()

        try:
            for chunk in service.library.iter_range(str(target), start, end):
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ------------------------------------------------------------ 日志
    def log_message(self, fmt: str, *args) -> None:       # noqa: A003
        logger.debug("api " + fmt % args)


def make_token() -> str:
    import secrets
    return secrets.token_urlsafe(24)
