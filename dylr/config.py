# -*- coding: utf-8 -*-
"""配置元数据 + 类型化读写。

上游 ``main.py`` 在模块顶层用 ~130 行 ``read_config_value(...)`` 把 ``config.ini``
摊平成一堆全局变量，任何一处拼错 key 都只在运行时静默退回默认值。这里改成
**一份声明式字段表**：

* 每个字段声明小节、ini 键名、默认值、类型、界面标签、说明、分组；
* Python 侧只按 slug 取值，不再关心中文键名；
* ``/api/config`` 直接把同一张表下发给 Electron，设置界面由它渲染，
  前端不会再出现「后端改了 key、前端没跟上」的漂移；
* 写回采用**逐行替换**，保留注释、顺序与 BOM，不再让 ConfigParser 把
  ``# 可选微信|钉钉|...`` 这类说明注释整段吃掉（上游每次缺键保存都会丢注释）。

取值语义（与上游对齐，唯一的例外已在下方标注）：

===========  ==========================================
文件中的值   Python 结果
===========  ==========================================
缺失         取该字段的声明默认值
空字符串     bool/int/float/choice → 取默认值；其余 → 空
有值         按类型解析
===========  ==========================================

> 例外：上游对「空字符串」的布尔项会退化成 ``否``（``options.get("", False)``），
> 例如 ``是否使用SMTP服务SSL加密`` 为空时上游实际不启用 SSL，与其注释声明的默认
> ``是`` 相矛盾。这里统一按**声明默认值**处理，属于有意修正。
"""
from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from src import paths

TEXT_ENCODING = "utf-8-sig"

TRUTHY = {"是", "true", "1", "yes", "on", "y"}
FALSY = {"否", "false", "0", "no", "off", "n", ""}


class ConfigError(ValueError):
    """配置值非法。"""


@dataclass(frozen=True)
class FieldSpec:
    """一个配置项的全部元信息。"""

    slug: str                     # 程序内标识（稳定，不随中文键名变）
    section: str                  # ini 小节
    key: str                      # ini 键名（与上游保持一致）
    default: Any = ""
    kind: str = "str"             # str|int|float|bool|choice|multichoice|path|secret|text
    label: str = ""               # 界面显示名
    group: str = "基础"           # 界面分组
    help: str = ""                # 界面说明
    choices: Sequence[str] = ()
    placeholder: str = ""
    minimum: float | None = None
    maximum: float | None = None
    advanced: bool = False
    restart_required: bool = False

    # ------------------------------------------------------------ 值转换
    def to_raw(self, value: Any) -> str:
        """Python 值 → ini 字符串。"""
        if self.kind == "bool":
            return "是" if _as_bool(value) else "否"
        if self.kind in ("int", "float"):
            return str(self.default) if value is None or value == "" else str(value)
        if value is None:
            return ""
        if self.kind == "multichoice" and isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
        return str(value)

    def to_python(self, raw: Any) -> Any:
        """ini 字符串 → Python 值。"""
        text = "" if raw is None else str(raw).strip()
        if self.kind == "bool":
            if text == "":
                return _as_bool(self.default)
            return _as_bool(text)
        if self.kind == "int":
            if text == "":
                return int(self.default or 0)
            try:
                return int(float(text))
            except ValueError:
                return int(self.default or 0)
        if self.kind == "float":
            if text == "":
                return float(self.default or 0)
            try:
                return float(text)
            except ValueError:
                return float(self.default or 0)
        if self.kind in ("choice", "multichoice"):
            try:
                return self.validate(text)
            except ConfigError:
                return self.validate(self.default) if self.default not in ("", None) else (
                    [] if self.kind == "multichoice" else ""
                )
        return text

    def validate(self, value: Any) -> Any:
        """校验并规范化，失败抛 :class:`ConfigError`。"""
        name = self.label or self.key
        if self.kind == "bool":
            if isinstance(value, str) and value.strip().lower() not in TRUTHY | FALSY:
                raise ConfigError(f"{name} 需要填「是」或「否」")
            return _as_bool(value)
        if self.kind == "int":
            try:
                iv = int(str(value).strip())
            except (TypeError, ValueError):
                raise ConfigError(f"{name} 需要整数")
            if self.minimum is not None and iv < self.minimum:
                raise ConfigError(f"{name} 不能小于 {int(self.minimum)}")
            if self.maximum is not None and iv > self.maximum:
                raise ConfigError(f"{name} 不能大于 {int(self.maximum)}")
            return iv
        if self.kind == "float":
            try:
                fv = float(str(value).strip())
            except (TypeError, ValueError):
                raise ConfigError(f"{name} 需要数字")
            if self.minimum is not None and fv < self.minimum:
                raise ConfigError(f"{name} 不能小于 {self.minimum}")
            if self.maximum is not None and fv > self.maximum:
                raise ConfigError(f"{name} 不能大于 {self.maximum}")
            return fv
        if self.kind == "choice":
            text = "" if value is None else str(value).strip()
            if text in self.choices:
                return text
            lowered = {str(c).lower(): c for c in self.choices}
            hit = lowered.get(text.lower())
            if hit is None:
                raise ConfigError(f"{name} 只能取 {' / '.join(str(c) for c in self.choices)}")
            return hit
        if self.kind == "multichoice":
            parts = _split_multi(value)
            lookup = {str(c).lower(): c for c in self.choices}
            picked: list[str] = []
            for part in parts:
                hit = lookup.get(part.lower())
                if hit is None:
                    raise ConfigError(f"{name} 含未知取值「{part}」")
                if hit not in picked:
                    picked.append(hit)
            return picked
        return "" if value is None else str(value)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in TRUTHY:
        return True
    if text in FALSY:
        return False
    return default


