#!/usr/bin/env python3
"""
NFL + NBA Discord bot.

Data:    ESPN's free public scoreboard/summary feed (no key needed).
Posting: Discord channel webhooks (NFL_WEBHOOK_URL, NBA_WEBHOOK_URL env vars).

NFL
  * Prime-time games (anything NOT kicking off Sunday 18:00-23:59 UK):
      live post for every touchdown (scorer, how, PAT/2pt, score)
  * Every game: FT score + top performer + elite fantasy lines.
NBA
  * Games grouped into tip-off batches; one post per batch when all are final:
      FT scores, each game's standout stat line, other elite fantasy lines.

Usage:
  python3 bot.py                      normal run (loops while NFL games are live)
  python3 bot.py --test               send a test message to each webhook
  python3 bot.py --preview-nfl 20261123   print what it would post for that date
  python3 bot.py --preview-nba 20261124   (previews never post to Discord)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
CONFIG = json.loads((ROOT / "config.json").read_text())

UK = ZoneInfo("Europe/London")
ET = ZoneInfo("America/New_York")
ESPN = "https://site.api.espn.com/apis/site/v2/sports"

NFL_HOOK = os.environ.get("NFL_WEBHOOK_URL", "").strip()
NBA_HOOK = os.environ.get("NBA_WEBHOOK_URL", "").strip()
DRY_RUN = os.environ.get("DRY_RUN") == "1"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "45"))
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME_MIN", "50")) * 60
NBA_EVERY = 300          # check NBA every 5 minutes inside the loop
STALE_HOURS = 30         # never post results for games older than this
TD_PAT_WAIT = 150        # seconds to wait for the extra point to be added to a TD

SILENT = False           # True on the very first run: mark everything as seen, post nothing
CAPTURE = None           # list -> collect embeds instead of posting (simulator)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def log(*a):
    print(datetime.now(UK).strftime("%H:%M:%S"), *a, flush=True)


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(s):
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --------------------------------------------------------------------- HTTP

def fetch(path, tries=3):
    return fetch_url(f"{ESPN}/{path}", tries)


def fetch_url(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"fetch failed: {url}: {last}")


def discord(hook, embed, label=""):
    """Post one embed. Returns True when it is safe to mark the item as posted."""
    if SILENT:
        return True
    if CAPTURE is not None:              # used by the simulator
        CAPTURE.append(embed)
        return True
    if DRY_RUN or not hook:
        print("\n" + "=" * 70)
        print(f"[{label or 'post'}]  {embed.get('title', '')}")
        if embed.get("description"):
            print(embed["description"])
        for f in embed.get("fields", []):
            print(f"-- {f['name']}\n{f['value']}")
        if embed.get("footer"):
            print(f"({embed['footer']['text']})")
        return True
    body = json.dumps({"username": "Scores Bot", "embeds": [embed]}).encode()
    for _ in range(5):
        req = urllib.request.Request(
            hook, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "User-Agent": "DiscordBot (sports-bot, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=20):
                time.sleep(1)
                return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    wait = float(json.loads(e.read().decode()).get("retry_after", 2))
                except Exception:  # noqa: BLE001
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            log(f"Discord rejected post ({e.code}): {e.read().decode()[:300]}")
            return 400 <= e.code < 500   # bad payload: don't retry forever
        except Exception as e:  # noqa: BLE001
            log(f"Discord post failed: {e}")
            return False
    return False


# -------------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st):
    # keep it small: drop games older than 21 days
    cutoff = (now_utc() - timedelta(days=21)).strftime("%Y-%m-%d")
    for league in ("nfl", "nba"):
        games = st.get(league, {})
        for gid in [g for g, v in games.items() if v.get("date", "9999") < cutoff]:
            del games[gid]
    # monthly heartbeat so GitHub never sees the repo as "inactive" (it pauses schedules after 60 days)
    st["heartbeat"] = now_utc().strftime("%Y-%m")
    STATE_FILE.write_text(json.dumps(st, indent=1, sort_keys=True) + "\n")


# ------------------------------------------------------------------ helpers

def num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def fmt_pts(x):
    return f"{x:.1f}".rstrip("0").rstrip(".")


def color(hexstr, default=0x2F3136):
    try:
        return int(hexstr, 16)
    except (TypeError, ValueError):
        return default


def teams_of(event):
    c = event["competitions"][0]
    t = {x["homeAway"]: x for x in c["competitors"]}
    return t["away"], t["home"]


def score_line(away, home, a_score=None, h_score=None, bold_winner=False):
    a = int(num(away.get("score") if a_score is None else a_score))
    h = int(num(home.get("score") if h_score is None else h_score))
    an, hn = away["team"]["abbreviation"], home["team"]["abbreviation"]
    left, right = f"{an} {a}", f"{h} {hn}"
    if bold_winner and a != h:
        left, right = (f"**{left}**", right) if a > h else (left, f"**{right}**")
    return f"{left} – {right}"


def ot_tag(event):
    detail = event["status"]["type"].get("shortDetail", "") or ""
    m = re.search(r"(\d*OT)", detail)
    return f" ({m.group(1)})" if m else ""


def uk_time(dt_utc):
    return dt_utc.astimezone(UK).strftime("%a %-d %b, %H:%M")


# ====================================================================== NFL

def is_prime(kick_utc):
    w = CONFIG["nfl"]["standard_window"]
    k = kick_utc.astimezone(UK)
    in_window = (WEEKDAYS[k.weekday()] == w["weekday"]
                 and w["start_hour_uk"] <= k.hour < w["end_hour_uk"])
    return not in_window


SLEEPER = "https://api.sleeper.app/v1"
_SCORING = {}


def league_scoring(sport):
    """Scoring settings straight from the Sleeper league (falls back to config.json)."""
    if sport in _SCORING:
        return _SCORING[sport]
    sc = dict(CONFIG[sport]["fallback_scoring"])
    lid = str(CONFIG[sport].get("sleeper_league_id") or "").strip()
    if lid:
        try:
            got = fetch_url(f"{SLEEPER}/league/{lid}").get("scoring_settings") or {}
            if got:
                sc = got
        except Exception as e:  # noqa: BLE001
            log(f"Sleeper {sport} scoring unavailable, using config.json: {e}")
    _SCORING[sport] = sc
    return sc


def nfl_positions(summary):
    """ESPN athlete id -> position (QB/RB/WR/TE/PK...), from both teams' rosters."""
    pos = {}
    for team in summary.get("boxscore", {}).get("players", []):
        tid = team["team"].get("id")
        try:
            roster = fetch(f"football/nfl/teams/{tid}/roster")
        except Exception as e:  # noqa: BLE001
            log(f"roster {tid} unavailable: {e}")
            continue
        for group in roster.get("athletes", []):
            for a in group.get("items", []):
                pos[str(a["id"])] = (a.get("position") or {}).get("abbreviation", "")
    return pos


