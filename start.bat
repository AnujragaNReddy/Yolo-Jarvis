@echo off
setlocal
cd /d "%~dp0"
title YOLO-JARVIS

rem Keep pip's temp files and cache on this drive (the C: drive is often full).
if not exist ".dev\tmp" mkdir ".dev\tmp"
set "TEMP=%~dp0.dev\tmp"
set "TMP=%~dp0.dev\tmp"
set "PIP_CACHE_DIR=%~dp0.dev\pip-cache"

if not exist "backend\.venv\Scripts\python.exe" (
  echo Setting up Python environment, first run only...
  python -m venv backend\.venv || (echo Python 3.11+ is needed: https://www.python.org/downloads/ & pause & exit /b 1)
  backend\.venv\Scripts\python.exe -m pip install -q --upgrade pip
  backend\.venv\Scripts\python.exe -m pip install -q -r backend\requirements.txt || (echo Package install failed. & pause & exit /b 1)
)

where ollama >nul 2>nul || (
  echo.
  echo Ollama is not installed. JARVIS needs it for its brain ^(free^): https://ollama.com/download
  echo Then run:  ollama pull qwen2.5:3b
  echo.
)

rem Ollama's tray app usually runs it already; start it quietly if not.
powershell -NoProfile -Command "try { Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 2 | Out-Null } catch { Start-Process ollama -ArgumentList serve -WindowStyle Hidden }" >nul 2>nul

set "PORT=8040"
if exist "backend\.env" for /f "tokens=1,2 delims==" %%a in ('findstr /b "JARVIS_PORT=" backend\.env') do set "PORT=%%b"

echo.
echo   YOLO-JARVIS  ->  http://localhost:%PORT%
echo   Open it in Chrome or Edge (they have speech recognition). Close this window to shut JARVIS down.
echo.
start "" "http://localhost:%PORT%"
cd backend
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port %PORT% --timeout-keep-alive 75
