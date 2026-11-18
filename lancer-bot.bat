@echo off
REM Double-clique pour lancer le bot. Installe ou repare tout seul ce qui manque.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Premiere installation, patiente 1 a 3 minutes...
  python -m venv .venv || goto pas_de_python
)
".venv\Scripts\python.exe" -c "import httpx, websockets, feedparser, dotenv, telethon, solders" 2>nul || (
  echo Installation des modules, patiente...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto erreur
)
if not exist config.toml copy config.example.toml config.toml >nul
if not exist .env copy .env.example .env >nul
".venv\Scripts\python.exe" -m memebot %*
pause
exit /b 0

:pas_de_python
echo Python est introuvable. Installe-le depuis https://www.python.org/downloads/
echo et coche "Add python.exe to PATH" pendant l'installation.
pause
exit /b 1

:erreur
echo L'installation a echoue. Envoie a Claude le texte affiche au-dessus.
pause
exit /b 1