def two_point_credits(summary):
    """Successful 2-pt conversions by player name -> {sleeper_key: count}."""
    credits = {}

    def add(name, key):
        credits.setdefault(name.strip(), {}).setdefault(key, 0)
        credits[name.strip()][key] += 1

    for p in summary.get("scoringPlays", []):
        m = re.search(r"\(([^()]*two-point[^()]*)\)", p.get("text") or "", re.I)
        if not m or re.search(r"fail|no good", m.group(1), re.I):
            continue
        pat = m.group(1)
        mm = re.match(r"^(.+?)\s+pass to\s+(.+?)\s+for two-point", pat, re.I)
        if mm:
            add(mm.group(1), "pass_2pt")
            add(mm.group(2), "rec_2pt")
            continue
        mm = re.match(r"^(.+?)\s+(?:run|rush)\s+for two-point", pat, re.I)
        if mm:
            add(mm.group(1), "rush_2pt")
    return credits


def nfl_fantasy(summary):
    """Offensive players with Sleeper-league fantasy points + readable stat line, best first."""
    sc = league_scoring("nfl")
    v = lambda k: float(sc.get(k) or 0)  # noqa: E731
    positions = nfl_positions(summary)
    two_pt = two_point_credits(summary)

    players = {}
    for team in summary.get("boxscore", {}).get("players", []):
        abbr = team["team"]["abbreviation"]
        for cat in team.get("statistics", []):
            keys = cat.get("keys", [])
            for a in cat.get("athletes", []):
                ath = a["athlete"]
                p = players.setdefault(str(ath["id"]), {"name": ath["displayName"], "team": abbr, "s": {}})
                for k, val in zip(keys, a.get("stats", [])):
                    p["s"][f"{cat['name']}.{k}"] = val

    out = []
    for pid, p in players.items():
        s = p["s"]
        g = lambda k: num(s.get(k, 0))  # noqa: E731
        comp_att = str(s.get("passing.completions/passingAttempts", "0/0")).split("/")
        att = num(comp_att[1]) if len(comp_att) > 1 else 0
        pos = positions.get(pid) or ("QB" if att >= 10 else "")
        if pos in ("PK", "K", "P", "LS"):
            continue
        py, ptd, pint = g("passing.passingYards"), g("passing.passingTouchdowns"), g("passing.interceptions")
        ra, ry, rtd = g("rushing.rushingAttempts"), g("rushing.rushingYards"), g("rushing.rushingTouchdowns")
        rec, recy, rectd = g("receiving.receptions"), g("receiving.receivingYards"), g("receiving.receivingTouchdowns")
        ret_td = g("kickReturns.kickReturnTouchdowns") + g("puntReturns.puntReturnTouchdowns")
        fl = g("fumbles.fumblesLost")

        pts = (py * v("pass_yd") + ptd * v("pass_td") + pint * v("pass_int")
               + ry * v("rush_yd") + rtd * v("rush_td")
               + rec * v("rec") + recy * v("rec_yd") + rectd * v("rec_td")
               + ret_td * v("st_td") + fl * v("fum_lost"))
        pts += rec * v({"TE": "bonus_rec_te", "RB": "bonus_rec_rb", "WR": "bonus_rec_wr"}.get(pos, "-"))
        for key, stat, line in (("bonus_pass_yd_300", py, 300), ("bonus_pass_yd_400", py, 400),
                                ("bonus_rush_yd_100", ry, 100), ("bonus_rush_yd_200", ry, 200),
                                ("bonus_rec_yd_100", recy, 100), ("bonus_rec_yd_200", recy, 200)):
            if stat >= line:
                pts += v(key)
        for key, n in two_pt.get(p["name"], {}).items():
            pts += n * v(key)
        if pts <= 0:
            continue

        parts = []
        if att:
            t = f"{'/'.join(comp_att)}, {int(py)} pass yds"
            if ptd: t += f", {int(ptd)} TD"
            if pint: t += f", {int(pint)} INT"
            parts.append(t)
        if ra and (ry >= 20 or rtd or not att):
            t = f"{int(ra)} car, {int(ry)} rush yds"
            if rtd: t += f", {int(rtd)} TD"
            parts.append(t)
        if rec:
            t = f"{int(rec)} rec, {int(recy)} yds"
            if rectd: t += f", {int(rectd)} TD"
            parts.append(t)
        if ret_td:
            parts.append(f"{int(ret_td)} return TD")
        thr = CONFIG["nfl"]["elite_threshold"]["QB" if pos == "QB" else "other"]
        out.append({"id": pid, "name": p["name"], "team": p["team"], "pos": pos, "pts": pts,
                    "line": " · ".join(parts), "elite": pts >= thr})
    out.sort(key=lambda x: -x["pts"])
    return out


def nfl_player_line(p):
    tag = f"{p['team']} · {p['pos']}" if p.get("pos") else p["team"]
    return f"**{p['name']}** ({tag}) — {p['line']} · **{fmt_pts(p['pts'])} fpts**"


