@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo 尚未创建 Python 环境，请先在本目录运行：
    echo powershell -NoProfile -ExecutionPolicy Bypass -File management_tools.ps1 -Action init-env
    pause
    popd
    exit /b 1
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0management_tools.ps1" -Action run-app
set "appExitCode=%ERRORLEVEL%"
if not "%appExitCode%"=="0" pause
popd
exit /b %appExitCode%
