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
echo.
echo [*] 依赖安装完成
pause
