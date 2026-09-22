# 重构说明（v5.0）

本文记录 DouyinLiveRecorder 这一次重构的**问题清单、设计决策与验证方式**。
录制核心来自上游 <https://github.com/ihmily/DouyinLiveRecorder>（v4.0.7），
重构不改变「拉流—录制—转码—推送」的行为，改的是**代码结构**与**可交互性**。

```
┌──────────────────────────────────────────────────────────────────────┐
│  Electron 桌面端（electron/）                                         │
│  概览 · 任务 · 录制文件 · 设置 · 日志 · 环境                            │
│  ── HTTP + SSE ──▶  127.0.0.1:<随机端口>  （令牌校验，仅本机可访问）      │
└──────────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────────┐
│  dylr —— 控制核心（新增，纯标准库）                                     │
│  service   进程装配 + 终端面板                                        │
│  api       HTTP + SSE 接口                                            │
│  manager   任务编排、并发自适应、磁盘保护、配置轮询                       │
│  recorder  单直播间线程（原 start_record）                             │
│  platforms 平台注册表（原 40 分支 if/elif）                             │
│  config    字段元数据 + 类型化读写（唯一真相源）                          │
│  urlstore  URL_config.ini 解析/回写                                    │
│  ffmpeg    命令构建 / 转码 / 切片 / 字幕                                │
│  notify    推送渠道                                                    │
│  files     录制产物浏览 / 在线播放                                      │
│  events    事件总线 + 日志桥接                                          │
└──────────────────────────────────────────────────────────────────────┘
                              │
┌──────────────────────────────────────────────────────────────────────┐
│  src/ —— 上游取流层（保留，仅修路径与日志的健壮性问题）                    │
│  spider（40+ 平台抓取）· stream · ab_sign · proxy · utils · logger       │
└──────────────────────────────────────────────────────────────────────┘
```

命令行模式仍然可用：`python main.py`（或双击 `启动.bat`），行为与上游一致，
只是内部改由 `dylr.service` 驱动，与桌面端**共用同一套核心**。

---

## 一、原状的问题（都有具体位置）

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| 1 | 顶层 `while True` **没有休眠**，每轮完整读取 `config.ini` 100+ 次、重解析并重写 URL 文件 | `main.py:1783-2154` | 常驻高 IO 忙循环，CPU/磁盘空转 |
| 2 | `start_record()` 单个函数 1100 行，内含 460 行 `if record_url.find(...) > -1` 平台分支 | `main.py:545-1645` | 新增平台要在巨函数中间插分支；无法单测 |
| 3 | 同一平台的属性散落在 **7 张互不相干的清单**里：`get_record_headers` 字典、`is_flv_preferred_platform`、`only_flv_platform_list`、`only_audio_platform_list`、`re_plat`、`http_record_list`、`overseas_platform_host` | `main.py:514-542,1211-1227` 等 | 改一个平台要同时记住 7 处，漏一处就是线上 bug |
| 4 | 配置读取是 130 行平铺的 `read_config_value(...)`，键名是中文长串 | `main.py:1802-1925` | 拼错键名不报错、静默退回默认值；前端/脚本无法复用同一份定义 |
| 5 | 全局代理探测在**模块导入阶段**同步执行 `urlopen(google, timeout=15)` | `main.py:1763-1781` | 无代理时启动被卡十几秒 |
| 6 | 所有运行期状态是模块级全局变量（`recording`/`error_count`/`running_list`/`first_run`…） | `main.py:47-67` | 无法并存两个实例、无法测试、线程间隐式耦合 |
| 7 | 保存配置走 `ConfigParser.write()` | `main.py:1749` | 每次保存都把 `# 可选微信\|钉钉…` 这类注释整段吃掉，产生巨大无意义 diff |
| 8 | 日志文件不可写时在模块顶层抛 `PermissionError` | `src/logger.py:21-43` | **整个程序起不来**，而日志本不该是硬依赖 |
| 9 | 路径全部由 `os.path.realpath(sys.argv[0])` 反推 | `src/logger.py:19`、`src/__init__.py:10`、`ffmpeg_install.py:21`、`src/initializer.py:22`、`i18n.py:16` | 以 `python -m` 方式启动时 `logs/`、`node/`、`ffmpeg/`、`i18n/` 全部错位 |
| 10 | 强杀 Python 会留下孤儿 ffmpeg 继续写盘 | 上游已知问题（`LOCAL_SETUP.zh.md` 第 116 行） | 磁盘被悄悄写满 |
| 11 | 终端界面靠 `os.system('cls')` + `print` 拼表格 | `main.py:90-134` | 无图形界面，无历史记录，只能盯着黑框 |
| 12 | 没有任何接口，只能改 ini 文件 + 重启 | — | 无法做界面，也无法集成到其它流程 |

