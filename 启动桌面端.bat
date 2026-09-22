@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================================
rem  DouyinLiveRecorder 桌面端启动脚本
rem  ---------------------------------------------------------------------------
rem  这里必须清掉两个环境变量：
rem    NODE_OPTIONS         宿主环境可能注入 --use-system-ca 等参数，Electron 不认
rem    ELECTRON_RUN_AS_NODE 若被置位，Electron 会退化成普通 Node，界面起不来
rem  cmd 里 `set VAR=` 表示删除该变量（不是设为空串），正好符合需要。
rem ============================================================================
set NODE_OPTIONS=
set ELECTRON_RUN_AS_NODE=

set ELECTRON=electron\node_modules\electron\dist\electron.exe

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

echo [2/2] 启动桌面端...
"%ELECTRON%" "%~dp0electron"

if errorlevel 1 (
    echo.
    echo [i] 正常启动失败，改用软件渲染重试（远程桌面 / 无显卡驱动的环境需要）...
    "%ELECTRON%" --disable-gpu --disable-gpu-compositing --disable-software-rasterizer "%~dp0electron"
)

if errorlevel 1 (
    echo.
    echo [!] 桌面端启动失败。可先用命令行模式确认后端是否正常：
    echo     .venv\Scripts\python.exe -m dylr --print-port
    echo     后端运行日志：logs\backend-console.log
    pause
)
