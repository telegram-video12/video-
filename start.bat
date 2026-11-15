@echo off
chcp 65001 >nul
cd /d %~dp0
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip
  .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env (
  copy .env.example .env >nul
  echo Fill in .env first, then run start.bat again.
  notepad .env
  exit /b
)
if not exist data\telegram.session (
  .venv\Scripts\python -m app.login
)
.venv\Scripts\python -m app
pause