NFL_LOGO = "https://a.espncdn.com/i/teamlogos/leagues/500/nfl.png"


def parse_pat(pat):
    """'Evan McPherson Kick' / 'Jake Elliott PAT failed' / 'Jalen Hurts Run for Two-Point Conversion'."""
    if not pat:
        return None
    p = pat.lower()
    failed = any(w in p for w in ("fail", "miss", "block", "no good"))
    mark = "❌ No good" if failed else "✅ Good"
    if "two-point" in p or "two point" in p or "2pt" in p or "2-pt" in p:
        who = ""
        m = re.match(r"^(.+?)\s+pass to\s+(.+?)\s+for two-point", pat, re.I)
        if m:
            who = f"{m.group(1)} ➜ {m.group(2)}"
        else:
            m = re.match(r"^(.+?)\s+(?:run|rush)\s+for two-point", pat, re.I)
            if m:
                who = f"{m.group(1)} run"
        return {"label": "2️⃣ 2-pt conversion", "mark": mark, "who": who}
    m = re.match(r"^(.+?)\s+(?:kick|pat|extra point)\b", pat, re.I)
    return {"label": "🦶 Extra point", "mark": mark, "who": m.group(1).strip() if m else ""}


def td_parse(play):
    """'Tee Higgins 31 Yd pass from Joe Burrow (Evan McPherson Kick)' -> structured touchdown."""
    raw = (play.get("text") or "").strip()
    text, pat = raw, None
    m = re.match(r"^(.*?)\s*\(([^()]*)\)\s*$", raw)
    if m:
        text, pat = m.group(1).strip(), m.group(2).strip()
    td = {"kind": "other", "scorer": text, "qb": None, "yds": None, "raw": raw, "pat": parse_pat(pat)}
    m = re.match(r"^(?P<who>.+?)\s+(?P<yds>\d+)\s+(?:Yd|Yds|Yard|Yards)\s+(?P<rest>.+)$", text, re.I)
    if not m:
        return td
    td["scorer"], td["yds"] = m.group("who").strip(), int(m.group("yds"))
    rest = m.group("rest").strip()
    r = rest.lower()
    if r.startswith("pass from"):
        td["kind"], td["qb"] = "pass", rest[len("pass from"):].strip()
    elif "rush" in r or r.startswith("run"):
        td["kind"] = "rush"
    elif "blocked" in r:
        td["kind"] = "blocked"
    elif "interception" in r:
        td["kind"] = "int"
    elif "fumble" in r:
        td["kind"] = "fumble"
    elif "punt" in r:
        td["kind"] = "punt"
    elif "kick" in r:
        td["kind"] = "kick"
    return td


# kind -> (headline wording, label for the scorer field, emoji)
TD_KINDS = {
    "rush":    ("touchdown run",                 "Ball carrier", "🏃"),
    "int":     ("pick-six",                      "Defender",     "🛡️"),
    "fumble":  ("fumble return touchdown",       "Defender",     "🛡️"),
    "punt":    ("punt return touchdown",         "Returner",     "⚡"),
    "kick":    ("kick-off return touchdown",     "Returner",     "⚡"),
    "blocked": ("blocked kick return touchdown", "Returner",     "⚡"),
}


# ------------------------------------------------------- Sleeper league rosters

_LEAGUE = {}
SLEEPER_TEAM = {"WSH": "WAS"}     # ESPN abbreviation -> Sleeper abbreviation where they differ


def league_index(sport="nfl"):
    """ESPN athlete id -> who rosters him in the Sleeper league (and whether he's in their lineup)."""
    if sport in _LEAGUE:
        return _LEAGUE[sport]
    idx = {}
    lid = str(CONFIG[sport].get("sleeper_league_id") or "").strip()
    if lid:
        try:
            users = {u["user_id"]: u for u in fetch_url(f"{SLEEPER}/league/{lid}/users")}
            rosters = fetch_url(f"{SLEEPER}/league/{lid}/rosters")
            players = fetch_url(f"{SLEEPER}/players/{sport}", tries=2)
            for r in rosters:
                u = users.get(r.get("owner_id")) or {}
                starters = set(r.get("starters") or [])
                for pid in r.get("players") or []:
                    p = players.get(pid) or {}
                    if not p.get("full_name"):
                        continue
                    entry = {
                        "manager": u.get("display_name", "?"),
                        "team_name": (u.get("metadata") or {}).get("team_name") or "",
                        "starting": pid in starters,
                        "name": p.get("full_name") or "",
                        "pos": p.get("position") or "",
                        "team": p.get("team") or "",
                    }
                    idx[name_key(p["full_name"])] = entry      # fallback: Sleeper lacks some ESPN ids
                    if p.get("espn_id"):
                        idx[str(p["espn_id"])] = entry
        except Exception as e:  # noqa: BLE001
            log(f"Sleeper league rosters unavailable: {e}")
    _LEAGUE[sport] = idx
    return idx


def name_key(name):
    n = re.sub(r"[^a-z ]", "", (name or "").lower())
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", n)
    return "n:" + " ".join(n.split())


def league_owner(espn_id, name=""):
    idx = league_index("nfl")
    return idx.get(str(espn_id)) or idx.get(name_key(name))


def owner_text(espn_id, name=""):
    o = league_owner(espn_id, name)
    if not o:
        return "🆓 unrostered in the league"
    star = " ⭐ starting" if o["starting"] else " (bench)"
    return f"**{o['manager']}**{star}"


# ----------------------------------------------------------------- touchdowns

def play_detail(summary, play_id):
    drives = summary.get("drives") or {}
    pool = list(drives.get("previous") or [])
    if drives.get("current"):
        pool.append(drives["current"])
    for d in pool:
        for p in d.get("plays") or []:
            if str(p.get("id")) == str(play_id):
                return d, p
    return None, None


