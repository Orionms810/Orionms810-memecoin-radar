@echo off
REM Double-clique pour installer la derniere version du bot depuis GitHub.
REM Garde tes fichiers : .env, config.toml, la base memebot.db et la session Telegram.
REM Ferme d'abord la fenetre du bot.
cd /d "%~dp0"
set "URL=https://github.com/Orionms810/Orionms810-memecoin-radar/archive/refs/heads/claude/memecoin-trading-bot-pumpfun-jbsdzq.zip"
set "ZIP=%TEMP%\memecoin-radar-maj.zip"
set "DEST=%TEMP%\memecoin-radar-maj"
echo Telechargement de la derniere version...
powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile '%ZIP%'" || goto erreur
if exist "%DEST%" rmdir /s /q "%DEST%"
powershell -NoProfile -Command "Expand-Archive -Force -LiteralPath '%ZIP%' -DestinationPath '%DEST%'" || goto erreur
set "SRC="
for /d %%D in ("%DEST%\*") do set "SRC=%%D"
if not defined SRC goto erreur
robocopy "%SRC%" "%CD%" /E /NFL /NDL /NJH /NJS /NP /XF .env config.toml memebot.db memebot.db-wal memebot.db-shm *.session *.session-journal mettre-a-jour.bat /XD .venv >nul
if errorlevel 8 goto erreur
if exist ".venv\Scripts\python.exe" (
  echo Installation des nouveaux modules...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto erreur
)
rmdir /s /q "%DEST%" 2>nul
del "%ZIP%" 2>nul
echo.
echo Mise a jour terminee. Lance le bot avec lancer-bot.bat
pause
exit /b 0

:erreur
echo La mise a jour a echoue. Envoie a Claude le texte affiche au-dessus.
pause
exit /b 1
