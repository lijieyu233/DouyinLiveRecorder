# 本地运行说明（DouyinLiveRecorder）

上游仓库：<https://github.com/ihmily/DouyinLiveRecorder> —— 原作者 ihmily 的**活跃主线**，当前版本 **v4.0.7**（提交 `add187f8`，2025-11-03）。

本目录 = 上游 `main` 分支的干净克隆 + 本文件与两个启动脚本。

> ⚠️ **重要变更**：本目录此前克隆的是 `LyzenX/DouyinLiveRecorder`（旧 `dylr/` 结构，最后提交停在 2025-09，与原作者仓库**无共同提交历史**）。
> 现已切换为原作者仓库的 `src/` 结构版。旧版内容完整保留在本地分支 **`legacy-lyzenx`**，需要时 `git switch legacy-lyzenx` 即可回退。

## 目录布局

```
DouyinLiveRecorder/
├── main.py                  # 入口（脚本式，顶部 while True 主循环）
├── ffmpeg_install.py        # ffmpeg 检测/自动安装
├── msg_push.py              # 直播状态推送（钉钉/tg/邮箱/bark/ntfy/pushplus）
├── i18n.py + i18n/          # 国际化
├── src/
│   ├── spider.py            # 各平台直播数据抓取（40+ 平台，3400 行）
│   ├── stream.py            # 取真实拉流地址
│   ├── ab_sign.py           # 抖音 a_bogus 签名
│   ├── room.py / utils.py / logger.py / proxy.py
│   ├── initializer.py       # 检测/自动安装 Node.js
│   ├── http_clients/        # 同步/异步 HTTP
│   └── javascript/          # 各平台 JS 反混淆代码（需 Node 执行）
├── config/
│   ├── config.ini           # 【要改这里】录制设置 + Cookie + 推送
│   └── URL_config.ini       # 【要改这里】直播间地址，一行一个
├── downloads/               # 【本地】录制产物（.gitignore 已忽略）
├── logs/                    # 【本地】运行日志（.gitignore 已忽略）
├── backup_config/           # 【本地】配置备份
├── .venv/                   # 【本地】Python 3.11 隔离环境（.git/info/exclude 本地忽略）
├── 启动.bat                 # 【本地新增】双击运行
└── 安装依赖.bat             # 【本地新增】重建环境用
```

## 环境（已创建，可直接用）

**Python 3.11.16 + uv 隔离环境**（上游 `pyproject.toml` 要求 `>=3.10`，作者开发环境为 3.11）。

```bash
uv python install 3.11
uv venv --python 3.11 .venv
# --no-cache 必须带：受限环境下 uv 清理构建缓存会被拒，导致安装中断
uv pip install --no-cache --python .venv/Scripts/python.exe \
    -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
```

实测装入版本：requests 2.34.2 / loguru 0.7.3 / pycryptodome 3.23.0 / distro 1.9.0 /
tqdm 4.70.1 / httpx 0.28.1(+h2 4.4.1) / pyexecjs 1.5.1。

### 外部依赖（本机已具备，无需自动安装）

| 依赖 | 用途 | 本机状态 |
|---|---|---|
| **ffmpeg** | 实际录制、转 mp4/m4a、切片 | 系统 PATH 有 7.0.2 ✓ |
| **Node.js** | 执行 `src/javascript/*.js` 做签名 | 系统 PATH 有 v22.22.2 ✓ |

上游会在检测不到时自动下载（ffmpeg 走蓝奏云、Node 走 npmmirror）。本机两者都在 PATH，
`check_ffmpeg()` / `check_nodejs_installed()` 会直接判为已装，**不会触发任何下载**。

## 运行

- 双击 **`启动.bat`**（等价于 `.venv\Scripts\python.exe main.py`）
- v4 版**没有 GUI**，纯命令行界面，运行时打印实时状态表格
- 停止：`Ctrl+C`，或 Windows 下执行 `StopRecording.vbs`
- 只想停某个房间：在 `config/URL_config.ini` 该行前加 `#`
- 单房间单独设画质：行首写 `超清,https://live.douyin.com/xxxx`（逗号分隔）

## 配置

### `config/config.ini`（录制设置）

关键项（中文键名，`utf-8-sig` 编码）：