def win_prob(summary, play_id, home_scored):
    wp = summary.get("winprobability") or []
    for i, w in enumerate(wp):
        if str(w.get("playId")) == str(play_id):
            now = w.get("homeWinPercentage")
            before = wp[i - 1].get("homeWinPercentage") if i else None
            if now is None:
                return None
            if not home_scored:
                now = 1 - now
                before = None if before is None else 1 - before
            return now, before
    return None


DIR_DEEP = {"left": "down the left sideline", "middle": "down the seam", "right": "down the right sideline"}
DIR_SHORT = {"left": "to the left", "middle": "over the middle", "right": "to the right"}
GAP = {"left end": "around the left edge", "right end": "around the right edge",
       "left tackle": "off left tackle", "right tackle": "off right tackle",
       "left guard": "behind the left guard", "right guard": "behind the right guard",
       "up the middle": "straight up the middle"}


def commentary(td, detail_text, down_text, nick, lead_txt, seed):
    """Build a commentator-style line from ESPN's play-by-play description."""
    t = (detail_text or "").lower()
    gun = "from the shotgun " if "shotgun" in t else ""
    dd = f"on {down_text.split(' at ')[0]}, " if down_text else ""
    yds = td["yds"] or 0
    pick = lambda opts: opts[seed % len(opts)]  # noqa: E731
    line = None

    if td["kind"] == "pass":
        qb, rec = td["qb"], td["scorer"]
        m = re.search(r"pass (short|deep) (left|middle|right)", t)
        depth, side = (m.group(1), m.group(2)) if m else ("short", "middle")
        if yds <= 5:
            line = pick([f"{qb} {gun}{dd}fires it {DIR_SHORT[side]} and {rec} makes the grab in the end zone!",
                         f"Goal-to-go, {qb} looks {side}… {rec} is open and that's six!"])
        elif depth == "deep" or yds >= 30:
            line = pick([f"{qb} {gun}{dd}steps up and lets it fly {DIR_DEEP[side]} — {rec} gets behind the coverage and hauls it in. {yds} yards, touchdown {nick}!",
                         f"{qb} takes the shot! Deep ball {DIR_DEEP[side]}, {rec} tracks it all the way in — a {yds}-yard strike!"])
        else:
            line = pick([f"{qb} {gun}{dd}finds {rec} {DIR_SHORT[side]}, and he does the rest — {yds} yards to the house!",
                         f"Quick throw {DIR_SHORT[side]} from {qb}, {rec} breaks a tackle and walks in from {yds}!"])
    elif td["kind"] == "rush":
        rb = td["scorer"]
        gap = next((v for k, v in GAP.items() if k in t), "")
        if yds <= 3:
            line = pick([f"{rb} {gap or 'up the gut'} and pushes the pile over the goal line!",
                         f"{dd.capitalize()}{rb} keeps the legs churning {gap} and he's in!"])
        elif yds >= 40:
            line = f"{rb} bursts {gap or 'through the hole'}, breaks into the open field and nobody's catching him — {yds} yards to the house!"
        else:
            line = f"{rb} takes the handoff {gap or 'up field'}, finds a crease and scores from {yds} out!"
    elif td["kind"] == "int":
        line = f"PICKED OFF! {td['scorer']} jumps the route and takes it {yds} yards the other way for six!"
    elif td["kind"] in ("fumble",):
        line = f"Ball's out! {td['scorer']} scoops it up and rumbles {yds} yards to the end zone!"
    elif td["kind"] in ("punt", "kick", "blocked"):
        line = f"{td['scorer']} takes it back {yds} yards — special teams touchdown!"
    if not line:
        return ""
    return f"🎙️ *{line[0].upper() + line[1:]}* {lead_txt}".strip()


def lead_context(team_is_away, a_s, h_s, a_before, h_before):
    us, them = (a_s, h_s) if team_is_away else (h_s, a_s)
    us0, them0 = (a_before, h_before) if team_is_away else (h_before, a_before)
    if us == them:
        return "All square!", "TIES IT"
    if us > them and us0 <= them0:
        return "That's the go-ahead score!", "GO-AHEAD SCORE"
    if us > them:
        return f"Lead stretches to {us - them}.", f"LEAD NOW {us - them}"
    return f"Gap cut to {them - us}.", f"WITHIN {them - us}"


def player_tonight(summary, name):
    """Stat line + fpts so far for a player in this game, and his ESPN id."""
    try:
        for p in nfl_fantasy(summary):
            if p["name"] == name:
                return p
    except Exception as e:  # noqa: BLE001
        log(f"fantasy line unavailable: {e}")
    return None


