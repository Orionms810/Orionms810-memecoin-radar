@echo off
REM Double-clique UNE FOIS pour connecter le bot a ton compte Telegram (lecture des chaines).
REM Arrete le bot avant (ferme sa fenetre).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Lance d'abord lancer-bot.bat une fois pour installer le bot.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m memebot telegram-login
pause
