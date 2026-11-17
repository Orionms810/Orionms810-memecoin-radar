#!/usr/bin/env bash
# Met à jour le bot sur le serveur sans toucher à .env, config.toml ni à l'historique.
set -euo pipefail
DIR="$HOME/memecoin-radar"
cd "$DIR"
git pull --ff-only
.venv/bin/pip install --quiet -r requirements.txt
sudo systemctl restart memebot
echo "✅ Bot mis à jour et relancé."
