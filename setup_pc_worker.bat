@echo off
setlocal
cd /d "%~dp0" || (
  echo Cannot find the project folder.
  pause
  exit /b 1
)

echo.
echo AI Stock Assistant - PC worker setup
echo.

if not exist "requirements.txt" (
  echo requirements.txt was not found.
  echo Please run this file inside the ai-stock-assistant folder.
  pause
  exit /b 1
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3.12 -m venv .venv
  if errorlevel 1 py -3 -m venv .venv
) else (
  python -m venv .venv
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Failed to create Python virtual environment.
  echo Please install Python 3.12 and run this file again.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo.
  echo Failed to upgrade pip.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Failed to install requirements.
  pause
  exit /b 1
)

echo.
echo Setup complete.
echo Next: copy .env.pc.example to .env, fill it, then run run_pc_worker.bat.
echo.
pause
