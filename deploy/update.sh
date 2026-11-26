#!/usr/bin/env bash
# Pull the latest code, run the pre-upload check, restart the bot only if it passes.
set -euo pipefail
cd /opt/sports-bot
cp state.json /tmp/sports-bot-state.json 2>/dev/null || true
git fetch -q origin main
git checkout -q origin/main -- bot.py config.json tools deploy
cp /tmp/sports-bot-state.json state.json 2>/dev/null || true
if python3 tools/check.py /tmp/sports-bot-check; then
  sudo systemctl restart sports-bot
  echo "Updated and restarted."
else
  echo "Check failed — bot NOT restarted (still running the previous code)."
  exit 1
fi