def _split_multi(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in str(value).replace("，", ",").split(",") if p.strip()]


# ---------------------------------------------------------------------------
# 字段表工厂
# ---------------------------------------------------------------------------

def _rec(slug, key, default="", kind="str", group="录制", **kw) -> FieldSpec:
    return FieldSpec(slug, "录制设置", key, default, kind, group=group, **kw)


def _push(slug, key, default="", kind="str", group="推送", **kw) -> FieldSpec:
    return FieldSpec(slug, "推送配置", key, default, kind, group=group, **kw)


RECORD_SPECS: list[FieldSpec] = [
    _rec("language", "language(zh_cn/en)", "zh_cn", "choice", "基础",
         label="界面语言", choices=("zh_cn", "en"), restart_required=True),
    _rec("skip_proxy_check", "是否跳过代理检测(是/否)", "否", "bool", "基础",
         label="跳过代理检测", help="跳过启动时的全局代理探测以加快启动；录制海外平台时仍按平台单独走代理"),
    _rec("video_save_path", "直播保存路径(不填则默认)", "", "path", "基础",
         label="录制保存路径", placeholder="留空则保存到项目的 downloads/ 目录"),
    _rec("folder_by_author", "保存文件夹是否以作者区分", "是", "bool", "基础", label="按作者分文件夹"),
    _rec("folder_by_time", "保存文件夹是否以时间区分", "否", "bool", "基础", label="按日期分文件夹"),
    _rec("folder_by_title", "保存文件夹是否以标题区分", "否", "bool", "基础", label="按标题分文件夹"),
    _rec("filename_by_title", "保存文件名是否包含标题", "否", "bool", "基础", label="文件名包含标题"),
    _rec("clean_emoji", "是否去除名称中的表情符号", "是", "bool", "基础", label="过滤昵称中的表情"),
    _rec("video_save_type", "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频", "TS", "choice", "录制",
         label="保存格式", choices=("TS", "MKV", "FLV", "MP4", "MP3音频", "M4A音频"),
         help="TS 断流容错最好，推荐长挂；MP3/M4A 只保留音频"),
    _rec("video_record_quality", "原画|超清|高清|标清|流畅", "原画", "choice", "录制",
         label="默认画质", choices=("原画", "蓝光", "超清", "高清", "标清", "流畅"),
         help="列表里单独写了画质的房间优先用房间自己的设置"),
    _rec("use_proxy", "是否使用代理ip(是/否)", "是", "bool", "录制", label="启用代理录制"),
    _rec("proxy_addr", "代理地址", "", "str", "录制", label="代理地址", placeholder="例如 127.0.0.1:10808"),
    _rec("max_request", "同一时间访问网络的线程数", 3, "int", "录制", label="并发请求数",
         minimum=1, maximum=64, help="同时访问各平台接口的线程数，过高容易被风控"),
    _rec("delay_default", "循环时间(秒)", 300, "int", "录制", label="循环检测间隔",
         minimum=5, maximum=86400, help="每个房间两次状态检测之间等待的秒数"),
    _rec("local_delay_default", "排队读取网址时间(秒)", 0, "int", "录制", label="启动排队间隔",
         minimum=0, maximum=600, advanced=True, help="批量启动任务时的错峰间隔，避免同时打接口"),
    _rec("loop_time", "是否显示循环秒数", "否", "bool", "录制", label="显示循环倒计时", advanced=True),
    _rec("show_url", "是否显示直播源地址", "否", "bool", "录制", label="日志显示拉流地址", advanced=True),
    _rec("split_video_by_time", "分段录制是否开启", "是", "bool", "录制", label="分段录制"),
    _rec("split_time", "视频分段时间(秒)", 1800, "int", "录制", label="分段时间(秒)",
         minimum=10, maximum=86400),
    _rec("enable_https_recording", "是否强制启用https录制", "否", "bool", "录制",
         label="强制 https 拉流", advanced=True),
    _rec("disk_space_limit", "录制空间剩余阈值(gb)", 1.0, "float", "录制", label="磁盘剩余阈值(GB)",
         minimum=0.0, maximum=10240.0, help="剩余空间低于该值时停止录制，保护磁盘不被写满"),
    _rec("converts_to_mp4", "录制完成后自动转为mp4格式", "是", "bool", "转码", label="录完自动转 MP4"),
    _rec("converts_to_h264", "mp4格式重新编码为h264", "否", "bool", "转码", label="转 MP4 时重编码 h264",
         help="重编码很吃 CPU，仅在播放器不兼容时开启"),
    _rec("delete_origin_file", "追加格式后删除原文件", "是", "bool", "转码", label="转码后删除原文件"),
    _rec("create_time_file", "生成时间字幕文件", "否", "bool", "转码", label="生成时间字幕"),
    _rec("is_run_script", "是否录制完成后执行自定义脚本", "否", "bool", "转码", label="录完执行脚本", advanced=True),
    _rec("custom_script", "自定义脚本执行命令", "", "str", "转码", label="脚本命令", advanced=True,
         placeholder="python myscript.py 或 ./hook.sh"),
    _rec("enable_proxy_platform", "使用代理录制的平台(逗号分隔)",
         "tiktok, sooplive, pandalive, winktv, flextv, popkontv, twitch, liveme, showroom, chzzk, "
         "shopee, shp, youtu, faceit", "str", "网络", label="走代理的平台", advanced=True,
         help="这些平台的取流请求会带上代理地址"),
    _rec("extra_enable_proxy_platform", "额外使用代理录制的平台(逗号分隔)", "", "str", "网络",
         label="额外走代理的平台", advanced=True),
]