def post_touchdown(event, play, summary=None):
    summary = summary or {}
    away, home = teams_of(event)
    abbr = play.get("team", {}).get("abbreviation", "")
    team = away if away["team"]["abbreviation"] == abbr else home
    t = team["team"]
    nick = t.get("name") or t.get("shortDisplayName") or abbr
    td = td_parse(play)
    yds = td["yds"]
    yd_txt = (f"{yds} yd" + ("" if yds == 1 else "s")) if yds is not None else "—"

    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    pat_pts = 0
    if td["pat"] and "Good" in td["pat"]["mark"]:
        pat_pts = 2 if "2-pt" in td["pat"]["label"] else 1
    scored = 6 + pat_pts
    a0, h0 = (a_s - scored, h_s) if team is away else (a_s, h_s - scored)
    lead_txt, tag = lead_context(team is away, a_s, h_s, a0, h0)

    drive, detail = play_detail(summary, play.get("id"))
    down_text = ((detail or {}).get("start") or {}).get("downDistanceText", "")
    seed = int(re.sub(r"\D", "", str(play.get("id", "0")))[-6:] or 0)
    call = commentary(td, (detail or {}).get("text", ""), down_text, nick, lead_txt, seed)

    q = play.get("period", {}).get("number", 0)
    qtxt = f"Q{q}" if q <= 4 else ("OT" if q == 5 else f"{q - 4}OT")
    clock = play.get("clock", {}).get("displayValue", "")

    if td["kind"] == "pass":
        head = f"### {td['qb']} ➜ {td['scorer']}"
        sub = f"{yds}-yard touchdown pass"
        fields = [{"name": "🎯 Quarterback", "value": td["qb"], "inline": True},
                  {"name": "🙌 Receiver", "value": td["scorer"], "inline": True},
                  {"name": "📏 Distance", "value": yd_txt, "inline": True}]
    elif td["kind"] in TD_KINDS:
        what, label, emoji = TD_KINDS[td["kind"]]
        head = f"### {td['scorer']}"
        sub = f"{yds}-yard {what}"
        fields = [{"name": f"{emoji} {label}", "value": td["scorer"], "inline": True},
                  {"name": "📏 Distance", "value": yd_txt, "inline": True},
                  {"name": "🧭 Situation", "value": down_text.split(" at ")[0] if down_text else "—", "inline": True}]
    else:
        head, sub, fields = f"### {td['raw']}", "", []

    pat = td["pat"]
    if pat:
        fields.append({"name": pat["label"], "value": pat["mark"] + (f"\n{pat['who']}" if pat["who"] else ""),
                       "inline": True})
    if drive and drive.get("description") and (drive.get("team") or {}).get("abbreviation", abbr) == abbr:
        fields.append({"name": "🚚 Drive", "value": drive["description"].replace(", ", " · "), "inline": True})
    wp = win_prob(summary, play.get("id"), team is home)
    if wp:
        now, before = wp
        delta = "" if before is None else f"  {'▲' if now >= before else '▼'}{abs(round((now - before) * 100))}"
        fields.append({"name": "📈 Win probability", "value": f"{abbr} {round(now * 100)}%{delta}", "inline": True})

    a_ab, h_ab = away["team"]["abbreviation"], home["team"]["abbreviation"]
    board = (f"{a_ab:<4}{a_s:>3}{'  ◀' if team is away else ''}\n"
             f"{h_ab:<4}{h_s:>3}{'  ◀' if team is home else ''}")
    fields.append({"name": "📊 Score", "value": f"```\n{board}\n```", "inline": False})

    fan = []
    for who in ([td["qb"]] if td.get("qb") else []) + [td["scorer"]]:
        p = player_tonight(summary, who)
        if p:
            fan.append(f"**{p['name']}** — {p['line']} · **{fmt_pts(p['pts'])} fpts**\n↳ {owner_text(p['id'], p['name'])}")
    if fan:
        fields.append({"name": f"🧢 {CONFIG['nfl'].get('league_name', 'League')} — tonight so far",
                       "value": "\n".join(fan)[:1024], "inline": False})

    desc = f"**{tag}** · {qtxt} {clock}\n{head}\n{sub}" + (f"\n\n{call}" if call else "")
    embed = {
        "author": {"name": t.get("displayName", abbr), "icon_url": t.get("logo", "")},
        "title": f"🏈 TOUCHDOWN {nick.upper()}!",
        "description": desc,
        "color": color(t.get("color")),
        "thumbnail": {"url": t.get("logo", "")},
        "fields": fields,
        "footer": {"text": f"{qtxt} · {clock}  |  {away['team'].get('name') or a_ab} @ {home['team'].get('name') or h_ab}",
                   "icon_url": NFL_LOGO},
    }
    return discord(NFL_HOOK, embed, "NFL TD")


# -------------------------------------------------------------- pre-game post

def form_line(summary, abbr):
    for t in summary.get("lastFiveGames") or []:
        if t.get("team", {}).get("abbreviation") != abbr:
            continue
        evs = sorted(t.get("events") or [], key=lambda e: e.get("gameDate", ""))
        marks = {"W": "🟩", "L": "🟥", "T": "⬜"}
        return "".join(marks.get(e.get("gameResult"), "⬜") for e in evs)
    return ""


def season_leaders(summary, abbr):
    out = []
    for t in summary.get("leaders") or []:
        if t.get("team", {}).get("abbreviation") != abbr:
            continue
        for cat in t.get("leaders") or []:
            if cat.get("name") not in ("passingYards", "rushingYards", "receivingYards") or not cat.get("leaders"):
                continue
            x = cat["leaders"][0]
            dv = x.get("displayValue", "")
            if cat["name"] == "passingYards":
                dv = ", ".join(dv.split(", ")[1:]) or dv
            dv = dv.replace(" YDS", " yds").replace(" CAR", " car").replace(" REC", " rec")
            out.append(f"{x['athlete']['displayName']} · {dv}")
    return out


def injury_lines(summary, abbr, limit=4):
    rank = {"Out": 0, "Injured Reserve": 1, "Doubtful": 2, "Questionable": 3}
    short = {"Out": "OUT", "Injured Reserve": "IR", "Doubtful": "D", "Questionable": "Q"}
    skill = {"QB", "RB", "WR", "TE", "K"}
    rows = []
    for t in summary.get("injuries") or []:
        if t.get("team", {}).get("abbreviation") != abbr:
            continue
        for i in t.get("injuries") or []:
            st = i.get("status")
            if st not in rank:
                continue
            pos = (i.get("athlete", {}).get("position") or {}).get("abbreviation", "")
            rows.append((0 if pos in skill else 1, rank[st], f"{i['athlete']['displayName']} {pos} ({short[st]})"))
    rows.sort()
    return [r[2] for r in rows[:limit]]


