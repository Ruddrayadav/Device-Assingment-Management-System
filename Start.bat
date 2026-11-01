@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ==========================================================
echo  STARTING HHD DEVICE MANAGEMENT SERVER
echo ==========================================================

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Virtual environment not found.
  echo Please run INSTALL.bat first to set up the system.
  pause
  exit /b 1
)

:: Get Server IP Address for Operator PCs
for /f "tokens=4" %%a in ('route print ^| findstr 0.0.0.0.*0.0.0.0 ^| findstr /v "Default"') do (
  set "SERVER_IP=%%a"
)

echo Server Host IP on LAN: !SERVER_IP!
echo.
echo ==========================================================
echo  ACCESS URLS:
echo  - Server Local:        http://127.0.0.1:8000
echo  - Operator PCs (LAN):  http://!SERVER_IP!:8000
echo  - Wall Display Board:  http://!SERVER_IP!:8000/board
echo ==========================================================
echo.
echo Press Ctrl+C in this window or run Stop.bat to stop the server.
echo.

".venv\Scripts\python.exe" main.py
if %errorlevel% neq 0 (
  echo.
  echo [ERROR] The server stopped unexpectedly (exit code: %errorlevel%).
  echo Please review any diagnostic messages above.
  echo For administrator password reset or account recovery, run:
  echo   .venv\Scripts\python.exe setup_admin.py
  echo.
)
pause

