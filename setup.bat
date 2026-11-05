@echo off
setlocal
cd /d "%~dp0"

set "ENV_PY=%~dp0.venv311\Scripts\python.exe"
set "BASE_PY="
set "RESET_VENV="

if not exist "%ENV_PY%" goto FIND_PYTHON
"%ENV_PY%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1
if errorlevel 1 set "RESET_VENV=1"
if not defined RESET_VENV goto CHECK_PACKAGES

:FIND_PYTHON
where py >nul 2>&1
if not errorlevel 1 (
    py -3.11 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set "BASE_PY=py -3.11"
)
if defined BASE_PY goto CREATE_VENV

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set "BASE_PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
)
if defined BASE_PY goto CREATE_VENV

where winget >nul 2>&1
if errorlevel 1 goto NO_PYTHON
echo Installing Python 3.11 for this user...
winget install --id Python.Python.3.11 --exact --scope user --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto PYTHON_INSTALL_FAILED

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "BASE_PY="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
if not defined BASE_PY goto NO_PYTHON

:CREATE_VENV
echo Preparing the project Python 3.11 environment...
if defined RESET_VENV (
    %BASE_PY% -m venv --clear .venv311
) else (
    %BASE_PY% -m venv .venv311
)
if errorlevel 1 goto VENV_FAILED
set "ENV_PY=%~dp0.venv311\Scripts\python.exe"

:CHECK_PACKAGES
"%ENV_PY%" -c "import sys,importlib.metadata as m,pythoncom,win32com.client,reportlab; assert sys.version_info[:2] == (3,11); assert 308 <= int(m.version('pywin32').split('.')[0]) < 400; assert m.version('reportlab').split('.')[0] == '4'" >nul 2>&1
if not errorlevel 1 goto CHECK_PIP

echo Installing Outlook and PDF dependencies from requirements.txt...
"%ENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto PACKAGE_INSTALL_FAILED

:CHECK_PIP
"%ENV_PY%" -m pip check
if errorlevel 1 goto PACKAGE_INSTALL_FAILED
echo.
echo Setup complete. Python 3.11, pywin32, and reportlab are ready.
echo Groupware browser collection is optional and is not installed by this Outlook setup.
exit /b 0

:NO_PYTHON
echo Python 3.11 was not found and winget is unavailable.
echo Install Python 3.11 (64-bit), then run setup.bat again.
if not defined SETUP_NOPAUSE pause
exit /b 1

:PYTHON_INSTALL_FAILED
echo Python 3.11 could not be installed. Check winget access and retry.
if not defined SETUP_NOPAUSE pause
exit /b 1

:VENV_FAILED
echo Could not create .venv311. Check the Python 3.11 installation and folder permissions.
if not defined SETUP_NOPAUSE pause
exit /b 1

:PACKAGE_INSTALL_FAILED
echo Dependency installation or validation failed.
echo Check internet or your approved PyPI mirror, then run setup.bat again.
if not defined SETUP_NOPAUSE pause
exit /b 1
