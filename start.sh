#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "מלא את הקובץ .env ואז הרץ שוב ./start.sh"
  exit 0
fi
if [ ! -f data/telegram.session ]; then
  .venv/bin/python -m app.login
fi
exec .venv/bin/python -m app