---

## 二、改造后的结构

### 2.1 平台注册表取代 if/elif 链

每个平台收敛成 `PlatformSpec` 的一条记录（`dylr/platforms.py`）：

```python
PlatformSpec(
    "douyin", "抖音直播", ("douyin.com/",), _p_douyin,
    flv_preferred=True, cookie_key="douyin",
)
PlatformSpec(
    "pandatv", "PandaTV", ("www.pandalive.co.kr/",), _p_pandatv,
    group="海外", needs_proxy=True, overseas=True, log_m3u8=True,
    header="origin:https://www.pandalive.co.kr", cookie_key="pandatv",
)
```

上面这一条同时表达了原来分散在 7 处的信息：**怎么抓**（`_p_pandatv`）、
**要不要代理**（`needs_proxy`）、**是否海外**（`overseas` → ffmpeg 放宽超时）、
**日志用哪个地址**（`log_m3u8`）、**请求头**（`header`）、**Cookie 键**（`cookie_key`）。

`resolve(url)` 按表顺序匹配——与上游 if/elif 的顺序严格一致，保证匹配结果不变。

### 2.2 配置：一份字段表同时驱动后端与界面

`dylr/config.py` 里每个配置项声明一次：

```python
_rec("video_save_type", "视频保存格式ts|mkv|flv|mp4|mp3音频|m4a音频", "TS", "choice", "录制",
     label="保存格式", choices=("TS", "MKV", "FLV", "MP4", "MP3音频", "M4A音频"),
     help="TS 断流容错最好，推荐长挂；MP3/M4A 只保留音频")
```

由此得到：

* Python 侧按 slug 取值（`cfg.text("video_save_type")`），不再出现中文字符串字面量；
* `/api/config` 把同一张表下发给界面，设置页由它渲染——**后端改键名，界面自动跟上**；
* 类型、范围、枚举在写入前校验（`循环检测间隔 不能大于 86400`）；
* 写回采用逐行替换，只动变化的那一行，`git diff` 精确到 1 行；
* `secret` 字段出参自动脱敏（`ttwi********9o=`），界面回传脱敏值时不会覆盖真实密钥。

### 2.3 录制任务：从全局变量到可观察对象

`dylr/recorder.py::RecorderTask` 是上游 `start_record()` 的等价实现：

* 配置在任务启动时快照为 `RuntimeSettings`（与原语义一致：改配置需重启任务）；
* 状态机 `idle → checking → waiting/live → recording → …`，每次变化都推事件；
* 输出体积每 2 秒统计一次（`_start_size_watcher`），界面能看到「正在写入」；
* `stop()` 先给 ffmpeg 发退出指令（Windows 写 `q` 到 stdin / POSIX 发 SIGINT）再等待，
  解决上游「强杀留下孤儿 ffmpeg」的问题。

### 2.4 管理器：忙循环 → 按变更轮询

`dylr/manager.py` 用 5 秒轮询 + `mtime` 比对替代上游的无休眠循环：

* URL 文件或 `config.ini` 没变就什么都不做；
* 代理探测挪到后台线程，启动不再被网络阻塞；
* 并发自适应（`_tuner_loop`）与磁盘水位保护（`_guard_disk`）保留原逻辑；
* 所有状态集中在一个对象上，`snapshot()` 一次性给出界面需要的全部数据。

### 2.5 接口（无新依赖）

`dylr/api.py` 只用标准库 `http.server`：上游的依赖里没有 web 框架，为了一个本地
界面引入 FastAPI/uvicorn 会显著增加安装体积与版本冲突面（用户环境常常是离线/受限网络）。

