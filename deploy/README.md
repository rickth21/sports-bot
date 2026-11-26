# Running the bot on a free always-on server

Since 26 Nov 2026 the bot runs on the Oracle Always Free server (systemd service `sports-bot`), not GitHub Actions.
Updates: upload to GitHub as before; the bot spots new code/config within ~2 minutes and restarts onto it at
the next quiet moment (no game on, nothing about to post). Never upgrade the Oracle account to Pay As You Go.

**Recommended:** Oracle Cloud "Always Free" (an Ampere/ARM VM with 4 cores and 24 GB RAM, free for good).
Any Ubuntu 22.04/24.04 server works the same way (Google Cloud e2-micro free tier, a Raspberry Pi at home, etc.).

## What you do (needs your own account — 15 minutes)

1. Sign up at oracle.com/cloud/free (a card is asked for to verify you; Always Free resources are not charged).
2. Create an instance: **Ubuntu 24.04**, shape **VM.Standard.A1.Flex** (1 OCPU, 6 GB is plenty).
   Download the SSH key it offers.
3. Note the instance's public IP.

## What happens next (I can do these with you, or you can paste them)

SSH in (`ssh -i key.pem ubuntu@<ip>`), then:

```bash
curl -fsSL https://raw.githubusercontent.com/rickth21/sports-bot/main/deploy/setup.sh | bash
```

Then put the webhook URLs in `/etc/sports-bot.env` (the setup script creates it with blanks) and start it:

```bash
sudo nano /etc/sports-bot.env
```

```bash
sudo systemctl enable --now sports-bot
```

**Then stop the GitHub workflow** so the bot doesn't post twice: on GitHub, Actions → sports-bot → ⋯ →
*Disable workflow*, and cancel the running job.

## Day to day

- Logs: `journalctl -u sports-bot -f`
- Update to the latest code: `sudo /opt/sports-bot/deploy/update.sh` (pulls from GitHub, runs the pre-upload
  check, restarts only if it passes)
- The bot keeps its state in `/opt/sports-bot/state.json` on the server.