def post_nfl_preview(event, summary):
    away, home = teams_of(event)
    a, h = away["team"], home["team"]
    kick = parse_time(event["date"]).astimezone(UK)
    comp = event["competitions"][0]
    tv = ", ".join(n for b in comp.get("broadcasts") or [] for n in b.get("names", []))
    gi = summary.get("gameInfo") or {}
    venue = (gi.get("venue") or {}).get("fullName") or (comp.get("venue") or {}).get("fullName", "")
    city = ((gi.get("venue") or {}).get("address") or {}).get("city", "")

    lines = [f"**{kick.strftime('%a %-d %b')} · kick-off {kick.strftime('%H:%M')} UK** — 1 hour to go",
             f"🏟️ {venue}{', ' + city if city else ''}",
             f"📺 {tv + ' (US) · ' if tv else ''}DAZN (UK)"]
    head = (summary.get("article") or {}).get("headline")
    if head:
        lines.append(f"\n📰 *{head}*")

    fields = []
    for t in (away, home):
        ab = t["team"]["abbreviation"]
        rec = record_of(t)
        val = f"**{rec}**" if rec else ""
        form = form_line(summary, ab)
        if form:
            val += f"\nLast 5: {form}"
        fields.append({"name": f"{t['team'].get('displayName', ab)}", "value": val or "—", "inline": True})
    fields.append({"name": "​", "value": "​", "inline": True})

    pc = (summary.get("pickcenter") or [{}])[0]
    if pc.get("details"):
        fields.append({"name": "🎲 Line", "value": f"{pc['details']}\nO/U {pc.get('overUnder', '—')}", "inline": True})
    pr = summary.get("predictor") or {}
    try:
        hp = float(pr["homeTeam"]["gameProjection"])
        fav, pct = (h["abbreviation"], hp) if hp >= 50 else (a["abbreviation"], 100 - hp)
        fields.append({"name": "🔮 ESPN predictor", "value": f"{fav} {round(pct)}% to win", "inline": True})
    except (KeyError, TypeError, ValueError):
        pass
    w = gi.get("weather") or {}
    if w.get("temperature") is not None:
        f_ = w["temperature"]
        fields.append({"name": "🌡️ Conditions", "value": f"{f_}°F / {round((f_ - 32) * 5 / 9)}°C", "inline": True})

    for t in (away, home):
        ab = t["team"]["abbreviation"]
        ldr = season_leaders(summary, ab)
        if ldr:
            fields.append({"name": f"⭐ {t['team'].get('name', ab)} to watch", "value": "\n".join(ldr)[:1024],
                           "inline": True})
    inj = []
    for t in (away, home):
        ab = t["team"]["abbreviation"]
        rows = injury_lines(summary, ab)
        if rows:
            inj.append(f"**{ab}:** " + " · ".join(rows))
    if inj:
        fields.append({"name": "🚑 Injury report", "value": "\n".join(inj)[:1024], "inline": False})

    # league stakes: who in the Sleeper league has players in this game
    hurt = {name_key(i["athlete"]["displayName"]) for t in summary.get("injuries") or []
            for i in t.get("injuries") or [] if i.get("status") in ("Out", "Injured Reserve", "Doubtful")}
    teams = {SLEEPER_TEAM.get(x, x) for x in (a["abbreviation"], h["abbreviation"])}
    by_mgr = {}
    seen = set()
    for o in league_index("nfl").values():
        if id(o) in seen:
            continue
        seen.add(id(o))
        if o["team"] in teams and o["pos"] in ("QB", "RB", "WR", "TE", "K"):
            by_mgr.setdefault(o["manager"], []).append(o)
    if by_mgr:
        rows = []
        for mgr, ps in sorted(by_mgr.items(), key=lambda kv: (-sum(p["starting"] for p in kv[1]), kv[0].lower())):
            ps.sort(key=lambda p: (not p["starting"], p["name"]))
            rows.append(f"**{mgr}** — " + ", ".join(
                p["name"] + (" ⭐" if p["starting"] else "") + (" 🚑" if name_key(p["name"]) in hurt else "")
                for p in ps))
        fields.append({"name": f"🧢 {CONFIG['nfl'].get('league_name', 'League')} stakes  (⭐ in lineup · 🚑 out)",
                       "value": "\n".join(rows)[:1024], "inline": False})

    embed = {
        "author": {"name": "PRIME TIME · KICK-OFF IN 1 HOUR", "icon_url": NFL_LOGO},
        "title": f"{a.get('displayName')} @ {h.get('displayName')}",
        "description": "\n".join(lines),
        "color": color(h.get("color")),
        "thumbnail": {"url": h.get("logo", "")},
        "fields": fields,
        "footer": {"text": "Touchdowns will be posted live in this channel", "icon_url": a.get("logo", "")},
    }
    return discord(NFL_HOOK, embed, "NFL preview")


def record_of(comp):
    for r in comp.get("records") or []:
        if r.get("type") in ("total", None) or r.get("name") == "overall":
            return r.get("summary", "")
    return ""


