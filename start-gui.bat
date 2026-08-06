@echo off
chcp 65001 >nul
REM 双击打开控制台 GUI。GUI 本身只用标准库 tkinter，随便哪个环境的 python 都能跑；
REM 它会在窗口里让你选 GPU/CPU 环境来启动后端。用 pythonw 隐藏这个黑窗口。

set "PYW="
if exist "%USERPROFILE%\.conda\envs\ocrt-vl\pythonw.exe"  set "PYW=%USERPROFILE%\.conda\envs\ocrt-vl\pythonw.exe"
if not defined PYW if exist "%USERPROFILE%\.conda\envs\ocrt-gpu\pythonw.exe" set "PYW=%USERPROFILE%\.conda\envs\ocrt-gpu\pythonw.exe"
if not defined PYW if exist "%USERPROFILE%\.conda\envs\ocrt\pythonw.exe"     set "PYW=%USERPROFILE%\.conda\envs\ocrt\pythonw.exe"
if not defined PYW set "PYW=pythonw"

start "" "%PYW%" "%~dp0server\gui.py"
