# 本地运行说明（DouyinLiveRecorder）

上游仓库：https://github.com/LyzenX/DouyinLiveRecorder （抖音直播录制工具，作者 Lyzen，版本 `20240625`）
本目录 = 上游 `main` 分支的干净克隆 + 本文件与两个启动脚本。

## 目录布局

```
DouyinLiveRecorder/
├── main.pyw                 # 入口：有终端 -> 命令行模式，无终端 -> GUI 模式
├── config.txt               # 配置（检测间隔、线程数等）
├── rooms.json               # 待监测房间列表
├── dylr/
│   ├── core/                # 业务核心：dy_api 接口、monitor 监测、recording 录制、abogus 签名
│   ├── gui/                 # tkinter 界面
│   ├── plugin/plugin.py     # 自定义钩子（开播推送等）
│   └── util/                # cookie / ffmpeg / 日志工具
├── .venv/                   # 【本地】Python 3.10 独立环境（未纳入 git）
├── download/                # 【本地】录制产物，flv + 弹幕 xml
├── logs/                    # 【本地】运行日志
├── 启动_GUI.bat             # 【本地新增】GUI 模式
└── 启动_命令行版.bat        # 【本地新增】命令行模式
```

## 环境（已创建，可直接用）

Python 依赖里 `protobuf` 是**老版本生成代码**（`dylr/core/dy_pb2.py` 由 protobuf 3.6 生成），
只能跑在 protobuf 3.x 上，因此使用 **Python 3.10 + protobuf 3.20.3**（3.20 是最后一个兼容旧生成代码的版本）。

```bash
# 环境已存在：.venv (CPython 3.10.21)
uv python install 3.10
uv venv --python 3.10 .venv
uv pip install --python .venv/Scripts/python.exe -i https://mirrors.aliyun.com/pypi/simple/ \
    "requests~=2.28.2" "websocket-client~=1.5.1" "protobuf==3.20.3" \
    "jsengine~=1.0.7.post1" "gmssl~=3.2.2" "quickjs~=1.19.4"
```

依赖清单与上游 `requirements.txt` 的唯一差异：`protobuf==3.6.0` → `protobuf==3.20.3`（其余一致）。

## 运行

- GUI：双击 `启动_GUI.bat`（或 `main.pyw`）
- 命令行：双击 `启动_命令行版.bat`

注意 `main.pyw` 靠 `sys.stdin.isatty()` 判断模式，因此**必须有真实终端**才会走命令行模式。

## 配置

`config.txt` 生效的键只有这几个（其余键会被警告并忽略）：

| 键 | 含义 |
|---|---|
| `debug` | 输出详细日志 |
| `check_period` / `check_period_random_offset` | 普通房间检测间隔 / 随机偏移（秒） |
| `important_check_period` / `..._random_offset` | 重要房间检测间隔 / 随机偏移 |
| `check_threads` | 检测线程数（建议 1-2） |
| `check_wait` | 每线程每个房间的检测间隔（秒） |
| `cli_key_l` | 命令行模式下按 L 查看正在录制的房间 |

房间在 `rooms.json` 中维护，字段：`id`(Web_Rid) / `name`(录制目录名) / `auto_record` / `record_danmu` / `important`
（`important=true` 会用独立线程高频检测，不要设太多）。

> ⚠️ **上游自带的 `rooms.json` 里预置了「瑞幸咖啡」「露娜的店」两个房间，且 `auto_record=true`。**
> 直接启动就会开始监测并录制这两个房间。不想录就先删掉或改成 `auto_record=false`。

## 验证记录

- 全部 12 个模块导入通过（含 GUI、弹幕、abogus 签名）。
- 实测接口链路：`auto_get_cookie()` 自动取到 `ttwid` → `get_live_state_json(945843973757)`
  返回真实房间数据（主播「瑞幸咖啡」、`status=2` 直播中、含 `FULL_HD1` 的 flv 拉流地址）。
- 首次请求会返回空 body（无 cookie），触发 5 次失败后自动获取 cookie 后恢复正常，属预期行为。

## 已知坑

1. **循环导入**：直接 `from dylr.core import dy_api` 会 ImportError，必须先 `import dylr.core.app`。
   用 `main.pyw` 启动不受影响。
2. **需要外部二进制**？不需要。上游有 `transcode_manager`/`ffmpeg_utils`，但本版本未接线
   （`configs` 里 auto_transcode 相关键已被注释），录制输出的是原始 flv，转码/修复请自行用录播姬或 ffmpeg。
3. **不要尝试用 conda 建环境**：本机 `conda create` 会抛 unexpected error，用 uv 即可。
4. 未发现任何遥测/上报逻辑，`plugin.on_*` 钩子默认都是空实现。

## 运行时产物隔离

`.venv/`、`__pycache__/`、`*.log` 通过 `.git/info/exclude` 本地忽略（**未改动上游 `.gitignore`**）；
`download/`、`logs/` 上游 `.gitignore` 已覆盖。`git status` 应保持干净。
