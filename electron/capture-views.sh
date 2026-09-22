#!/usr/bin/env bash
# ============================================================================
#  桌面端截图验收（开发用，不属于交付物）
#  ---------------------------------------------------------------------------
#  设计要点：
#  1. 不杀任何进程。每次抓图都是独立启动、抓完自己退出（见 main.js 的
#     scheduleCapture → forceQuit → app.quit）。用 taskkill 强杀会误伤用户
#     自己跑的 python/electron，也会触发权限确认。
#  2. 窗口全程不显示（main.js 的 CAPTURE_MODE），所以可以随时跑，
#     不会弹窗抢焦点。
#  3. 每个路由用独立的 --user-data-dir，避免单实例锁互相抢。
#
#  用法：bash electron/capture-views.sh [输出目录]
# ============================================================================
set -u

HERE="$(cd "$(dirname "$0")" && pwd -W)"          # 必须是 D:/... 形式：Electron 不认识 MSYS 的 /d/...
ROOT="$(cd "$HERE/.." && pwd -W)"
OUT_ARG="${1:-}"
OUT="$(cd "$ROOT" && pwd -W)/.workbuddy/tmp/shots"
if [ -n "$OUT_ARG" ]; then
  mkdir -p "$OUT_ARG"
  OUT="$(cd "$OUT_ARG" && pwd -W)"
fi
EXE="$HERE/node_modules/electron/dist/electron.exe"
COMMON=(--no-sandbox --disable-gpu --disable-software-rasterizer --disable-gpu-compositing)

# 路由与等待时间：后端冷启动约 4~9 秒，留足余量再截；
# 抓启动页时把延迟压到 3 秒以内才能落在引导界面上。
capture() {
  local route="$1" file="$2" delay="$3"
  echo "=== $file（route=${route:-启动页}，延迟 ${delay}ms）==="
  env -u ELECTRON_RUN_AS_NODE -u NODE_OPTIONS \
    "$EXE" "$HERE" "${COMMON[@]}" \
    --user-data-dir="$OUT/udata-$file" \
    --capture "$OUT/$file" --capture-delay "$delay" \
    ${route:+--capture-route "$route"} \
    > "$OUT/$file.log" 2>&1
  if [ -f "$OUT/$file" ]; then
    echo "    ok  $(stat -c%s "$OUT/$file") bytes"
  else
    echo "    失败，见 $OUT/$file.log"
    tail -5 "$OUT/$file.log"
  fi
}

mkdir -p "$OUT"

capture ""        boot.png       2600
capture dashboard dashboard.png  9000
capture tasks     tasks.png      9000
capture library   library.png    9500
capture settings  settings.png   9000
capture logs      logs.png       9000
capture about     about.png      9000

echo "=== 完成，输出在 $OUT ==="
