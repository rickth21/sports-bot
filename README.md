# NFL + NBA Discord Scores Bot

Free. Runs on GitHub Actions (your computer can be off). Data from ESPN's public feed.

## What it posts

**NFL channel**
- **Prime-time games** (anything *not* kicking off Sunday 18:00–23:59 UK, so TNF, SNF, MNF, London, Christmas, Thanksgiving, Saturday games):
  a post for every **touchdown**: who scored, how (catch + QB / run / pick-six / return), extra point or 2-pt, and the score.
- **Every game** when it ends: FT score, top performer of the game, any other elite fantasy lines. Prime-time FT posts also get a quarter-by-quarter box score.

**NBA channel**
- Games are grouped by tip-off time (e.g. the 00:00–01:30 UK batch, the 03:00 UK West Coast batch).
  Once every game in a batch is finished: one post with all the FT scores, each game's standout stat line, plus any other elite fantasy games.

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
