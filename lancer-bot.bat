@echo off
REM Double-clique sur ce fichier pour lancer le bot.
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Premiere installation...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)
if not exist config.toml copy config.example.toml config.toml
if not exist .env copy .env.example .env
python -m memebot
pause
