#!/usr/bin/env bash
# Saves a Discord BOT token (for pinning the weekly matchups boards) in /etc/sports-bot.env. Never prints it.
# Copy the token in the Discord Developer Portal (Bot -> Reset Token -> Copy), then on the Mac:
#   pbpaste | ssh -i ~/.ssh/r21_oracle ubuntu@145.241.251.194 'bash /opt/sports-bot/deploy/save-token.sh'
# The bot notices the change and restarts onto it at the next quiet moment (no game on).
v="$(cat)"; v="${v//[[:space:]]/}"
if ! [[ "$v" =~ ^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{20,}$ ]]; then
  echo "❌ Your clipboard doesn't hold a Discord bot token. In the Developer Portal: Bot → Reset Token → Copy, then run this again."
  exit 1
fi
me="$(curl -s -H "Authorization: Bot $v" https://discord.com/api/v10/users/@me)"
id="$(printf '%s' "$me" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id",""))' 2>/dev/null)"
name="$(printf '%s' "$me" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("username",""))' 2>/dev/null)"
if [ -z "$id" ]; then
  echo "❌ Discord didn't accept that token (it may have been reset again). Copy a fresh one and run this again."
  exit 1
fi
sudo env V="$v" python3 - <<'PY'
import os
p = "/etc/sports-bot.env"
lines = [l for l in open(p).read().splitlines() if not l.startswith("DISCORD_BOT_TOKEN=")]
lines.append("DISCORD_BOT_TOKEN=" + os.environ["V"])
open(p, "w").write("\n".join(lines) + "\n")
PY
echo "✅ Bot token saved for \"$name\". Donald picks it up at the next quiet moment (no game on)."
echo "   If the bot isn't in your server yet, open this link and add it to Rickth21 - Top 100 Discord:"
echo "   https://discord.com/oauth2/authorize?client_id=$id&scope=bot&permissions=2251799813760000"
