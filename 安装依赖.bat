@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [*] 使用 uv 创建 Python 3.11 隔离环境并安装依赖...
uv venv --python 3.11 .venv
if errorlevel 1 (
    echo [!] 创建虚拟环境失败，请确认已安装 uv（https://docs.astral.sh/uv/）
    pause
    exit /b 1
)
rem --no-cache：跳过 uv 构建缓存，避免在受限环境下清理缓存目录被拒导致安装失败
uv pip install --no-cache --python ".venv\Scripts\python.exe" -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
if errorlevel 1 (
    echo [!] 依赖安装失败
    pause
    exit /b 1
)
echo.
echo [*] Python 依赖安装完成

rem 桌面端依赖（Electron）体积较大，装过就跳过
if not exist "electron\node_modules\electron\dist\electron.exe" (
    echo [*] 正在安装桌面端依赖（Electron，约 250MB）...
    pushd electron
    call npm install --registry=https://registry.npmmirror.com
    set NPM_RESULT=%errorlevel%
    popd
    if not "%NPM_RESULT%"=="0" (
        echo [!] 桌面端依赖安装失败，可稍后手动执行：cd electron ^&^& npm install
    ) else (
        echo [*] 桌面端依赖安装完成
    )
) else (
    echo [*] 桌面端依赖已存在，跳过
)
echo.
echo 完成。双击「启动.bat」使用命令行模式，双击「启动桌面端.bat」使用图形界面。
pause
