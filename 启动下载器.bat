@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "OPEN_BROWSER=1"
title Video Downloader - close this window to stop

set "PY="
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" set "PY=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
if not defined PY (
  py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  python -c "import sys" >nul 2>&1 && set "PY=python"
)

if not defined PY (
  echo.
  echo Python not found on this PC.
  echo Install Python 3.9+ from https://www.python.org/downloads/  ^(tick "Add to PATH"^)
  echo or download the portable build instead.
  echo.
  pause
  exit /b 1
)

echo Starting Video Downloader ...
%PY% app.py
if errorlevel 1 (
  echo.
  echo Failed to start. Press any key to exit.
  pause >nul
)