def post_nfl_final(event, summary, prime):
    away, home = teams_of(event)
    a, h = int(num(away.get("score"))), int(num(home.get("score")))
    winner = away if a > h else home
    an = away["team"].get("name") or away["team"]["abbreviation"]
    hn = home["team"].get("name") or home["team"]["abbreviation"]
    fields = []

    if a == h:
        desc = "🤝 **Tie game**"
    else:
        rec = record_of(winner)
        desc = f"🏆 **{winner['team'].get('displayName')}** win" + (f"  ·  now {rec}" if rec else "")

    if prime:
        rows = []
        for t in (away, home):
            ls = [str(int(num(x.get("value", x.get("displayValue", 0))))) for x in t.get("linescores", [])]
            rows.append((t["team"]["abbreviation"], ls, str(int(num(t.get("score"))))))
        n = max(len(r[1]) for r in rows) if rows else 0
        hdr = ["Q1", "Q2", "Q3", "Q4"] + ["OT"] * max(0, n - 4)
        lines = ["     " + " ".join(f"{x:>3}" for x in hdr[:n]) + "   T"]
        for abbr, ls, tot in rows:
            lines.append(f"{abbr:<4} " + " ".join(f"{x:>3}" for x in ls) + f" {tot:>3}")
        fields.append({"name": "📊 Box score", "value": "```\n" + "\n".join(lines) + "\n```", "inline": False})

    ff = nfl_fantasy(summary)
    if ff:
        fields.append({"name": "⭐ Top performer", "value": nfl_player_line(ff[0]), "inline": False})
        elite = [p for p in ff[1:] if p["elite"]]
        if elite:
            fields.append({"name": "🔥 Elite fantasy",
                           "value": "\n".join(nfl_player_line(p) for p in elite)[:1024], "inline": False})
        if prime:
            idx = league_index("nfl")
            by_mgr = {}
            for p in ff:
                o = league_owner(p.get("id"), p["name"])
                if o and o["starting"]:
                    by_mgr.setdefault(o["manager"], []).append(p)
            if by_mgr:
                rows = []
                for mgr, ps in sorted(by_mgr.items(), key=lambda kv: -sum(p["pts"] for p in kv[1])):
                    total = sum(p["pts"] for p in ps)
                    rows.append(f"**{mgr}** {fmt_pts(total)} — " +
                                ", ".join(f"{p['name'].split(' ')[-1]} {fmt_pts(p['pts'])}" for p in ps))
                fields.append({"name": f"🧢 {CONFIG['nfl'].get('league_name', 'League')} — starters in this game",
                               "value": "\n".join(rows)[:1024], "inline": False})

    embed = {
        "author": {"name": "FULL TIME" + ot_tag(event).upper(), "icon_url": NFL_LOGO},
        "title": f"{an} {a} – {h} {hn}",
        "description": desc,
        "color": color(winner["team"].get("color")),
        "thumbnail": {"url": winner["team"].get("logo", "")},
        "fields": fields,
        "footer": {"text": f"fpts = {CONFIG['nfl'].get('league_name', 'league')} scoring", "icon_url": NFL_LOGO},
    }
    return discord(NFL_HOOK, embed, "NFL FT")


def nfl_game(event, st, force=False):
    """Handle one NFL game. Returns True if the game needs fast polling right now."""
    gid = event["id"]
    kick = parse_time(event["date"])
    g = st["nfl"].setdefault(gid, {"date": event["date"][:10], "tds": [], "pending": {}})
    stype = event["status"]["type"]
    state = stype["state"]
    prime = is_prime(kick)
    age = (now_utc() - kick).total_seconds() / 3600

    if state == "pre":
        # pre-game post for prime-time games, about an hour before kick-off
        if prime and not g.get("preview") and -1.1 <= age < -0.15:
            summary = fetch(f"football/nfl/summary?event={gid}")
            if post_nfl_preview(event, summary):
                g["preview"] = True
        return -0.4 < age < 0.5          # kicking off soon (or running late)

    if state == "in" or force:
        if prime:
            summary = fetch(f"football/nfl/summary?event={gid}")
            plays = [p for p in summary.get("scoringPlays", [])
                     if p.get("type", {}).get("abbreviation") == "TD"
                     or "touchdown" in (p.get("type", {}).get("text", "").lower())]
            for i, p in enumerate(plays):
                if p["id"] in g["tds"]:
                    continue
                # wait briefly for the PAT to be attached so the score is right
                has_pat = "(" in (p.get("text") or "")
                first = g["pending"].setdefault(p["id"], time.time())
                is_latest = i == len(plays) - 1
                if not force and not has_pat and is_latest and time.time() - first < TD_PAT_WAIT \
                        and state == "in":
                    continue
                if post_touchdown(event, p, summary):
                    g["tds"].append(p["id"])
                    g["pending"].pop(p["id"], None)
        if state == "in":
            return True

    if state == "post" and not g.get("final"):
        if not stype.get("completed"):
            g["final"] = True            # postponed / cancelled
            return False
        if age > STALE_HOURS and not force:
            g["final"] = True
            return False
        summary = fetch(f"football/nfl/summary?event={gid}")
        if post_nfl_final(event, summary, prime):
            g["final"] = True
    return False


def run_nfl(st, date=None, force=False):
    path = "football/nfl/scoreboard" + (f"?dates={date}" if date else "")
    events = fetch(path).get("events", [])
    active = False
    for e in sorted(events, key=lambda x: x["date"]):
        try:
            active |= bool(nfl_game(e, st, force=force))
        except Exception as ex:  # noqa: BLE001
            log(f"NFL game {e.get('id')} error: {ex}")
    return active


# ====================================================================== NBA

