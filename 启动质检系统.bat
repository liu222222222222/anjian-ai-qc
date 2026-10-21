@echo off
chcp 65001 >nul
title 安检照片 AI 质检系统
cd /d "%~dp0ai"

rem ============================================================
rem  自动探测 Python：优先项目内 venv，其次 PATH 里的 python
rem ============================================================
set PY=
if exist "%~dp0.venv\Scripts\python.exe" set PY=%~dp0.venv\Scripts\python.exe
if not defined PY if exist "%~dp0ai\.venv\Scripts\python.exe" set PY=%~dp0ai\.venv\Scripts\python.exe
if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY set PY=%%i
  )
)
if not defined PY (
  echo [错误] 未找到 Python。请安装 Python 3.9+ 并加入 PATH，
  echo        或在项目根目录创建 .venv 虚拟环境。
  pause
  exit /b 1
)

set PORT=8790
if not "%AJ_PORT%"=="" set PORT=%AJ_PORT%

echo [1/2] Python: %PY%
echo       端口  : %PORT%
start "AnjianQC Server" /min "%PY%" server.py

:wait
timeout /t 1 >nul
curl -s -m 2 "http://127.0.0.1:%PORT%/api/stats" >nul 2>&1
if errorlevel 1 goto wait

echo [2/2] 打开浏览器 ...
start "" "http://127.0.0.1:%PORT%/"

echo.
echo 服务已启动: http://127.0.0.1:%PORT%/
echo   总览      /
echo   实时看板  /live
echo   月度报告  /report
echo   人工复核  /review
echo   设置      /settings
echo.
echo 关闭本窗口不会停止服务；停服务请在任务管理器结束 python.exe。
echo ============================================
pause
