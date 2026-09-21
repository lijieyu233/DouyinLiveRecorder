@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [!] 未找到 .venv 环境，请先运行「安装依赖.bat」
    pause
    exit /b 1
)
".venv\Scripts\python.exe" main.py
echo.
echo [*] 程序已退出
pause
