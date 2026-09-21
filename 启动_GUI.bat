@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [错误] 未找到 .venv，请先按 LOCAL_SETUP.zh.md 创建环境并安装依赖。
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" main.pyw