* 只监听 `127.0.0.1`，随机端口 + 随机令牌（`X-DYLR-Token` 请求头，SSE 用 `?token=`）；
* `GET /api/events` 是 SSE 长连接，断线自动重连并回放断线期间的事件；
* `GET /api/media` 支持 `Range`，桌面端可以直接拖动进度条播放录制文件；
* 文件类接口全部经 `MediaLibrary.resolve()` 限制在录制目录内，杜绝 `../` 穿越。

接口一览：`/api/health`、`/api/state`、`/api/env`、`/api/platforms`、`/api/events`、
`/api/logs`、`/api/config`(GET/PUT)、`/api/config/test-push`、`/api/tasks`(增删改查)、
`/api/tasks/reorder`、`/api/tasks/{id}/action`、`/api/control`、`/api/probe`、
`/api/files`、`/api/files/delete`、`/api/files/reveal`、`/api/media`、`/api/disk`、`/api/quit`。

---

## 三、行为保持：哪些是「逐项对齐」的

重构不是重写业务逻辑，以下细节与上游**完全一致**（含参数顺序与取值）：

* ffmpeg 输入参数集：`-v verbose`、`-rw_timeout/-analyzeduration/-probesize/-bufsize`、
  固定 UA、`-protocol_whitelist rtmp,crypto,file,http,https,tcp,tls,udp,rtp,httpproxy`、
  `-fflags +discardcorrupt`、`-re`、`-reconnect_streamed -reconnect_at_eof`、
  `-max_muxing_queue_size`、`-correct_ts_overflow 1`、`-avoid_negative_ts 1`；
  海外平台（16 个）使用放宽后的一组值；
* 各保存格式的输出参数（TS/MKV/FLV/MP4/MP3/M4A、分段与不分段共 10 套）；
* 文件名模板 `{主播}_{标题_}{时间}{_%03d}.{ext}`、目录结构
  （平台 / 作者 / 日期 / 标题 的组合规则）；
* 循环间隔策略：`±5 秒抖动`、瞬时错误 > 20 时 `+60 秒`、刚录完先 `30 秒`复查；
* 抖音/TikTok 的 FLV 优先策略与 h265 回退 HLS/TS；
* shopee 强制 `http` + `origin` 头、花椒与 shopee 直连下载 FLV、
  猫耳FM 与 Look 仅录音频、Blued/PandaTV/WinkTV/PopkonTV/FlexTV/17Live/浪Live 的请求头；
* SOOP/FlexTV 的 `new_cookies`、PopkonTV 的 `new_token`、TwitCasting 的 `new_cookies` 自动回写；
* 首次拿到主播名后回填 `URL_config.ini` 的「主播:」字段；
* 推送的 7 个渠道与文案占位符 `[直播间名称]`/`[时间]`、开播/关播去重逻辑。

## 四、有意修正的缺陷（与上游行为不同的地方）

| # | 修正 | 理由 |
|---|---|---|
| 1 | 忙循环 → 5 秒变更轮询 | 原实现空转磁盘与 CPU |
| 2 | 配置保存只改变化的行、保留注释 | 原实现每次保存都丢注释 |
| 3 | 日志文件不可写时降级为「换文件名 → 只写终端」 | 原实现直接让程序无法启动 |
| 4 | 路径统一由 `src/paths.py` 解析（支持 `DYLR_HOME`） | 模块方式启动不再错位 |
| 5 | 代理探测改为后台线程 | 原实现会阻塞启动 15 秒 |
| 6 | 停止时先优雅结束 ffmpeg 再退出 | 原实现会留下孤儿 ffmpeg 写盘 |
| 7 | 空字符串布尔项按声明默认值处理 | 上游 `options.get("", False)` 让「SSL 加密」默认值形同虚设 |
| 8 | 未识别平台 → 任务标记为「异常」并给出原因 | 上游直接 `return` 结束线程，界面/终端只会看到一行错误 |
| 9 | `retry` 变量（只自增、从未被读）删除 | 死代码 |
| 10 | 聚合错误计数不再因 `port_info` 类型变化而重复计数 | 上游对同一失败会记两次错误，导致并发被过度下调 |

> 保留的「上游怪癖」：`视频保存格式` 配成 TS 时，猫耳FM/Look 这类纯音频平台会写出
> 扩展名为 `.mp3`、实际容器为 MPEG-TS 的文件（上游同此行为，VLC 可正常播放）。

---

## 五、桌面端（electron/）的交互设计

### 交互原则

