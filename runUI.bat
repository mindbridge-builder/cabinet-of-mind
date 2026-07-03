@echo off
cd /d %~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

:: Python from PATH; override with a full path if you keep several versions.
set PYTHON=python

:: Kill a stale server if one is still holding the port.
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8381 " ^| findstr "LISTENING"') do (
    echo [cabinet] stopping stale process PID %%a...
    taskkill /PID %%a /F >nul 2>&1
)

echo [cabinet] starting server...
%PYTHON% server.py
pause
