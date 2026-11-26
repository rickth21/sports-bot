#!/usr/bin/env bash
# One-time setup of the sports bot on an Ubuntu server (run as the default sudo user).
set -euo pipefail

sudo apt-get update -qq
sudo apt-get install -y -qq git python3 chromium-browser fonts-noto-color-emoji fonts-inter >/dev/null || \
  sudo apt-get install -y -qq git python3 chromium fonts-noto-color-emoji >/dev/null

if [ ! -d /opt/sports-bot ]; then
  sudo git clone -q https://github.com/rickth21/sports-bot.git /opt/sports-bot
fi
sudo chown -R "$USER" /opt/sports-bot

if [ ! -f /etc/sports-bot.env ]; then
  sudo tee /etc/sports-bot.env >/dev/null <<'ENV'
# Paste the webhook URLs after the = signs (keep this file private)
NFL_WEBHOOK_URL=
NBA_WEBHOOK_URL=
ALERT_WEBHOOK_URL=
KEEPALIVE=1
MAX_RUNTIME_MIN=100000000
POLL_SECONDS=45
IDLE_SECONDS=300
ENV
  sudo chmod 600 /etc/sports-bot.env
fi

sudo tee /etc/systemd/system/sports-bot.service >/dev/null <<UNIT
[Unit]
Description=Fantasy sports Discord bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=/opt/sports-bot
EnvironmentFile=/etc/sports-bot.env
ExecStart=/usr/bin/python3 /opt/sports-bot/bot.py
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
echo "Installed. Next: sudo nano /etc/sports-bot.env  then  sudo systemctl enable --now sports-bot"
