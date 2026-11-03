@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title HHD Server Handover System - Installer
set "LOG=%~dp0install.log"

echo ==========================================================
echo  HHD DEVICE MANAGEMENT SYSTEM - WINDOWS INSTALLER
echo ==========================================================
echo.
echo Installer diagnostics will be saved to:
echo "%LOG%"
echo.

> "%LOG%" echo HHD Installer started: %DATE% %TIME%
>>"%LOG%" echo Working directory: %CD%

REM Find a supported Python launcher.
set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 (
    py -3 --version >>"%LOG%" 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)

if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python --version >>"%LOG%" 2>&1
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

if not defined PYTHON_CMD (
    echo [ERROR] Python was not found.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo During installation, enable "Add Python to PATH".
    >>"%LOG%" echo ERROR: Python launcher not found.
    goto :FAILED
)

echo [OK] Python launcher found: %PYTHON_CMD%
echo [1/4] Checking Python version...
%PYTHON_CMD% -c "import sys; print(sys.version); raise SystemExit(0 if (sys.version_info[0]*100+sys.version_info[1]) >= 310 else 1)" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.10 or newer is required. See install.log.
    goto :FAILED
)

echo [2/4] Creating virtual environment...
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv >>"%LOG%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Could not create the virtual environment. See install.log.
        goto :FAILED
    )
) else (
    echo [OK] Existing virtual environment found.
)

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment Python executable is missing.
    >>"%LOG%" echo ERROR: .venv\Scripts\python.exe missing after venv creation.
    goto :FAILED
)

echo [3/4] Installing dependencies. This may take a few minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [NOTE] pip self-upgrade skipped or offline. Proceeding with dependency installation... >>"%LOG%"
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Dependency installation failed. See install.log for details.
    goto :FAILED
)

echo [4/4] Initializing and checking the application database...
".venv\Scripts\python.exe" -c "from app.database import Base, engine; from app.migration import run_schema_migration; Base.metadata.create_all(bind=engine); run_schema_migration(); print('Database initialized successfully.')" >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [ERROR] Database initialization failed. See install.log.
    goto :FAILED
)

echo.
echo ==========================================================
echo  INSTALLATION COMPLETED SUCCESSFULLY
echo ==========================================================
echo.
echo  IMPORTANT:
echo  Never copy or commit the data folder; a new install must
echo  start with an empty data folder so /setup runs.
echo.
echo Next:
echo  1. Double-click Start.bat to start the server.
echo  2. Keep the server window open.
echo  3. Open http://127.0.0.1:8000/setup in a browser.
echo  4. Create the first Administrator account.
echo.
echo Full installer log: "%LOG%"
echo.
pause
exit /b 0

:FAILED
echo.
echo ==========================================================
echo  INSTALLATION FAILED - THE WINDOW WILL STAY OPEN
echo ==========================================================
echo Please read install.log in this folder and share it if you
echo need help. No existing database or data folder was deleted.
echo.
echo Log: "%LOG%"
echo.
pause
exit /b 1
