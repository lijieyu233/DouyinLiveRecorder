@echo off
rem 编码要求：本文件必须存为 GBK(936)，且不要在里面写 chcp 命令。
rem 原因见「启动桌面端.bat」顶部注释：中途切代码页会让 cmd 的行边界错位。

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [!] 未找到 .venv 环境，请先双击「安装依赖.bat」。
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
echo.
echo [*] 程序已退出
pause