PUSH_CHANNELS = ("微信", "钉钉", "TG", "邮箱", "BARK", "NTFY", "PUSHPLUS")

PUSH_SPECS: list[FieldSpec] = [
    _push("live_status_push", "直播状态推送渠道", "", "multichoice",
          label="推送渠道", choices=PUSH_CHANNELS,
          help="可多选；留空表示不发任何通知"),
    _push("push_message_title", "自定义推送标题", "直播间状态更新通知", label="推送标题"),
    _push("begin_push_message_text", "自定义开播推送内容", "", "text", label="开播文案",
          placeholder="留空使用默认：直播间状态更新：[直播间名称] 正在直播中，时间：[时间]"),
    _push("over_push_message_text", "自定义关播推送内容", "", "text", label="关播文案",
          placeholder="留空使用默认：直播间状态更新：[直播间名称] 直播已结束！时间：[时间]"),
    _push("begin_show_push", "开播推送开启(是/否)", "是", "bool", label="开播推送"),
    _push("over_show_push", "关播推送开启(是/否)", "否", "bool", label="关播推送"),
    _push("disable_record", "只推送通知不录制(是/否)", "否", "bool", label="只通知不录制",
          help="开启后仅监控开播状态并推送，不落盘"),
    _push("push_check_seconds", "直播推送检测频率(秒)", 1800, "int", label="仅推送模式检测间隔(秒)",
          minimum=10, maximum=86400),

    _push("dingtalk_api_url", "钉钉推送接口链接", label="钉钉 Webhook", group="推送·钉钉"),
    _push("dingtalk_phone_num", "钉钉通知@对象(填手机号)", label="@手机号", group="推送·钉钉"),
    _push("dingtalk_is_atall", "钉钉通知@全体(是/否)", "否", "bool", label="@全体成员", group="推送·钉钉"),

    _push("xizhi_api_url", "微信推送接口链接", label="微信推送接口", group="推送·微信"),

    _push("tg_token", "tgapi令牌", kind="secret", label="Bot Token", group="推送·TG"),
    _push("tg_chat_id", "tg聊天id(个人或者群组id)", label="Chat ID", group="推送·TG"),

    _push("email_host", "smtp邮件服务器", label="SMTP 服务器", group="推送·邮箱"),
    _push("smtp_port", "SMTP邮件服务器端口", label="端口", group="推送·邮箱"),
    _push("open_smtp_ssl", "是否使用SMTP服务SSL加密(是/否)", "是", "bool", label="SSL 加密", group="推送·邮箱"),
    _push("login_email", "邮箱登录账号", label="登录账号", group="推送·邮箱"),
    _push("email_password", "发件人密码(授权码)", kind="secret", label="授权码", group="推送·邮箱"),
    _push("sender_email", "发件人邮箱", label="发件人", group="推送·邮箱"),
    _push("sender_name", "发件人显示昵称", label="发件人昵称", group="推送·邮箱"),
    _push("to_email", "收件人邮箱", label="收件人", group="推送·邮箱"),

    _push("bark_msg_api", "bark推送接口链接", label="Bark 接口", group="推送·BARK"),
    _push("bark_msg_level", "bark推送中断级别", "active", "choice", label="中断级别", group="推送·BARK",
          choices=("active", "timeSensitive", "passive", "critical")),
    _push("bark_msg_ring", "bark推送铃声", "bell", label="铃声", group="推送·BARK"),

    _push("ntfy_api", "ntfy推送地址", label="ntfy 地址", group="推送·NTFY"),
    _push("ntfy_tags", "ntfy推送标签", "tada", label="标签", group="推送·NTFY"),
    _push("ntfy_email", "ntfy推送邮箱", label="邮箱", group="推送·NTFY"),

    _push("pushplus_token", "pushplus推送token", kind="secret", label="Token", group="推送·PUSHPLUS"),
]