def nba_players(summary):
    """Players with Sleeper-league fantasy points + readable stat line, best first."""
    sc = league_scoring("nba")
    v = lambda k: float(sc.get(k) or 0)  # noqa: E731
    cfg = CONFIG["nba"]
    out = []
    for team in summary.get("boxscore", {}).get("players", []):
        abbr = team["team"]["abbreviation"]
        for block in team.get("statistics", []):
            keys = block.get("keys", [])
            for a in block.get("athletes", []):
                if a.get("didNotPlay") or not a.get("stats"):
                    continue
                s = dict(zip(keys, a["stats"]))
                pts, reb, ast = num(s.get("points")), num(s.get("rebounds")), num(s.get("assists"))
                stl, blk, tov = num(s.get("steals")), num(s.get("blocks")), num(s.get("turnovers"))
                tpm = num(str(s.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0-0")).split("-")[0])
                fg = s.get("fieldGoalsMade-fieldGoalsAttempted", "")
                doubles = sum(1 for x in (pts, reb, ast, stl, blk) if x >= 10)

                fp = (pts * v("pts") + reb * v("reb") + ast * v("ast") + stl * v("stl")
                      + blk * v("blk") + tov * v("to") + tpm * v("tpm"))
                if doubles >= 2: fp += v("dd")
                if doubles >= 3: fp += v("td")
                if doubles >= 4: fp += v("qd")
                if pts >= 40: fp += v("bonus_pt_40p")
                if pts >= 50: fp += v("bonus_pt_50p")
                if reb >= 20: fp += v("bonus_reb_20p")
                if ast >= 15: fp += v("bonus_ast_15p")

                parts = [f"{int(pts)} pts", f"{int(reb)} reb", f"{int(ast)} ast"]
                if stl >= 2: parts.append(f"{int(stl)} stl")
                if blk >= 2: parts.append(f"{int(blk)} blk")
                if tpm >= 4: parts.append(f"{int(tpm)} 3PM")
                if fg: parts.append(f"{fg} FG")
                tag = " · triple-double" if doubles >= 3 else ""
                elite = (fp >= cfg["elite_threshold"] or pts >= cfg["always_flag_points"]
                         or (cfg["always_flag_triple_double"] and doubles >= 3))
                out.append({"name": a["athlete"]["displayName"], "team": abbr, "fp": fp,
                            "line": " · ".join(parts) + tag + f" · **{fmt_pts(fp)} fpts**",
                            "elite": elite})
    out.sort(key=lambda x: -x["fp"])
    return out


def nba_batches(events):
    gap = CONFIG["nba"]["batch_gap_minutes"] * 60
    evs = sorted(events, key=lambda e: e["date"])
    batches, cur, last = [], [], None
    for e in evs:
        t = parse_time(e["date"])
        if cur and (t - last).total_seconds() > gap:
            batches.append(cur)
            cur = []
        cur.append(e)
        last = t
    if cur:
        batches.append(cur)
    return batches


def run_nba(st, dates=None, force=False):
    if not NBA_HOOK and not DRY_RUN and not SILENT and not force:
        return
    if dates is None:
        today = datetime.now(ET).date()
        dates = [(today - timedelta(days=1)).strftime("%Y%m%d"), today.strftime("%Y%m%d")]
    for d in dates:
        events = fetch(f"basketball/nba/scoreboard?dates={d}").get("events", [])
        for batch in nba_batches(events):
            if not all(e["status"]["type"]["state"] == "post" for e in batch):
                continue
            todo = [e for e in batch if not st["nba"].get(e["id"], {}).get("posted")]
            if not todo:
                continue
            fresh = [e for e in todo if force or (now_utc() - parse_time(e["date"])).total_seconds() < STALE_HOURS * 3600]
            if fresh and not post_nba_batch(fresh):
                continue                 # Discord failed: retry next time
            for e in todo:
                st["nba"][e["id"]] = {"date": e["date"][:10], "posted": True}


def post_nba_batch(events):
    lines, extra = [], []
    for e in events:
        away, home = teams_of(e)
        if not e["status"]["type"].get("completed"):
            lines.append(f"{away['team']['abbreviation']} @ {home['team']['abbreviation']} — "
                         f"{e['status']['type'].get('description', 'Postponed')}")
            continue
        lines.append(f"{score_line(away, home, bold_winner=True)}{ot_tag(e)}")
        try:
            ps = nba_players(fetch(f"basketball/nba/summary?event={e['id']}"))
        except Exception as ex:  # noqa: BLE001
            log(f"NBA summary {e['id']} error: {ex}")
            ps = []
        if ps:
            top = ps[0]
            fire = " 🔥" if top["elite"] else ""
            lines.append(f"↳ ⭐ **{top['name']}** ({top['team']}) {top['line']}{fire}")
            extra += [p for p in ps[1:] if p["elite"]]
        lines.append("")
    first = parse_time(events[0]["date"])
    desc = "\n".join(lines).strip()
    if extra:
        desc += "\n\n**🔥 Also elite**\n" + "\n".join(
            f"**{p['name']}** ({p['team']}) {p['line']}" for p in extra)
    embed = {
        "title": f"🏀 NBA Full-Time — tip-offs from {uk_time(first)} UK",
        "description": desc[:4000],
        "color": 0xC9082A,
        "footer": {"text": f"fpts = {CONFIG['nba'].get('league_name', 'league')} scoring"},
    }
    return discord(NBA_HOOK, embed, "NBA batch")


# ===================================================================== main

def send_test():
    for name, hook in (("NFL", NFL_HOOK), ("NBA", NBA_HOOK)):
        if not hook:
            log(f"{name}_WEBHOOK_URL not set — skipped")
            continue
        ok = discord(hook, {"title": f"✅ {name} bot connected",
                            "description": "Scores will appear in this channel.",
                            "color": 0x2ECC71}, f"{name} test")
        log(f"{name} test message: {'sent' if ok else 'FAILED'}")


def main():
    global SILENT, DRY_RUN
    args = sys.argv[1:]
    if args and args[0] == "--test":
        return send_test()
    if args and args[0] in ("--preview-nfl", "--preview-nba"):
        DRY_RUN = True
        st = {"nfl": {}, "nba": {}}
        if args[0] == "--preview-nfl":
            run_nfl(st, date=args[1], force=True)
        else:
            run_nba(st, dates=[args[1]], force=True)
        return

    st = load_state()
    st.setdefault("nfl", {})
    st.setdefault("nba", {})
    if not st.get("initialized"):
        log("First run: recording current games without posting.")
        SILENT = True
        run_nfl(st)
        run_nba(st)
        SILENT = False
        st["initialized"] = True
        save_state(st)
        return

    start = time.time()
    last_nba = 0.0
    while True:
        active = False
        try:
            active = run_nfl(st)
        except Exception as ex:  # noqa: BLE001
            log(f"NFL error: {ex}")
        if time.time() - last_nba >= NBA_EVERY:
            try:
                run_nba(st)
            except Exception as ex:  # noqa: BLE001
                log(f"NBA error: {ex}")
            last_nba = time.time()
        save_state(st)
        if not active or time.time() - start > MAX_RUNTIME:
            break
        log(f"NFL game live — next check in {POLL_SECONDS}s")
        time.sleep(POLL_SECONDS)
    log("Done.")


if __name__ == "__main__":
    main()
