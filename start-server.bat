@echo off
chcp 65001 >nul
title Manga-Overlay-Translator 后端
cd /d "%~dp0server"

REM 一键启动后端：双击本文件即可，无需手动 conda activate / cd。
REM 按 GPU 优先的顺序自动挑选 Python 解释器，直接用环境里的 python.exe，
REM 免去 conda activate 在批处理里初始化的麻烦。

set "PY="
REM 1) 项目自带的 venv（README 的标准装法）
if exist "%~dp0server\.venv\Scripts\python.exe" set "PY=%~dp0server\.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
REM 2) conda 环境：ocrt-vl(GPU) 优先，其次 ocrt-gpu，最后 ocrt(CPU)
if not defined PY if exist "%USERPROFILE%\.conda\envs\ocrt-vl\python.exe"  set "PY=%USERPROFILE%\.conda\envs\ocrt-vl\python.exe"
if not defined PY if exist "%USERPROFILE%\.conda\envs\ocrt-gpu\python.exe" set "PY=%USERPROFILE%\.conda\envs\ocrt-gpu\python.exe"
if not defined PY if exist "%USERPROFILE%\.conda\envs\ocrt\python.exe"     set "PY=%USERPROFILE%\.conda\envs\ocrt\python.exe"

if not defined PY (
  echo [!] 没找到可用的 Python 环境。
  echo     请先按 README 安装后端依赖，或确认 conda 环境 ocrt-vl 已建好。
  echo.
  pause
  exit /b 1
)

echo [*] 解释器: %PY%
echo [*] 启动后端 http://127.0.0.1:8765
echo [*] 保持本窗口开着 = 服务运行中；关闭窗口或按 Ctrl+C = 停止服务。
echo.
"%PY%" main.py

REM 服务退出后（正常停止或报错）停在这里，方便看信息
echo.
echo [*] 服务已停止。
pause