#: 平台标识 → config.ini 里的 Cookie 键（与 :mod:`dylr.platforms` 的 cookie_key 对应）
COOKIE_KEYS: dict[str, str] = {
    "douyin": "抖音cookie",
    "kuaishou": "快手cookie",
    "tiktok": "tiktok_cookie",
    "huya": "虎牙cookie",
    "douyu": "斗鱼cookie",
    "yy": "yy_cookie",
    "bilibili": "b站cookie",
    "xiaohongshu": "小红书cookie",
    "bigo": "bigo_cookie",
    "blued": "blued_cookie",
    "sooplive": "sooplive_cookie",
    "netease": "netease_cookie",
    "qiandurebo": "千度热播_cookie",
    "pandatv": "pandatv_cookie",
    "maoerfm": "猫耳fm_cookie",
    "winktv": "winktv_cookie",
    "flextv": "flextv_cookie",
    "look": "look_cookie",
    "twitcasting": "twitcasting_cookie",
    "baidu": "baidu_cookie",
    "weibo": "weibo_cookie",
    "kugou": "kugou_cookie",
    "twitch": "twitch_cookie",
    "liveme": "liveme_cookie",
    "huajiao": "huajiao_cookie",
    "liuxing": "liuxing_cookie",
    "showroom": "showroom_cookie",
    "acfun": "acfun_cookie",
    "changliao": "changliao_cookie",
    "yinbo": "yinbo_cookie",
    "yingke": "yingke_cookie",
    "zhihu": "zhihu_cookie",
    "chzzk": "chzzk_cookie",
    "haixiu": "haixiu_cookie",
    "vvxqiu": "vvxqiu_cookie",
    "17live": "17live_cookie",
    "langlive": "langlive_cookie",
    "pplive": "pplive_cookie",
    "6room": "6room_cookie",
    "lehaitv": "lehaitv_cookie",
    "huamao": "huamao_cookie",
    "shopee": "shopee_cookie",
    "youtube": "youtube_cookie",
    "taobao": "taobao_cookie",
    "jd": "jd_cookie",
    "faceit": "faceit_cookie",
    "migu": "migu_cookie",
    "lianjie": "lianjie_cookie",
    "laixiu": "laixiu_cookie",
    "picarto": "picarto_cookie",
}

