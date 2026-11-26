#!/usr/bin/env bash
# Runs before every start of the bot: take the latest code + config from GitHub (state.json is kept as it is).
cd /opt/sports-bot || exit 0
git fetch -q origin main 2>/dev/null || exit 0
git checkout -q origin/main -- bot.py config.json tools deploy 2>/dev/null || true
exit 0
