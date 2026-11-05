@echo off
cd /d "%~dp0"
set "SETUP_NOPAUSE=1"
call "%~dp0setup.bat"
if errorlevel 1 (
    pause
    exit /b 1
)
if not exist config.json copy config.example.json config.json >nul
echo Starting Mail Brain. An existing instance will be reused.
"%~dp0.venv311\Scripts\python.exe" app.py serve --open-browser
if errorlevel 1 pause
