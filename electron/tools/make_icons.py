# -*- coding: utf-8 -*-
"""生成桌面端图标资源（纯标准库，无第三方依赖）。

为什么自己画：项目原本一个位图资源都没有，Electron 窗口、托盘、安装包都缺图标，
系统会退化成 Electron 默认标。这里用与应用内标题栏 logo 完全相同的
「广播波 + 直播点」字形生成，保证窗口、托盘、安装包三处视觉一致。

输出（electron/assets/）：
  icon.png      256×256  窗口图标 / 通知图标
  icon.ico      16~256   安装包与任务栏（多尺寸封装）
  tray.png      16×16    托盘（Windows 实际按 16px 显示，单独按 16px 渲染才够锐）
  tray@2x.png   32×32    托盘 HiDPI 表示（Electron 按 @2x 约定自动选用）

用法：python electron/tools/make_icons.py
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# 取自 electron/src/renderer/styles.css 的设计变量，保证与界面同色
ACCENT_TOP = (0x6F, 0x9B, 0xFF)    # --accent-hover
ACCENT_BOTTOM = (0x35, 0x5C, 0xD6)  # 比 --accent 深一档，给图标一点体积感
GLYPH = (0xF7, 0xF9, 0xFF)
LIVE = (0xFF, 0x4D, 0x5E)          # --live

SS = 3  # 每像素子采样倍数（解析式抗锯齿之外再叠一层超采样，16px 下差别明显）


# --------------------------------------------------------------------- 距离场
def sd_rounded_rect(px: float, py: float, hw: float, hh: float, r: float) -> float:
    """圆角矩形有符号距离（负值在内部）。"""
    qx = abs(px) - (hw - r)
    qy = abs(py) - (hh - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return min(max(qx, qy), 0.0) + outside - r


def in_arc(angle: float, a0: float, a1: float) -> bool:
    """角度是否落在 [a0, a1] 内。

    不能直接比较——左弧跨 180°，而 atan2 对第三象限返回负角，
    直接比大小会把下半段弧误判成区间外（描边在那里断掉）。
    这里以 a0 为原点归一化到 [0, 2π) 再比，跨 ±π 也成立。
    """
    return (angle - a0) % (2 * math.pi) <= (a1 - a0)


def sd_arc(px: float, py: float, radius: float, half_w: float,
           a0: float, a1: float) -> float:
    """以原点为中心、带圆头的粗圆弧距离。角度为弧度，a0 < a1。"""
    r = math.hypot(px, py)
    if in_arc(math.atan2(py, px), a0, a1):
        return abs(r - radius) - half_w

    # 角度落在区间外：退化为到最近端点圆帽的距离，避免弧口被切平
    best = 1e9
    for a in (a0, a1):
        vx = radius * math.cos(a)
        vy = radius * math.sin(a)
        length2 = vx * vx + vy * vy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, (px * vx + py * vy) / length2))
        best = min(best, math.hypot(px - vx * t, py - vy * t) - half_w)
    return best


# --------------------------------------------------------------------- 绘制
def coverage(distance: float) -> float:
    """距离 → 覆盖率，形成约 1 像素宽的羽化边。"""
    return max(0.0, min(1.0, 0.5 - distance))


def blend(dst: list[float], src: tuple[int, int, int], alpha: float) -> None:
    """预乘 alpha 的 over 合成。dst 存的是预乘后的 rgba。"""
    if alpha <= 0:
        return
    inv = 1.0 - alpha
    dst[0] = src[0] * alpha + dst[0] * inv
    dst[1] = src[1] * alpha + dst[1] * inv
    dst[2] = src[2] * alpha + dst[2] * inv
    dst[3] = alpha + dst[3] * inv


def _geometry(size: float, simplified: bool) -> tuple[list[tuple[float, float, float]], float, float]:
    """返回 (弧段列表, 描边半宽, 直播点半径)，均已换算成像素。

    比例来自应用内 24×24 viewBox 的 logo：
    内弧 R=8.2/24、外弧 R=12.16/24、点 r=3.2/24、描边 1.8/24。
    simplified 用于 16/32 这类小尺寸——四条弧加细描边会糊成一团，
    减成两条弧并把描边加粗，缩到 16px 也能看出是「直播信号」。
    """
    if simplified:
        radii = (0.185, 0.335)
        half_stroke = 0.055 * size
        dot_r = 0.105 * size
    else:
        radii = (0.2025, 0.3000)
        half_stroke = 0.0300 * size
        dot_r = 0.0790 * size

    arcs: list[tuple[float, float, float]] = []
    for ratio in radii:
        radius = ratio * size
        arcs.append((radius, math.radians(135), math.radians(225)))   # 左
        arcs.append((radius, math.radians(-45), math.radians(45)))    # 右
    return arcs, half_stroke, dot_r


def render(size: int, *, with_background: bool, simplified: bool) -> list[bytearray]:
    """渲染一张图，返回逐行 RGBA 字节。"""
    s = float(size)
    center = s / 2.0
    arcs, half_stroke, dot_r = _geometry(s, simplified)

    rows: list[bytearray] = []
    inv_samples = 1.0 / (SS * SS)
    for py_index in range(size):
        row = bytearray()
        for px_index in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(SS):
                for sx in range(SS):
                    nx = px_index + (sx + 0.5) / SS - center
                    ny = py_index + (sy + 0.5) / SS - center
                    _accumulate(acc, inv_samples, nx, ny, s, with_background,
                                arcs, half_stroke, dot_r)
            alpha = acc[3]
            if alpha <= 1e-6:
                row += b"\x00\x00\x00\x00"
            else:
                # 反预乘：acc 存的是「颜色×覆盖」，除以总覆盖得到真实颜色
                row += bytes((
                    _byte(acc[0] / alpha),
                    _byte(acc[1] / alpha),
                    _byte(acc[2] / alpha),
                    _byte(alpha * 255),
                ))
        rows.append(row)
    return rows


def _accumulate(acc, weight, nx, ny, s, with_background, arcs, half_stroke, dot_r) -> None:
    """累积单个子采样点。acc 为预乘 rgba 累加器。"""
    local = [0.0, 0.0, 0.0, 0.0]

    if with_background:
        # 圆角方形底盘 + 自上而下的渐变
        cov = coverage(sd_rounded_rect(nx, ny, s / 2, s / 2, 0.235 * s))
        if cov <= 0:
            return
        t = max(0.0, min(1.0, (ny + s / 2) / s))
        blend(local, (
            _lerp(ACCENT_TOP[0], ACCENT_BOTTOM[0], t),
            _lerp(ACCENT_TOP[1], ACCENT_BOTTOM[1], t),
            _lerp(ACCENT_TOP[2], ACCENT_BOTTOM[2], t),
        ), cov)

    for radius, a0, a1 in arcs:                       # 广播波（白）
        blend(local, GLYPH, coverage(sd_arc(nx, ny, radius, half_stroke, a0, a1)))

    blend(local, LIVE, coverage(math.hypot(nx, ny) - dot_r))  # 直播点（红）

    if local[3] <= 0:
        return
    acc[0] += local[0] * weight
    acc[1] += local[1] * weight
    acc[2] += local[2] * weight
    acc[3] += local[3] * weight


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t + 0.5)


def _byte(value: float) -> int:
    return max(0, min(255, int(value + 0.5)))


# --------------------------------------------------------------------- 编码
def encode_png(size: int, rows: list[bytearray]) -> bytes:
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def encode_ico(entries: list[tuple[int, bytes]]) -> bytes:
    """把多张 PNG 封进 ICO 容器（Vista 起支持 PNG 负载）。"""
    header = struct.pack("<HHH", 0, 1, len(entries))
    offset = 6 + 16 * len(entries)
    directory = bytearray()
    payload = bytearray()
    for size, data in entries:
        directory += struct.pack(
            "<BBBBHHII",
            size if size < 256 else 0,
            size if size < 256 else 0,
            0, 0, 1, 32, len(data), offset,
        )
        payload += data
        offset += len(data)
    return bytes(header + directory + payload)


def build(size: int, *, simplified: bool | None = None) -> bytes:
    if simplified is None:
        simplified = size <= 24
    return encode_png(size, render(size, with_background=True, simplified=simplified))


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    print("渲染 icon.png (256)…")
    (ASSETS / "icon.png").write_bytes(build(256, simplified=False))

    print("渲染托盘图标 (16 / 32)…")
    (ASSETS / "tray.png").write_bytes(build(16, simplified=True))
    (ASSETS / "tray@2x.png").write_bytes(build(32, simplified=True))

    print("渲染 icon.ico (16~256)…")
    (ASSETS / "icon.ico").write_bytes(
        encode_ico([(size, build(size)) for size in (16, 24, 32, 48, 64, 128, 256)])
    )

    for name in ("icon.png", "icon.ico", "tray.png", "tray@2x.png"):
        path = ASSETS / name
        print(f"  {name:14s} {path.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
