# -*- coding: utf-8 -*-
"""直播状态推送。

上游 ``push_message`` 把「七种渠道的调用参数」硬编码在 ``main.py`` 里，并且直接
读取十几个全局变量。这里把渠道收敛成一张表，参数从 :class:`~dylr.config.AppConfig`
取，渠道是否启用由 ``live_status_push`` 决定。
"""
from __future__ import annotations

import datetime
import threading
from dataclasses import dataclass
from typing import Any, Callable

from src.utils import logger

from msg_push import bark, dingtalk, ntfy, pushplus, send_email, tg_bot, xizhi

#: 渠道名 → 是否可用（缺参数时给出可读提示）
CHANNELS = ("微信", "钉钉", "TG", "邮箱", "BARK", "NTFY", "PUSHPLUS")

DEFAULT_TITLE = "直播间状态更新通知"
DEFAULT_BEGIN = "直播间状态更新：[直播间名称] 正在直播中，时间：[时间]"
DEFAULT_OVER = "直播间状态更新：[直播间名称] 直播已结束！时间：[时间]"


@dataclass
class PushResult:
    channel: str
    ok: bool
    message: str = ""


def render_template(template: str, record_name: str, when: str | None = None) -> str:
    """替换 [直播间名称] / [时间] 占位符。"""
    when = when or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (template.replace("[直播间名称]", record_name)
            .replace("[时间]", when)
            .replace(r"\n", "\n"))


class Notifier:
    """按配置向各渠道推送直播状态。"""

    def __init__(self, cfg, on_result: Callable[[PushResult], None] | None = None) -> None:
        self.cfg = cfg
        self.on_result = on_result

    # ------------------------------------------------------------ 状态
    @property
    def channels(self) -> list[str]:
        return [c for c in self.cfg.multi("live_status_push") if c in CHANNELS]

    @property
    def enabled(self) -> bool:
        return bool(self.channels)

    @property
    def title(self) -> str:
        return self.cfg.text("push_message_title").strip() or DEFAULT_TITLE

    def missing_fields(self, channel: str) -> list[str]:
        """渠道还缺哪些必填项（界面用来提示「去填写」）。"""
        required = {
            "微信": ("xizhi_api_url",),
            "钉钉": ("dingtalk_api_url",),
            "TG": ("tg_token", "tg_chat_id"),
            "邮箱": ("email_host", "login_email", "email_password", "to_email"),
            "BARK": ("bark_msg_api",),
            "NTFY": ("ntfy_api",),
            "PUSHPLUS": ("pushplus_token",),
        }.get(channel, ())
        return [slug for slug in required if not self.cfg.has_value(slug)]

    # ------------------------------------------------------------ 发送
    def _call(self, channel: str, content: str, live_url: str) -> Any:
        """调用对应渠道；返回上游风格的 {"success": [...], "error": [...]}。"""
        cfg = self.cfg
        title = self.title
        if channel == "微信":
            return xizhi(cfg.text("xizhi_api_url"), title, content)
        if channel == "钉钉":
            return dingtalk(
                cfg.text("dingtalk_api_url"), content,
                cfg.text("dingtalk_phone_num"), cfg.flag("dingtalk_is_atall"),
            )
        if channel == "TG":
            return tg_bot(cfg.text("tg_chat_id"), cfg.text("tg_token"), content)
        if channel == "邮箱":
            return send_email(
                cfg.text("email_host"), cfg.text("login_email"), cfg.text("email_password"),
                cfg.text("sender_email"), cfg.text("sender_name"), cfg.text("to_email"),
                title, content, cfg.text("smtp_port"), cfg.flag("open_smtp_ssl"),
            )
        if channel == "BARK":
            return bark(
                cfg.text("bark_msg_api"), title=title, content=content,
                level=cfg.text("bark_msg_level") or "active",
                sound=cfg.text("bark_msg_ring"),
            )
        if channel == "NTFY":
            return ntfy(
                cfg.text("ntfy_api"), title=title, content=content,
                tags=cfg.text("ntfy_tags") or "tada",
                action_url=live_url,
                email=cfg.text("ntfy_email"),
            )
        if channel == "PUSHPLUS":
            return pushplus(cfg.text("pushplus_token"), title, content)
        raise ValueError(f"未知推送渠道: {channel}")

    def send(self, content: str, live_url: str = "", channels: list[str] | None = None) -> list[PushResult]:
        """同步发送（调用方通常放在线程里）。"""
        targets = [c for c in (channels if channels is not None else self.channels) if c in CHANNELS]
        results: list[PushResult] = []
        for channel in targets:
            missing = self.missing_fields(channel)
            if missing:
                result = PushResult(channel, False, f"缺少配置：{'、'.join(missing)}")
            else:
                try:
                    raw = self._call(channel, content, live_url)
                    success = len(raw.get("success", [])) if isinstance(raw, dict) else 1
                    error = len(raw.get("error", [])) if isinstance(raw, dict) else 0
                    result = PushResult(channel, error == 0, f"成功 {success}，失败 {error}")
                except Exception as exc:          # noqa: BLE001 - 单渠道失败不影响其它渠道
                    logger.error(f"推送到{channel}失败: {exc}")
                    result = PushResult(channel, False, str(exc))
            results.append(result)
            if self.on_result:
                self.on_result(result)
        return results

    def send_async(self, content: str, live_url: str = "",
                   channels: list[str] | None = None) -> threading.Thread:
        """后台发送，绝不阻塞录制。"""
        thread = threading.Thread(
            target=self.send,
            kwargs={"content": content, "live_url": live_url, "channels": channels},
            name="push",
            daemon=True,
        )
        thread.start()
        return thread

    # ------------------------------------------------------------ 语义封装
    def begin_text(self, record_name: str) -> str:
        template = self.cfg.text("begin_push_message_text").strip() or DEFAULT_BEGIN
        return render_template(template, record_name)

    def over_text(self, record_name: str) -> str:
        template = self.cfg.text("over_push_message_text").strip() or DEFAULT_OVER
        return render_template(template, record_name)

    def notify_begin(self, record_name: str, live_url: str) -> None:
        if self.cfg.flag("begin_show_push") and self.enabled:
            self.send_async(self.begin_text(record_name), live_url)

    def notify_over(self, record_name: str, live_url: str) -> None:
        if self.cfg.flag("over_show_push") and self.enabled:
            self.send_async(self.over_text(record_name), live_url)

    def test(self, channel: str, content: str = "这是一条来自 DouyinLiveRecorder 桌面端的测试消息") -> PushResult:
        """设置界面里的「发送测试」按钮。"""
        payload = f"{content}\n\n推送渠道：{channel}\n时间：{datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
        results = self.send(payload, channels=[channel])
        return results[0] if results else PushResult(channel, False, "未知渠道")
