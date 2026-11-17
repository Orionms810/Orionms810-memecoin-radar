#!/usr/bin/env bash
# Installe Memecoin Radar sur un serveur Linux (Debian / Ubuntu) et le lance
# comme un service : il redémarre tout seul en cas de plantage ou de reboot.
#
# Usage (sur le serveur) :
#   curl -fsSL https://raw.githubusercontent.com/Orionms810/Orionms810-memecoin-radar/claude/memecoin-trading-bot-pumpfun-jbsdzq/deploy/install-serveur.sh | bash
set -euo pipefail

REPO="https://github.com/Orionms810/Orionms810-memecoin-radar.git"
DIR="$HOME/memecoin-radar"
RUN_USER="$(id -un)"

echo "==> Paquets système (Python, git)"
sudo apt-get update -y
sudo apt-get install -y git python3 python3-venv python3-pip

echo "==> Code du bot dans $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

echo "==> Environnement Python"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

[ -f config.toml ] || cp config.example.toml config.toml
[ -f .env ] || cp .env.example .env
chmod 600 .env

echo "==> Service (démarrage automatique)"
sudo tee /etc/systemd/system/memebot.service > /dev/null <<EOF
[Unit]
Description=Memecoin Radar
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m memebot
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable memebot > /dev/null

if grep -q '^TELEGRAM_BOT_TOKEN=.' .env; then
  sudo systemctl restart memebot
  echo
  echo "✅ Bot installé et lancé. Logs en direct : journalctl -u memebot -f"
else
  echo
  echo "✅ Bot installé. Il reste à remplir le fichier .env :"
  echo "     nano $DIR/.env"
  echo "   puis à lancer le bot :"
  echo "     sudo systemctl restart memebot"
fi