COOKIE_SPECS: list[FieldSpec] = [
    FieldSpec(f"cookie_{slug}", "Cookie", key, "", "secret", key, "登录凭据",
              placeholder="从浏览器开发者工具复制完整 Cookie", advanced=True)
    for slug, key in COOKIE_KEYS.items()
]

ACCOUNT_SPECS: list[FieldSpec] = [
    FieldSpec("sooplive_username", "账号密码", "sooplive账号", "", "str", "SOOP 账号", "平台账号", advanced=True),
    FieldSpec("sooplive_password", "账号密码", "sooplive密码", "", "secret", "SOOP 密码", "平台账号", advanced=True),
    FieldSpec("flextv_username", "账号密码", "flextv账号", "", "str", "FlexTV 账号", "平台账号", advanced=True),
    FieldSpec("flextv_password", "账号密码", "flextv密码", "", "secret", "FlexTV 密码", "平台账号", advanced=True),
    FieldSpec("popkontv_username", "账号密码", "popkontv账号", "", "str", "PopkonTV 账号", "平台账号", advanced=True),
    FieldSpec("popkontv_password", "账号密码", "popkontv密码", "", "secret", "PopkonTV 密码", "平台账号", advanced=True),
    FieldSpec("popkontv_partner_code", "账号密码", "partner_code", "P-00001", "str", "合作方代码",
              "平台账号", advanced=True),
    FieldSpec("twitcasting_account_type", "账号密码", "twitcasting账号类型", "normal", "choice",
              "TwitCasting 账号类型", "平台账号", choices=("normal", "streamer"), advanced=True),
    FieldSpec("twitcasting_username", "账号密码", "twitcasting账号", "", "str", "TwitCasting 账号",
              "平台账号", advanced=True),
    FieldSpec("twitcasting_password", "账号密码", "twitcasting密码", "", "secret", "TwitCasting 密码",
              "平台账号", advanced=True),
    FieldSpec("popkontv_token", "Authorization", "popkontv_token", "", "secret", "PopkonTV Token",
              "平台账号", advanced=True),
]

