# -*- coding: utf-8 -*-
"""平台注册表 —— 把「40 个分支的 if/elif 链」变成一张可读的表。

上游 ``main.py`` 的 ``start_record`` 里有约 460 行形如::

    elif record_url.find("https://www.douyu.com/") > -1:
        platform = '斗鱼直播'
        with semaphore:
            json_data = asyncio.run(spider.get_douyu_info_data(url=..., proxy_addr=..., cookies=...))
            port_info = asyncio.run(stream.get_douyu_stream_url(json_data, video_quality=..., ...))

的分发代码，并且同一个平台的属性被拆散在 7 张互不相干的列表里：
``get_record_headers`` 的字典、``is_flv_preferred_platform``、
``only_flv_platform_list``、``only_audio_platform_list``、``re_plat``、
``http_record_list``、``overseas_platform_host``，还有 ``main.py`` 末尾那段
用于校验 URL 的 ``platform_host`` / ``clean_url_host_list``。

这里把「一个平台怎么抓、要不要代理、用什么格式、带什么请求头」收敛成
:class:`PlatformSpec` 的一条记录，新增平台只需在 ``PLATFORMS`` 里加一行。
"""
from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from src import spider, stream
from src.utils import logger
from src.utils import get_query_params

#: 画质中文 → 上游 stream.get_quality_index 使用的代码
_QUALITY_CODES = {
    "原画": "OD",
    "蓝光": "BD",
    "超清": "UHD",
    "高清": "HD",
    "标清": "SD",
    "流畅": "LD",
}


def quality_code(label: str) -> str:
    """中文画质 → 平台侧代码。"""
    return _QUALITY_CODES.get(label, "OD")


def quality_options() -> list[str]:
    """界面下拉框用的画质选项。"""
    return list(_QUALITY_CODES.keys())


@dataclass
class ProbeContext:
    """一次平台探测所需的全部上下文。

    凭据统一从 :class:`~dylr.config.AppConfig` 现取，而不是像上游那样在
    ``main.py`` 顶部把 46 个 Cookie 变量全部展开——探测函数只在真正用到时
    才读对应字段。
    """

    url: str
    quality_label: str
    quality: str
    proxy: str | None
    has_proxy: bool
    platform: "PlatformSpec | None" = None
    config: Any = None

    @property
    def _key(self) -> str:
        return self.platform.key if self.platform else ""

    def cookie(self, key: str | None = None) -> str:
        if self.config is None:
            return ""
        return self.config.cookie(key or self._key)

    def cred(self, name: str, default: str = "") -> str:
        """取平台账号密码 / Token。"""
        if self.config is None:
            return default
        key = self._key
        special = {
            "access_token": "popkontv_token",
            "partner_code": "popkontv_partner_code",
        }
        slug = special.get(name) or f"{key}_{name}"
        return self.config.text(slug) or default


#: 探测函数签名：``ctx -> port_info dict``（返回 None 表示本次没取到信息）
Probe = Callable[[ProbeContext], dict[str, Any] | None]


@dataclass(frozen=True)
class PlatformSpec:
    """一个直播平台的全部元信息。"""

    key: str                       # 程序内标识
    name: str                      # 展示名（也是保存目录名，与上游一致）
    matchers: tuple[str, ...]      # URL 子串匹配
    probe: Probe                   # 取流实现

    group: str = "国内"
    #: 必须存在可用代理才尝试（海外平台）
    needs_proxy: bool = False
    #: 优先使用 FLV 源（抖音/TikTok）
    flv_preferred: bool = False
    #: 平台只提供 FLV，跳过 ffmpeg 直接用流式下载
    force_flv: bool = False
    #: 仅音频
    audio_only: bool = False
    #: ffmpeg ``-headers`` 值，``{domain}`` 会被替换成直播间域名
    header: str = ""
    #: 保存目录用这个平台名（默认等于 name）
    dir_name: str = ""
    #: 日志展示拉流地址时优先用 m3u8
    log_m3u8: bool = False
    #: 海外平台，ffmpeg 需要放宽超时/缓冲
    overseas: bool = False
    #: 自定义直链（.m3u8/.flv）
    custom: bool = False
    #: 需要长期监听更新的凭证字段（新 Cookie / Token 回写）
    credential_updates: tuple[tuple[str, str], ...] = ()
    #: 该平台是否需要 Cookie 才能取流（界面据此提示）
    cookie_key: str = ""

    @property
    def folder(self) -> str:
        return self.dir_name or self.name

    def matches(self, url: str) -> bool:
        return any(m and m in url for m in self.matchers)

    def resolve_header(self, url: str) -> str:
        if not self.header:
            return ""
        domain = "/".join(url.split("/")[0:3])
        return self.header.replace("{domain}", domain)