1. **状态永远可见**：标题栏常驻服务状态点；录制中的任务在侧栏有计数徽章；
   断线时顶部出现可重连的提示条；每个卡片自己说明「现在在干什么、下一步什么时候发生」。
2. **危险操作必须二次确认**：删除任务/文件、暂停全部录制、退出应用（有任务在录时）
   都会说明后果再执行。
3. **不让用户等看不清的东西**：所有异步按钮点击后立即进入转圈禁用态；
   列表加载用骨架屏；录制体积每 2 秒增长，而不是让用户猜有没有在写；
   启动页显示后端**真实上报**的里程碑与已用时长（见 5.2），不是装饰性进度条。
4. **不骗人**：数据没拿到就显示占位或「未配置」，不显示假的默认值；
   开关是乐观更新（先翻转，失败回滚并提示）；错误原文（含行号）直接展示在任务行上。
5. **少打字**：粘贴地址即自动预检（平台、是否需代理、是否缺 Cookie、是否重复）；
   画质、启停、排序、批量删除都在列表上直接完成。
6. **键盘可达**：`Ctrl+1..6` 切页、`Ctrl+N` 新增、`Ctrl+K` 搜索、`Ctrl+S` 保存、
   `Esc` 关闭弹窗；排序支持拖拽。
7. **能挂机**：关窗不中断录制，托盘常驻并显示实时状态，开播/录完/异常有系统通知
   （见 5.3）。

### 5.2 启动引导：只显示后端真实做过的事

上游后端在 `--print-port` 模式下，就绪之前**一行都不输出**，界面只能显示一个静止的
转圈，用户无法区分「在自检」和「已经卡死」。更糟的是：ffmpeg 自检原本只写在终端
banner 里，而这条路根本不走 banner——也就是说「启动时自检过了」是句空话，缺 ffmpeg
要等到真正开始录制才暴露。

现在后端用一行一个里程碑的方式上报（`dylr/service.py`）：

```
DYLR_STAGE 正在准备运行环境
DYLR_STAGE 正在检查 ffmpeg 与 Node.js
DYLR_STAGE 注意：未检测到 ffmpeg，录制会失败；请把 ffmpeg 加入 PATH 或放到项目的 ffmpeg/ 目录
DYLR_STAGE 正在启动本地控制接口
DYLR_STAGE 正在载入监控任务
DYLR_STAGE 就绪：52 个平台 · 2 个监控任务
DYLR_READY port=65393 token=…
```

* 解析放在 `electron/src/backend.js`，与 `DYLR_READY` 的解析放在一起；
  里程碑不混进启动日志面板——后者是原始输出，前者是结构化进度；
* 渲染层晚于后端启动时，`backend:status` 快照里带着已收到的里程碑，可以补齐；
* 自检发现的问题前缀为 `注意：`，界面用告警色而非完成的绿色；
* 界面里没有自己编的步骤：`_stage()` 的每一条都对应后端实际执行的动作，
  为此把 `Service.start()` 拆成了 `start_api()` / `start_manager()` 两段。

### 5.3 桌面集成：托盘、系统通知、窗口状态

这一层是「录制工具能不能真正挂机」的分水岭。

| 能力 | 实现 | 为什么用户需要它 |
|---|---|---|
| 系统托盘 | `electron/src/tray.js` | 原实现关窗即退出，录制直接断。托盘菜单提供显示/隐藏、开始/暂停全部、打开录制目录、退出；悬浮提示实时显示「录制中 N · 等待 M」 |
| 关闭到托盘 | 偏好 `desktop.closeToTray`（默认开） | 关窗只是收起窗口，录制继续；收起时给一次气泡提示，避免用户以为程序已经退出 |
| 系统通知 | `electron/src/notifications.js` | 开播时刻无法预测，这是最容易漏录的一环。开播、录制结束（含时长与体积）、任务异常、磁盘告警都会弹系统通知 |
| 窗口状态记忆 | `prefs.windowBounds()` | 记住尺寸/位置/最大化；多显示器拔插后校验可见区域交集，避免窗口跑到屏幕外「消失」 |
| 应用图标 | `electron/tools/make_icons.py` | 生成窗口/托盘/安装包三处图标，字形与应用内标题栏 logo 一致 |

两个刻意的设计决定：

