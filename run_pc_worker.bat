@echo off
setlocal
cd /d "%~dp0" || (
  echo Cannot find the project folder.
  pause
  exit /b 1
)

if not exist "app\worker.py" (
  echo app\worker.py was not found.
  echo Please run this file inside the ai-stock-assistant folder.
  pause
  exit /b 1
)

if not exist ".env" (
  echo.
  echo .env file was not found.
  echo Copy .env.pc.example to .env and fill your real values first.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Python environment is not installed yet.
  echo Running setup_pc_worker.bat first.
  echo.
  call "%~dp0setup_pc_worker.bat"
)

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo Python environment is still missing. Stop.
  pause
  exit /b 1
)

set PYTHONUTF8=1

:loop
echo.
echo AI Stock Assistant - PC worker starting
echo Keep this window open. Closing it stops trading, account refresh, and Telegram alerts.
echo.
".venv\Scripts\python.exe" -m app.worker
echo.
echo PC worker stopped. Restarting in 30 seconds.
echo Close this window if you want to stop it completely.
timeout /t 30 /nobreak
goto loop
