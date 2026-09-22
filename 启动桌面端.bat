@echo off
rem ============================================================================
rem  DouyinLiveRecorder 桌面端启动脚本
rem  ---------------------------------------------------------------------------
rem  【编码要求】本文件必须存为 GBK(936)，且不要在里面写 chcp 命令。
rem  cmd.exe 是按字节偏移逐行读取批处理文件的，中途用 chcp 切换代码页会让它
rem  的行边界错位：中文会被从中间截断、当成命令去执行，整个控制流都会乱掉。
rem  本文件此前用 UTF-8 + chcp 65001 就踩了这个坑——双击后窗口一闪而过，
rem  Electron 根本没被正确拉起，而且没有任何提示。
rem
rem  【必须清掉两个环境变量】
rem    NODE_OPTIONS         宿主环境可能注入 --use-system-ca 等参数，Electron 不认
rem    ELECTRON_RUN_AS_NODE 若被置位，Electron 会退化成普通 Node，界面起不来
rem  cmd 里 set VAR= 表示删除该变量（不是设为空串），正好符合需要。
rem
rem  【受限环境】远程桌面 / 虚拟机 / 被锁定的机器上，Chromium 的 GPU 进程可能
rem  因为建不了自己的沙箱而起不来，表现为双击后一闪而过。此时给这个脚本传
rem  DYLR_EXTRA_ARGS=--no-sandbox 即可（默认不加，因为不该由脚本替用户关沙箱）。
rem ============================================================================

cd /d "%~dp0"

set NODE_OPTIONS=
set ELECTRON_RUN_AS_NODE=

set ELECTRON=electron\node_modules\electron\dist\electron.exe
set LOGCONSOLE=logs\desktop-console.log

rem 桌面端要拉起 Python 后端，所以 .venv 是硬依赖，缺了就先说清楚
if not exist ".venv\Scripts\python.exe" (
    echo [!] 未找到 .venv 环境，桌面端需要它来运行录制服务。
    echo     请先双击「安装依赖.bat」。
    pause
    exit /b 1
)

if not exist "%ELECTRON%" (
    echo [1/2] 首次运行，正在安装桌面端依赖（Electron，约 250MB，需联网）...
    pushd electron
    call npm install --registry=https://registry.npmmirror.com
    set NPM_RESULT=%errorlevel%
    popd
    if not "%NPM_RESULT%"=="0" (
        echo.
        echo [!] 依赖安装失败。请确认已安装 Node.js 18+，或手动执行：
        echo     cd electron ^&^& npm install --registry=https://registry.npmmirror.com
        pause
        exit /b 1
    )
)

rem 桌面端窗口自己会显示后端启动进度，这里不必刷屏；
rem 但把它的原始输出落到文件，启动失败时才有据可查。
if not exist "logs" mkdir "logs"

echo [2/2] 启动桌面端...
echo     启动失败时请查看 %LOGCONSOLE%
"%ELECTRON%" %DYLR_EXTRA_ARGS% "%~dp0electron" > "%LOGCONSOLE%" 2>&1

rem errorlevel 0 = 正常退出（用户自己关掉窗口或选了退出），到此结束。
if not errorlevel 1 goto :eof

rem 非 0 说明压根没起来。先换软件渲染再试一次（真实显卡驱动异常的常见解法）。
echo.
echo [i] 正常启动失败，改用软件渲染重试...
"%ELECTRON%" %DYLR_EXTRA_ARGS% --disable-gpu --disable-gpu-compositing --disable-software-rasterizer "%~dp0electron" >> "%LOGCONSOLE%" 2>&1

if not errorlevel 1 goto :eof

echo.
echo [!] 桌面端启动失败。完整输出在：
echo     %~dp0%LOGCONSOLE%
echo.
echo     常见原因：
echo       1) 缺依赖  - 先双击「安装依赖.bat」
echo       2) 受限环境（远程桌面 / 虚拟机 / 被锁定的机器）下 Chromium 沙箱不可用，
echo          可以试：set DYLR_EXTRA_ARGS=--no-sandbox ^&^& 启动桌面端.bat
echo       3) 确认后端本身是否正常：
echo          .venv\Scripts\python.exe -m dylr --print-port
pause