FIELD_SPECS: list[FieldSpec] = RECORD_SPECS + PUSH_SPECS + COOKIE_SPECS + ACCOUNT_SPECS
SPEC_BY_SLUG: dict[str, FieldSpec] = {s.slug: s for s in FIELD_SPECS}
SPEC_BY_INI: dict[tuple[str, str], FieldSpec] = {(s.section, s.key): s for s in FIELD_SPECS}

#: 改动后需要重建任务线程的字段
RESTART_FIELDS = {
    "video_save_type", "split_video_by_time", "split_time", "use_proxy", "proxy_addr",
    "max_request", "video_save_path", "folder_by_author", "folder_by_time", "folder_by_title",
    "filename_by_title", "enable_proxy_platform", "extra_enable_proxy_platform",
}


# ---------------------------------------------------------------------------
# 配置对象
# ---------------------------------------------------------------------------

class AppConfig:
    """类型化配置。线程安全；写回保留原文件注释、空行与行序。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else paths.config_file()
        self._lock = threading.RLock()
        self._raw: dict[tuple[str, str], str] = {}
        self.load()

    # ------------------------------------------------------------ 读取
    def load(self) -> None:
        with self._lock:
            self._raw = self._parse(self.path)

    @staticmethod
    def _parse(path: Path) -> dict[tuple[str, str], str]:
        data: dict[tuple[str, str], str] = {}
        if not path.exists():
            return data
        section = ""
        for line in _read_lines(path):
            stripped = line.strip()
            if not stripped or stripped[0] in "#;":
                continue
            match = _SECTION_RE.match(line)
            if match:
                section = match.group("name").strip()
                continue
            if "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            data[(section, key.strip())] = value.strip()
        return data

    def _stored(self, section: str, key: str) -> str | None:
        with self._lock:
            return self._raw.get((section, key))

    def raw(self, slug: str) -> str:
        """文件里的原始字符串；键缺失时返回声明默认值。"""
        spec = SPEC_BY_SLUG[slug]
        stored = self._stored(spec.section, spec.key)
        if stored is None:
            return spec.to_raw(spec.default)
        return stored

    def get(self, slug: str, default: Any = None) -> Any:
        """取类型化值。"""
        spec = SPEC_BY_SLUG.get(slug)
        if spec is None:
            if default is not None:
                return default
            raise KeyError(f"未知配置项: {slug}")
        return spec.to_python(self.raw(slug))

    def has_value(self, slug: str) -> bool:
        """用户是否在文件里真正填了值（用于「未配置」提示）。"""
        spec = SPEC_BY_SLUG[slug]
        return bool((self._stored(spec.section, spec.key) or "").strip())

    # 便捷访问器
    def text(self, slug: str, default: str = "") -> str:
        value = self.get(slug, default)
        return default if value is None else str(value)

    def flag(self, slug: str) -> bool:
        return bool(self.get(slug))

    def number(self, slug: str) -> int:
        return int(self.get(slug) or 0)

    def decimal(self, slug: str) -> float:
        return float(self.get(slug) or 0.0)

    def multi(self, slug: str) -> list[str]:
        try:
            return list(SPEC_BY_SLUG[slug].validate(self.raw(slug)))
        except ConfigError:
            return []

    def by_ini(self, section: str, key: str) -> str:
        """按 ini 小节 + 键名直接取值（给 Cookie / 账号密码这类批量字段用）。"""
        return (self._stored(section, key) or "").strip()

    def cookie(self, platform_key: str) -> str:
        """按平台标识取 Cookie，未映射或未配置返回空串。"""
        key = COOKIE_KEYS.get(platform_key)
        return self.by_ini("Cookie", key) if key else ""

    def platform_credentials(self, platform_key: str) -> dict[str, str]:
        """SOOP / FlexTV / PopkonTV / TwitCasting 的账号密码。"""
        mapping = {
            "sooplive": ("sooplive_username", "sooplive_password"),
            "flextv": ("flextv_username", "flextv_password"),
            "popkontv": ("popkontv_username", "popkontv_password"),
            "twitcasting": ("twitcasting_username", "twitcasting_password"),
        }
        pair = mapping.get(platform_key)
        if not pair:
            return {}
        return {"username": self.text(pair[0]), "password": self.text(pair[1])}

    # ------------------------------------------------------------ 写入
    def _validate(self, patch: dict[str, Any]) -> dict[str, Any]:
        """只校验不落盘；任何一项非法都不会产生副作用。"""
        staged: dict[str, Any] = {}
        for slug, value in patch.items():
            spec = SPEC_BY_SLUG.get(slug)
            if spec is None:
                raise ConfigError(f"未知配置项：{slug}")
            staged[slug] = spec.validate(value)
        return staged

    def set(self, slug: str, value: Any) -> Any:
        """校验并写入内存，返回规范化后的值。"""
        clean = self._validate({slug: value})[slug]
        spec = SPEC_BY_SLUG[slug]
        with self._lock:
            self._raw[(spec.section, spec.key)] = spec.to_raw(clean)
        return clean

    def update(self, patch: dict[str, Any]) -> dict[str, Any]:
        """批量校验并写内存（不落盘）；任一字段非法则整体不生效。"""
        staged = self._validate(patch)
        for slug, value in staged.items():
            spec = SPEC_BY_SLUG[slug]
            with self._lock:
                self._raw[(spec.section, spec.key)] = spec.to_raw(value)
        return staged

    def save(self, patch: dict[str, Any] | None = None) -> list[str]:
        """校验 + 落盘，返回实际发生变化的 slug 列表。

        逐行替换，未涉及的键（含注释、空行、顺序）原样保留。只有真正变化的行
        会被写入，因此改一项配置只会产生一行 diff。
        """
        if not patch:
            return []
        staged = self._validate(patch)

        original: dict[tuple[str, str], str] = {}
        applied: dict[tuple[str, str], str] = {}
        for slug, value in staged.items():
            spec = SPEC_BY_SLUG[slug]
            target = (spec.section, spec.key)
            original[target] = self._stored(*target)
            applied[target] = spec.to_raw(value)

        # 先落内存，再算差异（差异必须和写盘前的文件状态比）
        with self._lock:
            self._raw.update(applied)

        delta = {target: value for target, value in applied.items()
                 if (original[target] or "") != value}
        if not delta:
            return []

        with self._lock:
            lines = _apply_to_lines(_read_lines(self.path), delta)
            self._write_lines(lines, trailing_newline=_ends_with_newline(self.path))
        return [SPEC_BY_INI[target].slug for target in delta]

    def _write_lines(self, lines: list[str], trailing_newline: bool = True) -> None:
        paths.ensure_dir(self.path.parent)
        text = "\n".join(line.rstrip("\r\n") for line in lines)
        # 保留文件原本「末尾是否有换行」，避免仅仅因为改一项配置就多出一行 diff
        if trailing_newline and not text.endswith("\n"):
            text += "\n"
        # utf-8-sig 保留 BOM，与上游一致（Windows 记事本 / 旧工具友好）
        with open(self.path, "w", encoding=TEXT_ENCODING, newline="\n") as fh:
            fh.write(text)

    # ------------------------------------------------------------ 导出
    def values(self, mask_secrets: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for spec in FIELD_SPECS:
            if spec.kind == "secret" and mask_secrets:
                out[spec.slug] = mask(self.raw(spec.slug))
            else:
                out[spec.slug] = self.get(spec.slug)
        return out

    def as_dict(self, mask_secrets: bool = True, only_set: bool = False) -> dict[str, Any]:
        out = self.values(mask_secrets=mask_secrets)
        if only_set:
            out = {k: v for k, v in out.items() if self.has_value(k)}
        return out

    def snapshot(self, mask_secrets: bool = True) -> dict[str, Any]:
        """给界面的完整视图：值 + 元数据 + 是否已填。"""
        items = []
        for spec in FIELD_SPECS:
            value: Any = self.get(spec.slug)
            shown = mask(self.raw(spec.slug)) if (spec.kind == "secret" and mask_secrets) else value
            items.append({
                "slug": spec.slug,
                "section": spec.section,
                "key": spec.key,
                "kind": spec.kind,
                "label": spec.label or spec.key,
                "group": spec.group,
                "help": spec.help,
                "choices": list(spec.choices),
                "placeholder": spec.placeholder,
                "minimum": spec.minimum,
                "maximum": spec.maximum,
                "default": spec.default,
                "advanced": spec.advanced,
                "restart_required": spec.restart_required,
                "value": shown,
                "is_set": self.has_value(spec.slug),
            })
        return {"items": items, "groups": _groups(), "path": str(self.path)}


def _groups() -> list[str]:
    seen: list[str] = []
    for spec in FIELD_SPECS:
        if spec.group not in seen:
            seen.append(spec.group)
    return seen


_SECRET_HEAD = 4
_SECRET_TAIL = 4


def mask(value: str) -> str:
    """secret 字段脱敏：保留首尾各 4 位，中间用 * 代替。"""
    if not value:
        return ""
    if len(value) <= _SECRET_HEAD + _SECRET_TAIL:
        return "*" * len(value)
    return f"{value[:_SECRET_HEAD]}{'*' * 8}{value[-_SECRET_TAIL:]}"


def is_masked(value: Any) -> bool:
    return isinstance(value, str) and "********" in value


_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    with open(path, "r", encoding=TEXT_ENCODING, errors="ignore") as fh:
        return fh.read().splitlines()


def _ends_with_newline(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.seek(-1, os.SEEK_END)
            return fh.read(1) in (b"\n", b"\r")
    except (OSError, ValueError):
        return True


def _apply_to_lines(lines: list[str], applied: dict[tuple[str, str], str]) -> list[str]:
    """保持行序与注释，就地替换键值；缺键则补到所属小节末尾。

    上游的 ``ConfigParser.write()`` 会把注释和顺序全部洗掉，导致每次改配置都产生
    巨大的无意义 diff。这里只动真正变化的那一行。
    """
    out = list(lines)
    pending = dict(applied)

    # 第一遍：找出已存在的键并在原位替换
    section = ""
    for idx, line in enumerate(out):
        match = _SECTION_RE.match(line)
        if match:
            section = match.group("name").strip()
            continue
        stripped = line.strip()
        if not stripped or stripped[0] in "#;" or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        target = (section, key)
        if target in pending:
            out[idx] = f"{key} = {pending.pop(target)}"

    if not pending:
        return out

    # 第二遍：按小节定位插入点（该小节最后一条有效行的下一行）
    bounds: dict[str, tuple[int, int]] = {}
    section, start = "", 0
    for idx, line in enumerate(out):
        match = _SECTION_RE.match(line)
        if match:
            if section:
                bounds[section] = (start, idx)
            section = match.group("name").strip()
            start = idx + 1
    if section:
        bounds[section] = (start, len(out))

    # 从后往前插入，避免下标漂移
    grouped: dict[str, list[str]] = {}
    for (sec, key), value in pending.items():
        grouped.setdefault(sec, []).append(f"{key} = {value}")

    for sec in sorted(grouped, key=lambda s: bounds.get(s, (0, 0))[0], reverse=True):
        entries = grouped[sec]
        if sec in bounds:
            lo, hi = bounds[sec]
            anchor = hi
            while anchor > lo and not out[anchor - 1].strip():
                anchor -= 1
            out[anchor:anchor] = entries
        else:
            if out and out[-1].strip():
                out.append("")
            out.append(f"[{sec}]")
            out.extend(entries)
    return out


def iter_specs(slugs: Iterable[str] | None = None) -> list[FieldSpec]:
    if not slugs:
        return list(FIELD_SPECS)
    wanted = set(slugs)
    return [s for s in FIELD_SPECS if s.slug in wanted]