# ---------------------------------------------------------------------------
# 探测实现
#
# 每个函数都保持与上游分支完全相同的调用顺序与参数，只是把「全局变量」换成
# ``ProbeContext``，把 ``asyncio.run`` 留在原处（上游就是同步线程里跑
# asyncio.run，没有事件循环复用问题）。
# ---------------------------------------------------------------------------

def _p_douyin(ctx: ProbeContext) -> dict | None:
    if "v.douyin.com" not in ctx.url and "/user/" not in ctx.url:
        data = asyncio.run(spider.get_douyin_web_stream_data(
            url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    else:
        data = asyncio.run(spider.get_douyin_app_stream_data(
            url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_douyin_stream_url(data, ctx.quality, ctx.proxy))


def _p_tiktok(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_tiktok_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_tiktok_stream_url(data, ctx.quality, ctx.proxy))


def _p_kuaishou(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_kuaishou_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_kuaishou_stream_url(data, ctx.quality))


def _p_huya(ctx: ProbeContext) -> dict | None:
    if ctx.quality in ("OD", "BD", "UHD"):
        # 高清档位走 App 接口，直接返回可用的 port_info
        return asyncio.run(spider.get_huya_app_stream_url(
            url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    data = asyncio.run(spider.get_huya_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_huya_stream_url(data, ctx.quality))


def _p_douyu(ctx: ProbeContext) -> dict | None:
    cookie = ctx.cookie()
    data = asyncio.run(spider.get_douyu_info_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=cookie))
    return asyncio.run(stream.get_douyu_stream_url(
        data, video_quality=ctx.quality, cookies=cookie, proxy_addr=ctx.proxy))


def _p_yy(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_yy_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_yy_stream_url(data))


def _p_bilibili(ctx: ProbeContext) -> dict | None:
    cookie = ctx.cookie()
    data = asyncio.run(spider.get_bilibili_room_info(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=cookie))
    return asyncio.run(stream.get_bilibili_stream_url(
        data, video_quality=ctx.quality, cookies=cookie, proxy_addr=ctx.proxy))


def _p_xiaohongshu(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_xhs_stream_url(
        ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_bigo(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_bigo_stream_url(
        ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_blued(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_blued_stream_url(
        ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_sooplive(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_sooplive_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie(),
        username=ctx.cred("username"), password=ctx.cred("password")))
    if data and data.get("new_cookies") and ctx.config is not None:
        ctx.config.save({"cookie_sooplive": data["new_cookies"]})
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_netease(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_netease_stream_data(url=ctx.url, cookies=ctx.cookie()))
    return asyncio.run(stream.get_netease_stream_url(data, ctx.quality))


def _p_qiandurebo(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_qiandurebo_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_pandatv(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_pandatv_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_maoerfm(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_maoerfm_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_winktv(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_winktv_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_flextv(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_flextv_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie(),
        username=ctx.cred("username"), password=ctx.cred("password")))
    if data and data.get("new_cookies") and ctx.config is not None:
        ctx.config.save({"cookie_flextv": data["new_cookies"]})
    if data and "play_url_list" in data:
        return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))
    return data


def _p_look(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_looklive_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_popkontv(ctx: ProbeContext) -> dict | None:
    info = asyncio.run(spider.get_popkontv_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy,
        access_token=ctx.cred("access_token"),
        username=ctx.cred("username"), password=ctx.cred("password"),
        partner_code=ctx.cred("partner_code", "P-00001")))
    if info and info.get("new_token") and ctx.config is not None:
        ctx.config.save({"popkontv_token": info["new_token"]})
    return info


def _p_twitcasting(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_twitcasting_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie(),
        account_type=ctx.cred("account_type", "normal"),
        username=ctx.cred("username"), password=ctx.cred("password")))
    info = asyncio.run(stream.get_stream_url(data, ctx.quality, spec=False))
    if info and info.get("new_cookies") and ctx.config is not None:
        ctx.config.save({"cookie_twitcasting": info["new_cookies"]})
    return info


def _p_baidu(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_baidu_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality))


def _p_weibo(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_weibo_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    # 微博的 m3u8 藏在 m3u8_url 字段里
    return asyncio.run(stream.get_stream_url(data, ctx.quality, hls_extra_key="m3u8_url"))


def _p_kugou(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_kugou_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_twitch(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_twitchtv_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_liveme(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_liveme_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_huajiao(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_huajiao_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_liuxing(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_liuxing_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_showroom(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_showroom_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_acfun(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_acfun_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(
        data, ctx.quality, url_type="flv", flv_extra_key="url"))


def _p_changliao(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_changliao_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_yinbo(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_yinbo_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_yingke(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_yingke_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_zhihu(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_zhihu_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_chzzk(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_chzzk_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_haixiu(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_haixiu_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_vvxqiu(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_vvxqiu_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_17live(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_17live_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_langlive(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_langlive_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_pplive(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_pplive_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_6room(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_6room_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_lehaitv(ctx: ProbeContext) -> dict | None:
    # 乐嗨直播与嗨秀共用同一个解析实现
    return asyncio.run(spider.get_haixiu_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_huamao(ctx: ProbeContext) -> dict | None:
    # 花猫直播与漂漂共用同一个解析实现
    return asyncio.run(spider.get_pplive_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_shopee(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_shopee_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_youtube(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_youtube_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_taobao(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_taobao_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(
        data, ctx.quality, url_type="all", hls_extra_key="hlsUrl", flv_extra_key="flvUrl"))


def _p_jd(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_jd_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_faceit(ctx: ProbeContext) -> dict | None:
    data = asyncio.run(spider.get_faceit_stream_data(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))
    return asyncio.run(stream.get_stream_url(data, ctx.quality, spec=True))


def _p_migu(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_migu_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_lianjie(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_lianjie_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_laixiu(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_laixiu_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_picarto(ctx: ProbeContext) -> dict | None:
    return asyncio.run(spider.get_picarto_stream_url(
        url=ctx.url, proxy_addr=ctx.proxy, cookies=ctx.cookie()))


def _p_custom(ctx: ProbeContext) -> dict:
    """自定义直链（.m3u8 / .flv）直接交给 ffmpeg。"""
    info: dict[str, Any] = {
        "anchor_name": f"自定义录制直播_{str(uuid.uuid4())[:8]}",
        "is_live": True,
        "record_url": ctx.url,
    }
    if ".flv" in ctx.url:
        info["flv_url"] = ctx.url
    else:
        info["m3u8_url"] = ctx.url
    return info


# ---------------------------------------------------------------------------
# 平台表
#
# 顺序 = 匹配优先级，与上游 if/elif 的顺序严格一致（第一个命中的分支胜出）。
# ---------------------------------------------------------------------------

PLATFORMS: list[PlatformSpec] = [
    PlatformSpec("douyin", "抖音直播", ("douyin.com/",), _p_douyin,
                 flv_preferred=True, cookie_key="douyin"),
    PlatformSpec("tiktok", "TikTok直播", ("https://www.tiktok.com/",), _p_tiktok,
                 group="海外", needs_proxy=True, overseas=True, flv_preferred=True, cookie_key="tiktok"),
    PlatformSpec("kuaishou", "快手直播", ("https://live.kuaishou.com/",), _p_kuaishou,
                 cookie_key="kuaishou"),
    PlatformSpec("huya", "虎牙直播", ("https://www.huya.com/",), _p_huya, cookie_key="huya"),
    PlatformSpec("douyu", "斗鱼直播", ("https://www.douyu.com/",), _p_douyu, cookie_key="douyu"),
    PlatformSpec("yy", "YY直播", ("https://www.yy.com/",), _p_yy, cookie_key="yy"),
    PlatformSpec("bilibili", "B站直播", ("https://live.bilibili.com/",), _p_bilibili,
                 cookie_key="bilibili"),

    PlatformSpec("xiaohongshu", "小红书直播",
                 ("http://xhslink.com/", "https://www.xiaohongshu.com/"), _p_xiaohongshu,
                 cookie_key="xiaohongshu"),
    PlatformSpec("bigo", "Bigo直播", ("www.bigo.tv/", "slink.bigovideo.tv/"), _p_bigo,
                 group="海外", cookie_key="bigo"),
    PlatformSpec("blued", "Blued直播", ("https://app.blued.cn/",), _p_blued,
                 header="referer:https://app.blued.cn", cookie_key="blued"),

    PlatformSpec("sooplive", "SOOP", ("sooplive.co.kr/", "sooplive.com/"), _p_sooplive,
                 group="海外", needs_proxy=True, overseas=True, cookie_key="sooplive"),
    PlatformSpec("netease", "网易CC直播", ("cc.163.com/",), _p_netease, cookie_key="netease"),
    PlatformSpec("qiandurebo", "千度热播", ("qiandurebo.com/",), _p_qiandurebo,
                 header="referer:https://qiandurebo.com", cookie_key="qiandurebo"),
    PlatformSpec("pandatv", "PandaTV", ("www.pandalive.co.kr/",), _p_pandatv,
                 group="海外", needs_proxy=True, overseas=True, log_m3u8=True,
                 header="origin:https://www.pandalive.co.kr", cookie_key="pandatv"),
    PlatformSpec("maoerfm", "猫耳FM直播", ("fm.missevan.com/",), _p_maoerfm,
                 audio_only=True, cookie_key="maoerfm"),
    PlatformSpec("winktv", "WinkTV", ("www.winktv.co.kr/",), _p_winktv,
                 group="海外", needs_proxy=True, overseas=True, log_m3u8=True,
                 header="origin:https://www.winktv.co.kr", cookie_key="winktv"),
    PlatformSpec("flextv", "FlexTV", ("www.flextv.co.kr/", "www.ttinglive.com/"), _p_flextv,
                 group="海外", needs_proxy=True, overseas=True,
                 header="origin:https://www.flextv.co.kr", cookie_key="flextv"),
    PlatformSpec("look", "Look直播", ("look.163.com/",), _p_look, audio_only=True, cookie_key="look"),
    PlatformSpec("popkontv", "PopkonTV", ("www.popkontv.com/",), _p_popkontv,
                 group="海外", needs_proxy=True, overseas=True,
                 header="origin:https://www.popkontv.com", cookie_key="popkontv"),
    PlatformSpec("twitcasting", "TwitCasting", ("twitcasting.tv/",), _p_twitcasting,
                 group="海外", cookie_key="twitcasting"),
    PlatformSpec("baidu", "百度直播", ("live.baidu.com/",), _p_baidu, cookie_key="baidu"),
    PlatformSpec("weibo", "微博直播", ("weibo.com/",), _p_weibo, cookie_key="weibo"),
    PlatformSpec("kugou", "酷狗直播", ("kugou.com/",), _p_kugou, cookie_key="kugou"),
    PlatformSpec("twitch", "TwitchTV", ("www.twitch.tv/",), _p_twitch,
                 group="海外", needs_proxy=True, overseas=True, cookie_key="twitch"),
    PlatformSpec("liveme", "LiveMe", ("www.liveme.com/",), _p_liveme,
                 group="海外", needs_proxy=True, overseas=True, cookie_key="liveme"),
    PlatformSpec("huajiao", "花椒直播", ("www.huajiao.com/",), _p_huajiao,
                 force_flv=True, cookie_key="huajiao"),
    PlatformSpec("liuxing", "流星直播", ("7u66.com/",), _p_liuxing, cookie_key="liuxing"),
    PlatformSpec("showroom", "ShowRoom", ("showroom-live.com/",), _p_showroom,
                 group="海外", needs_proxy=True, overseas=True, log_m3u8=True, cookie_key="showroom"),
    PlatformSpec("acfun", "Acfun", ("live.acfun.cn/", "m.acfun.cn/"), _p_acfun, cookie_key="acfun"),
    PlatformSpec("changliao", "畅聊直播", ("live.tlclw.com/",), _p_changliao, cookie_key="changliao"),
    PlatformSpec("yinbo", "音播直播", ("ybw1666.com/",), _p_yinbo, cookie_key="yinbo"),
    PlatformSpec("yingke", "映客直播", ("www.inke.cn/",), _p_yingke, cookie_key="yingke"),
    PlatformSpec("zhihu", "知乎直播", ("www.zhihu.com/",), _p_zhihu, cookie_key="zhihu"),
    PlatformSpec("chzzk", "CHZZK", ("chzzk.naver.com/",), _p_chzzk,
                 group="海外", needs_proxy=True, overseas=True, log_m3u8=True, cookie_key="chzzk"),
    PlatformSpec("haixiu", "嗨秀直播", ("www.haixiutv.com/",), _p_haixiu, cookie_key="haixiu"),
    PlatformSpec("vvxqiu", "VV星球", ("vvxqiu.com/",), _p_vvxqiu, cookie_key="vvxqiu"),
    PlatformSpec("17live", "17Live", ("17.live/",), _p_17live,
                 header="referer:https://17.live/en/live/6302408", cookie_key="17live"),
    PlatformSpec("langlive", "浪Live", ("www.lang.live/",), _p_langlive,
                 header="referer:https://www.lang.live", cookie_key="langlive"),
    PlatformSpec("pplive", "漂漂直播", ("m.pp.weimipopo.com/",), _p_pplive, cookie_key="pplive"),
    PlatformSpec("6room", "六间房直播", (".6.cn/",), _p_6room, cookie_key="6room"),
    PlatformSpec("lehaitv", "乐嗨直播", ("lehaitv.com/",), _p_lehaitv, cookie_key="lehaitv"),
    PlatformSpec("huamao", "花猫直播", ("h.catshow168.com/",), _p_huamao, cookie_key="huamao"),
    PlatformSpec("shopee", "shopee", ("live.shopee", "shp.ee/"), _p_shopee,
                 group="海外", needs_proxy=True, overseas=True, force_flv=True,
                 header="origin:{domain}", cookie_key="shopee"),
    PlatformSpec("youtube", "Youtube", ("www.youtube.com/", "youtu.be/"), _p_youtube,
                 group="海外", needs_proxy=True, overseas=True, log_m3u8=True, cookie_key="youtube"),
    PlatformSpec("taobao", "淘宝直播", ("tb.cn",), _p_taobao, cookie_key="taobao"),
    PlatformSpec("jd", "京东直播", ("3.cn", "m.jd.com"), _p_jd, cookie_key="jd"),
    PlatformSpec("faceit", "faceit", ("faceit.com/",), _p_faceit,
                 group="海外", needs_proxy=True, overseas=True, cookie_key="faceit"),
    PlatformSpec("migu", "咪咕直播", ("www.miguvideo.com", "m.miguvideo.com"), _p_migu, cookie_key="migu"),
    PlatformSpec("lianjie", "连接直播", ("show.lailianjie.com",), _p_lianjie, cookie_key="lianjie"),
    PlatformSpec("laixiu", "来秀直播", ("www.imkktv.com",), _p_laixiu, cookie_key="laixiu"),
    PlatformSpec("picarto", "Picarto", ("www.picarto.tv",), _p_picarto, group="海外", cookie_key="picarto"),
    # 直链必须放在最后，否则会抢掉带 .m3u8 查询参数的普通平台地址
    PlatformSpec("custom", "自定义录制直播", (".m3u8", ".flv"), _p_custom,
                 group="自定义", custom=True),
]

PLATFORM_BY_KEY: dict[str, PlatformSpec] = {p.key: p for p in PLATFORMS}


def resolve(url: str) -> PlatformSpec | None:
    """按 URL 定位平台；未识别返回 None。"""
    for spec in PLATFORMS:
        if spec.matches(url):
            return spec
    return None


def is_custom_direct_link(url: str) -> bool:
    return bool(re.search(r"\.(m3u8|flv)", url, re.IGNORECASE))


# ---------------------------------------------------------------------------
# URL 支持面（供 urlstore 校验用，等价上游的 platform_host 列表）
# ---------------------------------------------------------------------------

SUPPORTED_HOSTS: tuple[str, ...] = (
    "live.douyin.com", "v.douyin.com", "www.douyin.com",
    "live.kuaishou.com", "www.huya.com", "www.douyu.com", "www.yy.com",
    "live.bilibili.com", "www.redelight.cn", "www.xiaohongshu.com", "xhslink.com",
    "www.bigo.tv", "slink.bigovideo.tv", "app.blued.cn", "cc.163.com",
    "qiandurebo.com", "fm.missevan.com", "look.163.com", "twitcasting.tv",
    "live.baidu.com", "weibo.com", "fanxing.kugou.com", "fanxing2.kugou.com",
    "mfanxing.kugou.com", "www.huajiao.com", "www.7u66.com", "wap.7u66.com",
    "live.acfun.cn", "m.acfun.cn", "live.tlclw.com", "wap.tlclw.com",
    "live.ybw1666.com", "wap.ybw1666.com", "www.inke.cn", "www.zhihu.com",
    "www.haixiutv.com", "h5webcdnp.vvxqiu.com", "17.live", "www.lang.live",
    "m.pp.weimipopo.com", "v.6.cn", "m.6.cn", "www.lehaitv.com",
    "h.catshow168.com", "e.tb.cn", "huodong.m.taobao.com", "3.cn",
    "eco.m.jd.com", "www.miguvideo.com", "m.miguvideo.com",
    "show.lailianjie.com", "www.imkktv.com", "www.picarto.tv",
    # 海外
    "www.tiktok.com", "play.sooplive.co.kr", "m.sooplive.co.kr",
    "www.sooplive.com", "m.sooplive.com", "www.pandalive.co.kr",
    "www.winktv.co.kr", "www.flextv.co.kr", "www.ttinglive.com",
    "www.popkontv.com", "www.twitch.tv", "www.liveme.com",
    "www.showroom-live.com", "chzzk.naver.com", "m.chzzk.naver.com",
    "live.shopee.", ".shp.ee", "www.youtube.com", "youtu.be", "www.faceit.com",
)

#: 这些域名存进去时要裁掉查询参数（等价上游 clean_url_host_list）
CLEAN_QUERY_HOSTS: tuple[str, ...] = (
    "live.douyin.com", "live.bilibili.com", "www.huajiao.com", "www.zhihu.com",
    "www.huya.com", "chzzk.naver.com", "www.liveme.com", "www.haixiutv.com",
    "v.6.cn", "m.6.cn", "www.lehaitv.com",
)

_URL_RE = re.compile(r"(https?://)?(www\.)?[a-zA-Z0-9-]+(\.[a-zA-Z0-9-]+)+(:\d+)?(/.*)?")


def extract_host(url: str) -> str:
    """取 URL 的主机名，容忍缺少协议头的输入。"""
    normalized = url if "://" in url else "https://" + url
    parts = normalized.split("/")
    return parts[2] if len(parts) > 2 else ""


def matches_supported_host(url: str) -> bool:
    """URL 是否落在已知平台范围内。"""
    host = extract_host(url)
    if "live.shopee." in host or ".shp.ee" in host:
        return True
    if host in SUPPORTED_HOSTS:
        return True
    return is_custom_direct_link(url)


def looks_like_url(text: str) -> bool:
    return bool(_URL_RE.search(text))


def normalize_url(raw: str) -> str:
    """补协议头；顺带把平台的干净域名裁掉查询参数。"""
    url = raw.strip()
    if "://" not in url:
        url = "https://" + url
    host = extract_host(url)
    if host in CLEAN_QUERY_HOSTS and "?" in url:
        url = url.split("?")[0]
    elif "xiaohongshu" in url:
        kept = re.search(r"&host_id=(.*?)(?=&|$)", url)
        if kept:
            url = url.split("?")[0] + f"?host_id={kept.group(1)}"
    return url


def flv_usable(flv_url: str) -> bool:
    """抖音/TikTok 的 h265 FLV 浏览器侧不认，需要退回 HLS。"""
    codec = get_query_params(flv_url, "codec")
    return not (codec and codec[0] == "h265")


def warn_h265(platform_name: str) -> None:
    logger.warning(f"FLV 不支持 h265 编码，{platform_name} 将改用 HLS 源")


def platform_summary() -> list[dict[str, Any]]:
    """给界面用的平台清单。"""
    return [
        {
            "key": p.key,
            "name": p.name,
            "group": p.group,
            "overseas": p.overseas,
            "needs_proxy": p.needs_proxy,
            "audio_only": p.audio_only,
            "force_flv": p.force_flv,
            "custom": p.custom,
            "cookie_key": p.cookie_key,
            "cookie_slug": f"cookie_{p.cookie_key}" if p.cookie_key else "",
        }
        for p in PLATFORMS
    ]