* **「要不要打扰用户」由主进程判断**：只有主进程能可靠知道窗口是否在前台/是否被隐藏。
  渲染层只负责说「发生了什么」，主进程按偏好决定弹不弹。默认窗口在前台时不弹系统
  通知（界面内已有提示条），可以在设置页改成也弹。
* **首次见到某个任务只登记、不通知**：否则每次启动程序，正在录制的任务都会各弹一条
  「开始录制」，比不通知更打扰。同内容 5 秒内也只弹一次。

偏好存在 `app.getPath('userData')/desktop-prefs.json`，**不写进 `config/config.ini`**
——后者是录制核心的配置，会被命令行模式和上游逻辑读写，混入界面开关会污染它。
设置页的「桌面端」分组是即时生效的，因此不参与底部那条「未保存」队列，
页头也不会出现一个按不动的「保存」按钮。

另外 `backgroundThrottling: false` 是必需的：窗口收进托盘后 Chromium 默认会给隐藏窗口
的计时器降频，而这个应用在托盘状态下仍要按秒更新任务状态、及时弹通知。

### 页面

| 页面 | 关键内容 |
|---|---|
| 概览 | 全局启停总控、4 项环境状态、5 个统计卡、录制中卡片（秒级计时 + 体积 + 文件名 + 停止）、最近动态流 |
| 任务 | 地址粘贴预检、筛选/搜索、画质下拉、启停开关、重启/备注/删除、拖拽排序、错误原文 |
| 录制文件 | 按平台→主播聚合、体积/时间、内置播放器（Range 拖动）、定位/复制/删除、多选批量删除、磁盘占用 |
| 设置 | 顶部「桌面端」本地偏好分组（关窗行为、系统通知，即时生效）；其余为后端字段元数据渲染的完整表单、分组导航、脏值标记、范围校验、脱敏字段可显示、推送渠道一键测试、保存后提示哪些项需重启任务 |
| 日志 | 实时流、级别过滤、搜索、暂停、自动滚动、复制/导出 |
| 环境 | Python/ffmpeg/Node 版本自检、目录与打开、52 个平台清单（含需代理/Cookie 状态）、重启后端 |

### 验证方式（可复现）

```bash
cd electron
npm install
# 正常启动
npx electron .
# 无人值守环境（远程桌面 / 无显卡）：加软件渲染参数
npx electron . --disable-gpu --disable-software-rasterizer --disable-gpu-compositing
# 自动截图某个页面后退出（开发/验收用；此模式下窗口全程不显示，不会打扰正在用电脑的人）
npx electron . --capture out.png --capture-route tasks --capture-delay 9000
# 一次抓全部页面
bash capture-views.sh [输出目录]
```

> Windows 上用命令行启动前，务必清掉 `NODE_OPTIONS` 与 `ELECTRON_RUN_AS_NODE`
> 两个环境变量（`启动桌面端.bat` 已经处理）：前者会让 Electron 拒绝启动，
> 后者会让 Electron 退化成普通 Node，界面起不来。
>
> 抓图脚本**不使用 taskkill**：每次抓图都是独立启动、抓完自己退出。用 `taskkill`
> 强杀会误伤用户自己跑的 python/electron，也会触发系统权限确认。

---

## 六、验证记录（本次实测）

| 项目 | 结果 |
|---|---|
| 模块导入 | `dylr.*` 全部通过，Python 3.11.16 |
| 平台匹配 | 52 个平台，13 组真实 URL 全部命中预期平台，重复 key 数 0 |
| 配置读写 | 119 个字段、14 个分组；改一项后 `git diff config/config.ini` **0 行**（写回后完全还原） |
| 接口 | `health` 免鉴权 200、`/api/state` 无令牌 401、任务增删改查与排序、配置校验（非法值返回 400 且不落盘）、文件列表与磁盘信息全部通过 |
| 录制链路 | 真实启动录制抖音直播间「瑞幸咖啡」，`downloads/抖音直播/瑞幸咖啡/` 产出 TS 分段并自动转 MP4（本次运行累计 328MB） |
| 界面 | 6 个页面逐一截图核对（见 `.workbuddy/tmp/v-*.png`），包含真实录制中的任务卡、错误态、暂停态 |
| 静态检查 | 全部渲染层/主进程 JS 通过 `node --check` |
