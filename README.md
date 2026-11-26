# NFL + NBA Discord Scores Bot

Free. Runs on GitHub Actions (your computer can be off). Data from ESPN's public feed.

## What it posts

**NFL channel**
- **Prime-time games** (anything *not* kicking off Sunday 18:00–23:59 UK, so TNF, SNF, MNF, London, Christmas, Thanksgiving, Saturday games):
  - **1 hour before kick-off:** preview with records, form, line, ESPN predictor, players to watch, injuries and which league managers have players in the game.
  - **Every touchdown:** who scored, how (QB ➜ receiver / run / pick-six / return), kicker on the extra point, a commentary line, drive, win probability, the score, and the scorer's fantasy line + league owner.
- **Every game** when it ends: FT score, top performer, elite fantasy lines. Prime-time FT posts add a box score and a league round-up.

**NBA channel**
- One full-time post per game as soon as it ends: headline, short recap, insight bullets, and a graphic card
  (score, quarter scores, shooting/rebounds/assists/turnovers/paint bars, player of the game with fpts, league owner).

Note: a run that starts within ~70 minutes of a prime-time kick-off stays on until the game ends (up to ~6 hours),
so one run covers the preview and every touchdown even if GitHub's 10-minute schedule is slow.

## Setup (about 10 minutes)

### 1. Discord webhooks
In your NFL channel: **Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL**.
Do the same in your NBA channel. Keep both links private: anyone with one can post in that channel.

### 2. GitHub repo
1. github.com → **New repository** → name it e.g. `sports-bot` → choose **Public** → Create.
   Public repos get unlimited free Actions minutes, so this doesn't touch your other usage. The webhook links stay hidden (step 3).
2. Click **uploading an existing file** and drag in `bot.py`, `config.json`, `README.md` → Commit.
3. **Add file → Create new file**, type the name `.github/workflows/bot.yml`, paste the contents of that file from this folder → Commit.
   (Finder hides folders starting with a dot, so this is easier than dragging it. Or press Cmd+Shift+. in Finder to show it.)

### 3. Secrets
Repo **Settings → Secrets and variables → Actions → New repository secret**:
- `NFL_WEBHOOK_URL` = the NFL channel webhook link
- `NBA_WEBHOOK_URL` = the NBA channel webhook link

### 4. Switch it on
**Actions** tab → enable workflows if asked → **sports-bot** → **Run workflow** → tick *Send a test message* → Run.
You should see "✅ bot connected" in both channels. After that it runs by itself every 10 minutes.
Its first scheduled run only records what's already happened (so it doesn't spam old results); posting starts from the next game.

## Changing things
Fantasy points use your Sleeper leagues' own scoring, read fresh on every run:
**Rickth21 League** (NFL: PPR + TE premium) and **Rickth21** (NBA). Change the scoring in Sleeper and the bot follows.
To point it at a different league, change `sleeper_league_id` in `config.json`.

Everything else tweakable is in `config.json` (edit it on GitHub with the pencil icon):
- `nfl.elite_threshold`: points needed to be called "elite" (QB / everyone else).
- `nba.elite_threshold`, `always_flag_points`, `always_flag_triple_double`: when an NBA line counts as elite.
- `nba.batch_gap_minutes`: how close tip-offs must be to share a post.

## Good to know
- GitHub's scheduler can start runs a few minutes late. Once a game is live the bot stays on and checks every 45 seconds, so touchdowns usually arrive within about a minute of ESPN showing them.
- Fantasy points are worked out from ESPN's box score using your Sleeper settings, so they can be a fraction off Sleeper's after stat corrections. Kickers and team defences aren't included.
- ESPN's feed is free and unofficial. If ESPN changes it, the bot may need a small fix.

## Preview locally (optional)
```
python3 bot.py --preview-nfl 20261123
python3 bot.py --preview-nba 20261124
```
Previews only print what would be posted. They never post to Discord.
