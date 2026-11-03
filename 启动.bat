@echo off
chcp 65001>nul
cd /d "%~dp0"

rem 找到真正的 Python（避开应用商店别名）
set "PYDIR="
set "TMPF=%TEMP%\fanqie_pydir.txt"
py -3 -c "import sys,os;print(os.path.dirname(sys.executable))" > "%TMPF%" 2>nul
if exist "%TMPF%" set /p PYDIR=<"%TMPF%"
if exist "%TMPF%" del "%TMPF%" >nul 2>nul

if not defined PYDIR (
    echo 没找到 Python，请先安装 Python 3.9 以上版本。
    pause
    exit /b
)

rem 界面依赖 pywebview（WebView2 渲染），缺了自动装
"%PYDIR%\python.exe" -c "import webview" >nul 2>nul
if errorlevel 1 (
    echo 首次运行，正在安装界面组件 pywebview ...
    "%PYDIR%\python.exe" -m pip install pywebview
)

if exist "%PYDIR%\pythonw.exe" (
    start "" "%PYDIR%\pythonw.exe" "%~dp0番茄小说下载器.py"
    exit /b
)
"%PYDIR%\python.exe" "%~dp0番茄小说下载器.py"
if errorlevel 1 pause
