@echo off
rem ============================================
rem  直接启动 UI 界面（pythonw 后台拉起，无控制台窗口）
rem  需要看报错/日志时请改用 启动.bat（带控制台）
rem ============================================
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%~dp0main.py"
    exit /b 0
)

where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%~dp0main.py"
    exit /b 0
)

echo [提示] 未找到 pythonw，改用带控制台方式启动（同 启动.bat）
python main.py
endlocal
