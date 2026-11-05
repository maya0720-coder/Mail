@echo off
cd /d "%~dp0"
set PY=
if exist ".venv\Scripts\python.exe" set PY=".venv\Scripts\python.exe"
if not defined PY (
    where py >nul 2>&1
    if not errorlevel 1 set PY=py -3
)
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set PY=python
)
if not defined PY (
    echo Python 3.11 or newer is required. See README.md.
    pause
    exit /b 1
)
if not exist config.json copy config.example.json config.json >nul
echo Open the local URL printed below in your browser after startup.
%PY% app.py serve
if errorlevel 1 pause
