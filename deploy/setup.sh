#!/usr/bin/env bash
# One-time setup of the sports bot on an Ubuntu 24.04 server (ARM or x86), run as the default sudo user.
# Free hosting: Oracle Cloud Always Free (Ampere A1). Never upgrade the account to Pay As You Go.
set -euo pipefail

sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3-venv fonts-noto-color-emoji fonts-dejavu-core

[ -d /opt/sports-bot ] || sudo git clone -q https://github.com/rickth21/sports-bot.git /opt/sports-bot
sudo chown -R "$USER:$USER" /opt/sports-bot
[ -d /opt/sports-bot-venv ] || sudo python3 -m venv /opt/sports-bot-venv
sudo chown -R "$USER:$USER" /opt/sports-bot-venv
/opt/sports-bot-venv/bin/pip install -q --upgrade pip pillow playwright
# a normal (non-snap) Chromium for the image cards: the snap one can't read the bot's temp files
sudo /opt/sports-bot-venv/bin/playwright install-deps chromium
/opt/sports-bot-venv/bin/playwright install chromium
CHROME=$(ls "$HOME"/.cache/ms-playwright/chromium-*/chrome-linux*/chrome | tail -1)


if [ ! -f /etc/sports-bot.env ]; then
  sudo tee /etc/sports-bot.env >/dev/null <<ENV
# Paste the webhook URLs after the = signs (keep this file private)
NFL_WEBHOOK_URL=
NBA_WEBHOOK_URL=
ALERT_WEBHOOK_URL=
CHROME_BIN=$CHROME
KEEPALIVE=1
MAX_RUNTIME_MIN=100000000
POLL_SECONDS=45
IDLE_SECONDS=300
ENV
  sudo chmod 600 /etc/sports-bot.env
fi

sudo tee /etc/systemd/system/sports-bot.service >/dev/null <<UNIT
[Unit]
Description=Fantasy sports Discord bot (NFL + NBA)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=/opt/sports-bot
EnvironmentFile=/etc/sports-bot.env
ExecStartPre=-/bin/bash /opt/sports-bot/deploy/pull.sh
ExecStart=/opt/sports-bot-venv/bin/python /opt/sports-bot/bot.py
Restart=always
RestartSec=20

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
echo "Installed. Next: sudo nano /etc/sports-bot.env  then  sudo systemctl enable --now sports-bot"
