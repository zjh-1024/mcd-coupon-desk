@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "SCRIPT=%~dp0mcd-coupon-desk.py"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 Python。
  echo        请先安装 Python 3.10+，安装时勾选 "Add python.exe to PATH"。
  echo        下载地址：https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

rem 优先用 pythonw 启动：不留下黑色控制台窗口，直接开图形界面。
where pythonw >nul 2>nul
if errorlevel 1 (
  echo 提示：本机没有 pythonw，改用 python 启动 ^(会同时出现一个控制台窗口^)。
  python "%SCRIPT%" gui
  echo.
  pause
  exit /b %errorlevel%
)

start "mcd-coupon-desk" pythonw "%SCRIPT%" gui
exit /b 0
