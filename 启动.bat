@echo off
title 交易助手 - 调试控制台
rem ============================================
rem  调试启动：带控制台窗口运行，报错/日志直接可见
rem  日常启动请用桌面「交易助手」快捷方式（无黑窗）
rem ============================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 找不到 python，请确认已安装并加入 PATH。
    pause
    exit /b 1
)

python main.py
set "EC=%ERRORLEVEL%"
if not "%EC%"=="0" (
    echo.
    echo [提示] 程序异常退出，退出码 %EC%，请上翻查看报错信息。
    pause
)
endlocal & exit /b %EC%