| 键 | 建议 |
|---|---|
| `直播保存路径(不填则默认)` | 留空 = `<项目>/downloads` |
| `视频保存格式ts\|mkv\|flv\|mp4...` | 上游推荐 **ts**（断流容错好） |
| `原画\|超清\|高清\|标清\|流畅` | 默认 `原画` |
| `循环时间(秒)` | 300，长挂建议调大，避免请求过频被风控 |
| `同一时间访问网络的线程数` | 3 |
| `是否使用代理ip(是/否)` + `代理地址` | 录国内平台关掉；录 TikTok 等填 `127.0.0.1:10808` |
| `是否录制完成后执行自定义脚本` | 默认否 |

> ⚠️ 上游 `config.ini` 里**自带一份公开的示例抖音 cookie**（`[Cookie]` → `抖音cookie`）。
> 录抖音建议换成自己浏览器里登录后的 cookie，示例 cookie 随时可能失效。

### `config/URL_config.ini`

只放直播间地址，一行一个：

```
https://live.douyin.com/945843973757
超清,https://live.douyin.com/123456789
#https://live.douyin.com/000000000     ← 加 # 表示暂不录制
```

## 验证记录（本次实测）

1. **模块导入**：12 个模块全部导入通过（src / initializer / logger / proxy / room / spider /
   stream / utils / ab_sign / msg_push / ffmpeg_install / i18n），Python 3.11.16。
2. **抖音取流链路**：`spider.get_douyin_web_stream_data()` + `stream.get_douyin_stream_url()`
   → 拿到真实房间「瑞幸咖啡」`status=2` 直播中，返回真实拉流地址
   （`pull-h5.douyincdn.com/.../index.m3u8` 与 `.flv`，原画档）。一次请求即成功，无风控拦截。
3. **端到端实跑**：`python main.py` 连续运行 80 秒，界面正常输出
   `正在录制1个直播: 序号1 瑞幸咖啡[原画] 正在录制中 0:01:11`，`瞬时错误数: 0`。
4. **产物真实性**：`ffprobe` 校验录制文件 → `h264 1080x1920` 视频 + `aac` 音频，与「原画」竖屏直播一致。

## 已知坑

1. **uv 安装依赖必须加 `--no-cache`**
   受限/沙箱环境下，uv 清理构建缓存目录会被拒（PyExecJS 只有源码包，构建时需要清理临时文件），
   报 `[safe-delete] SAFE_DELETE_FAIL_CLOSED ... windows-sandbox-recycle-bin-unavailable`。
   加 `--no-cache` 后一次通过。
2. **强杀 Python 会留下孤儿 ffmpeg 继续录制**
   用 `kill`/`timeout -s KILL` 结束 `main.py` 时，它 `subprocess` 拉起的 ffmpeg 不会随之退出，
   会继续往 downloads 里写文件（实测文件从 80MB 涨到 552MB）。正确停止方式是 `Ctrl+C` 或 `StopRecording.vbs`；
   一旦出现孤儿进程，用 `taskkill /F /IM ffmpeg.exe` 清掉。
3. **`config/config.ini` 与 `config/URL_config.ini` 是 git 跟踪文件**
   运行时程序会自动改写 `URL_config.ini`（例如给失效链接行首加 `#`）。改配置后 `git status` 会脏，
   属上游设计，提交前自行判断是否要带上配置改动。
4. **中文路径删除**（仅影响在受限沙箱里做清理的场景）
   删除含中文的目录时回收站机制会报 `trash-failed`，先把目录重命名为纯 ASCII 名再删即可。
5. **无遥测**：全仓库检索无 telemetry / sentry / analytics / 上报类调用；推送逻辑只走用户在
   `config.ini` 里显式填写的渠道，默认全空 = 不推送。

## 运行时产物隔离

- 本地忽略（`.git/info/exclude`，**未改动上游 `.gitignore`**）：`.venv/`、`__pycache__/`、`*.pyc`、`*.log`、`.venv.py310-legacy/`
- 上游 `.gitignore` 已覆盖：`downloads/`、`logs/`、`backup_config/`、`node/`
- 旧 LyzenX 版遗留的 Py3.10 环境保留在 `.venv.py310-legacy/`（本地忽略），确认不用后可删

## 版本切换记录

| 分支 | 内容 |
|---|---|
| `main` | ihmily 官方 v4.0.7（`add187f8`）+ 本文件与启动脚本 |
| `legacy-lyzenx` | 旧 LyzenX 版（`dylr/` 结构）工作树快照，含当时未完成的 `dylr/core/api` 重构 |

`origin` = `https://github.com/lijieyu233/DouyinLiveRecorder`（用户 fork）；
`upstream` = `https://github.com/ihmily/DouyinLiveRecorder`（原作者）。
两个远程**无共同提交历史**，拉取更新时不要直接 merge，按需 `git switch` 或重新基线。
