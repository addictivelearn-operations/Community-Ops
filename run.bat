@echo off
rem Community Ops app - local run. First time: python -m pip install -r requirements.txt
rem Keep this window open while using the app (minimise it). Ctrl+C stops it;
rem the "Terminate batch job (Y/N)?" question that follows is Windows, not the
rem app - the server has already stopped by then, answer Y.
cd /d "%~dp0"
if not exist .env (
  echo No .env found. Copy .env.example to .env and fill it in first.
  pause
  exit /b 1
)
echo Community Ops - http://localhost:8000   (auto-restarts when code or .env changes)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-include .env
pause
