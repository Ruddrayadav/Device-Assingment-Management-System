@echo off
setlocal
cd /d "%~dp0"

echo ==========================================================
echo  STOPPING HHD DEVICE MANAGEMENT SERVER
echo ==========================================================

:: Find any process listening on port 8000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8000 ^| findstr LISTENING') do (
  echo Terminating process ID %%a on port 8000...
  taskkill /F /PID %%a >nul 2>&1
)

echo Done. Server has been stopped safely.
timeout /t 3 >nul

