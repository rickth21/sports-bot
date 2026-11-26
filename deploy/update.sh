#!/usr/bin/env bash
# Not normally needed: the bot restarts itself onto new code from GitHub at the next quiet moment.
# Use this to force it now (it pulls the latest files on start).
sudo systemctl restart sports-bot && echo "Restarted onto the latest code from GitHub."
