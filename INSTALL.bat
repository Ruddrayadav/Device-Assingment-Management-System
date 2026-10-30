@echo off
setlocal
cd /d "%~dp0"

echo ==========================================================
echo  HHD Device Management System - Windows Installation
echo ==========================================================

where py >nul 2>nul
if %errorlevel% neq 0 (
  where python >nul 2>nul
  if %errorlevel% neq 0 (
    echo [ERROR] Python 3.10 or newer was not found.
    echo Please install Python from https://www.python.org/
    echo IMPORTANT: Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
  ) else (
    set "PY_CMD=python"
  )
) else (
  set "PY_CMD=py -3"
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating isolated Python virtual environment (.venv)...
  %PY_CMD% -m venv .venv
  if %errorlevel% neq 0 (
    echo [ERROR] Failed to create virtual environment.
    pause
    exit /b 1
  )
) else (
  echo [1/3] Virtual environment already exists.
)

echo [2/3] Installing and upgrading required application packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %errorlevel% neq 0 (
  echo [ERROR] Package installation failed. Verify internet connection for initial setup.
  pause
  exit /b 1
)

echo [3/3] Initializing database and default accounts...
".venv\Scripts\python.exe" -c "from main import seed_initial_data_if_needed, Base, engine; Base.metadata.create_all(bind=engine); seed_initial_data_if_needed(); print('Database ready.')"

echo ==========================================================
echo  INSTALLATION COMPLETE!
echo  You can now start the application anytime by double-clicking Start.bat
echo ==========================================================
pause
