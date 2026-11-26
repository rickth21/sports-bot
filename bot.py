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


def discord(hook, embed, label="", content=None, files=None):
    """Post one embed (or a list of up to 10 as one message). True when safe to mark as posted."""
    if SILENT:
        return True
    embeds = embed if isinstance(embed, list) else [embed]
    if CAPTURE is not None:              # used by the simulator / previews
        if files or content:
            import copy as _copy
            import tempfile as _tf
            ems = _copy.deepcopy(embeds)
            for name, data in (files or {}).items():
                fp = Path(_tf.mkdtemp()) / name
                fp.write_bytes(data)
                for em in ems:
                    if (em.get("image") or {}).get("url") == f"attachment://{name}":
                        em["image"]["url"] = fp.as_uri()
            CAPTURE.append({"content": content, "embeds": ems})
        else:
            CAPTURE.append(embed)
        return True
    if DRY_RUN or not hook:
        if content:
            print("\n" + content)
        for em in embeds:
            print("\n" + "=" * 70)
            print(f"[{label or 'post'}]  {em.get('title', '')}")
            if em.get("description"):
                print(em["description"])
            for f in em.get("fields", []):
                print(f"-- {f['name']}\n{f['value']}")
            if em.get("footer"):
                print(f"({em['footer']['text']})")
        return True
    payload = {"username": "Scores Bot", "embeds": embeds[:10]}
    if content:
        payload["content"] = content
    if files:
        boundary = "----sportsbot" + str(int(time.time() * 1000))
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                 f"Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode() + b"\r\n"]
        for i, (name, data) in enumerate(files.items()):
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[{i}]\"; filename=\"{name}\"\r\n"
                         f"Content-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        ctype = f"multipart/form-data; boundary={boundary}"
    else:
        body, ctype = json.dumps(payload).encode(), "application/json"
    for _ in range(5):
        req = urllib.request.Request(
            hook, data=body, method="POST",
            headers={"Content-Type": ctype,
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
    for league in ("nfl", "nba", "nfl_slots"):
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
            players = sleeper_players(sport)
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


def league_owner(espn_id, name="", sport="nfl"):
    idx = league_index(sport)
    return idx.get(str(espn_id)) or idx.get(name_key(name))


def owner_text(espn_id, name="", sport="nfl"):
    o = league_owner(espn_id, name, sport)
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

    fan, fan_card = [], []
    import html as _H
    for who in ([td["qb"]] if td.get("qb") else []) + [td["scorer"]]:
        p = player_tonight(summary, who)
        if p:
            fan.append(f"**{p['name']}** — {p['line']} · **{fmt_pts(p['pts'])} fpts**\n↳ {owner_text(p['id'], p['name'])}")
            o = league_owner(p["id"], p["name"])
            own = (f"{o['manager']}{' ⭐' if o['starting'] else ' (bench)'}") if o else "unrostered"
            fan_card.append(f"<b>{_H.escape(p['name'])}</b> {fmt_pts(p['pts'])} fpts · {_H.escape(own)}")
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
    wp_txt = ""
    if wp:
        now, before = wp
        wp_txt = f"{abbr} {round(now * 100)}%" + ("" if before is None else f" {'▲' if now >= before else '▼'}{abs(round((now - before) * 100))}")
    drv = drive.get("description") if drive and (drive.get("team") or {}).get("abbreviation", abbr) == abbr else ""
    png = render_png(td_card_html(event, play, summary, td, tag, qtxt, clock, drv, wp_txt, down_text, fan_card),
                     880, 362 if fan_card else 292)
    if png:
        who = f"{td['qb']} ➜ {td['scorer']}" if td["kind"] == "pass" else td["scorer"]
        yd = f"{yds}-yd " if yds is not None else ""
        kind = {"pass": "TD pass", "rush": "TD run", "int": "pick-six"}.get(td["kind"], "TD")
        embed = {"author": {"name": t.get("displayName", abbr), "icon_url": t.get("logo", "")},
                 "title": f"{who} · {yd}{kind}",
                 "description": call or None,
                 "color": color(t.get("color")),
                 "image": {"url": "attachment://td.png"}}
        if not embed["description"]:
            del embed["description"]
        content = f"🏈 **TOUCHDOWN {nick.upper()}** · {a_ab} {a_s}–{h_s} {h_ab} · {qtxt} {clock}"
        return discord(NFL_HOOK, embed, "NFL TD", content=content, files={"td.png": png})
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
    head, tag = nfl_headline(event)
    png = render_png(ft_card_html(event, summary, ff, tag), 880, 530 if summary.get("boxscore", {}).get("teams") else 420)
    if png:
        lines = []
        if ff:
            top = ff[0]
            lines.append(f"⭐ **{top['name']}** — {top['line']} · {fmt_pts(top['pts'])} fpts")
            for p in [x for x in ff[1:] if x["elite"]][:3]:
                lines.append(f"🔥 {p['name']} — {fmt_pts(p['pts'])} fpts")
        rec = record_of(winner) if a != h else ""
        small = {"author": {"name": "NFL", "icon_url": NFL_LOGO},
                 "title": f"{head} · {max(a, h)}-{min(a, h)}{ot_tag(event)}",
                 "url": f"https://www.espn.com/nfl/game/_/gameId/{event['id']}",
                 "description": "\n".join(([f"{winner['team'].get('displayName')} move to {rec}"] if rec else []) + lines)[:4000],
                 "color": color(winner["team"].get("color")),
                 "image": {"url": "attachment://ft.png"},
                 "footer": {"text": f"fpts = {CONFIG['nfl'].get('league_name', 'league')} scoring", "icon_url": NFL_LOGO}}
        content = (f"🏁 **FULL-TIME** · {away['team'].get('displayName')} {a}–{h} "
                   f"{home['team'].get('displayName')}{ot_tag(event)}")
        return discord(NFL_HOOK, small, "NFL FT", content=content, files={"ft.png": png})
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
        # prime time: stay on from ~70 min before kick-off so one run covers preview + game
        return (-1.2 < age < 0.5) if prime else (-0.4 < age < 0.5)

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
    for slot in nfl_slots(events):
        if len(slot) >= 2:               # games kicking off together: grouped slate posts
            try:
                active |= bool(nfl_slot(slot, st, force=force))
            except Exception as ex:  # noqa: BLE001
                log(f"NFL slate {slot[0].get('date')} error: {ex}")
            continue
        try:
            active |= bool(nfl_game(slot[0], st, force=force))
        except Exception as ex:  # noqa: BLE001
            log(f"NFL game {slot[0].get('id')} error: {ex}")
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
                            "raw": {"pts": int(pts), "reb": int(reb), "ast": int(ast), "stl": int(stl),
                                    "blk": int(blk), "tpm": int(tpm)},
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


NBA_LOGO = "https://a.espncdn.com/i/teamlogos/leagues/500/nba.png"


def post_nba_batch(events):
    """One message per tip-off batch, one card per game (Discord allows 10 cards per message)."""
    if len(events) == 1 and events[0]["status"]["type"].get("completed"):
        return post_nba_game(events[0])
    cards = []
    first = parse_time(events[0]["date"])
    league = CONFIG["nba"].get("league_name", "league")
    for e in events:
        away, home = teams_of(e)
        an = away["team"].get("name") or away["team"]["abbreviation"]
        hn = home["team"].get("name") or home["team"]["abbreviation"]
        if not e["status"]["type"].get("completed"):
            cards.append({"author": {"name": "NBA · " + e["status"]["type"].get("description", "Postponed").upper(),
                                     "icon_url": NBA_LOGO},
                          "title": f"{an} @ {hn}", "color": 0x5865F2})
            continue
        a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
        winner, loser = (away, home) if a_s > h_s else (home, away)
        rec_w, rec_l = record_of(winner), record_of(loser)
        desc = [f"🏆 **{winner['team'].get('displayName')}** win"
                + (f" · {rec_w}" if rec_w else "")
                + (f"  |  {loser['team'].get('name')} {rec_l}" if rec_l else "")]
        try:
            ps = nba_players(fetch(f"basketball/nba/summary?event={e['id']}"))
        except Exception as ex:  # noqa: BLE001
            log(f"NBA summary {e['id']} error: {ex}")
            ps = []
        if ps:
            top = ps[0]
            fire = " 🔥" if top["elite"] else ""
            desc.append(f"\n⭐ **{top['name']}** ({top['team']}){fire}\n{top['line']}")
            o = league_owner("", top["name"], "nba")
            desc.append(f"↳ 🧢 {owner_text('', top['name'], 'nba')}" if o else "↳ 🆓 unrostered in the league")
            more = [p for p in ps[1:] if p["elite"]]
            if more:
                desc.append("\n🔥 **Also elite:** " + " · ".join(
                    f"{p['name']} ({p['team']}) {fmt_pts(p['fp'])} fpts" for p in more))
        cards.append({
            "author": {"name": "NBA · FULL TIME" + ot_tag(e).upper(), "icon_url": NBA_LOGO},
            "title": f"{an} {a_s} – {h_s} {hn}",
            "description": "\n".join(desc)[:4000],
            "color": color(winner["team"].get("color"), 0xC9082A),
            "thumbnail": {"url": winner["team"].get("logo", "")},
        })
    if not cards:
        return True
    cards[-1]["footer"] = {"text": f"Tip-offs from {uk_time(first)} UK  ·  fpts = {league} scoring",
                           "icon_url": NBA_LOGO}
    ok = True
    for i in range(0, len(cards), 10):
        ok = discord(NBA_HOOK, cards[i:i + 10], "NBA batch") and ok
    return ok


# ------------------------------------------------------ NBA full-time card post

CHROME_CANDIDATES = [os.environ.get("CHROME_BIN", ""), "google-chrome", "google-chrome-stable", "chromium",
                     "chromium-browser", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]


def find_chrome():
    import shutil
    for c in CHROME_CANDIDATES:
        if c and (shutil.which(c) or Path(c).exists()):
            return shutil.which(c) or c
    return None


def hex_rgb(h, default="5865f2"):
    h = (h or default).strip("#")
    if len(h) != 6:
        h = default
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def bright(h, alt=None):
    """A version of the team colour that shows up on a dark card."""
    r, g, b = hex_rgb(h)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    if lum < 0.28 and alt:
        ar, ag, ab = hex_rgb(alt)
        if (0.299 * ar + 0.587 * ag + 0.114 * ab) / 255 > lum and (ar, ag, ab) != (255, 255, 255):
            r, g, b = ar, ag, ab
            lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    if lum < 0.35:
        k = 0.45
        r, g, b = (int(c + (255 - c) * k) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def team_stats(summary, abbr):
    for t in summary.get("boxscore", {}).get("teams", []):
        if t["team"]["abbreviation"] == abbr:
            return {s["name"]: s.get("displayValue", "") for s in t.get("statistics", [])}
    return {}


def conf_rank(summary, team_id):
    for grp in (summary.get("standings") or {}).get("groups", []):
        entries = (grp.get("standings") or {}).get("entries", [])
        for i, e in enumerate(entries, 1):
            if str(e.get("id")) == str(team_id):
                hdr = grp.get("header", "") or grp.get("name", "") or ""
                div = re.sub(r"^\d{4}(-\d{2})?\s*|\s*Standings$", "", hdr).strip()
                return i, div
    return None, ""


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def top_scorers(summary, abbr, n=2):
    rows = []
    for team in summary.get("boxscore", {}).get("players", []):
        if team["team"]["abbreviation"] != abbr:
            continue
        for block in team.get("statistics", []):
            keys = block.get("keys", [])
            for a in block.get("athletes", []):
                if a.get("stats"):
                    s = dict(zip(keys, a["stats"]))
                    rows.append((num(s.get("points")), a["athlete"]["displayName"]))
    rows.sort(key=lambda r: -r[0])
    return rows[:n]


def headshot(summary, name):
    for team in summary.get("boxscore", {}).get("players", []):
        for block in team.get("statistics", []):
            for a in block.get("athletes", []):
                if a["athlete"]["displayName"] == name:
                    return (a["athlete"].get("headshot") or {}).get("href", "")
    return ""


def nba_story(event, summary, players):
    """Headline, recap sentences and bullet insights, written from the box score."""
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    w, l = (away, home) if a_s > h_s else (home, away)
    ws, ls = max(a_s, h_s), min(a_s, h_s)
    wn, ln = w["team"].get("name"), l["team"].get("name")
    wcity = w["team"].get("location") or w["team"].get("displayName")
    margin = ws - ls
    ot = ot_tag(event).strip(" ()")
    tw, tl = team_stats(summary, w["team"]["abbreviation"]), team_stats(summary, l["team"]["abbreviation"])
    lead_l = int(num(tl.get("largestLead")))
    lead_w = int(num(tw.get("largestLead")))

    if ot:
        head, verb, tag = f"{wn} outlast the {ln} in {ot}", "outlasted", f"{ot} THRILLER"
    elif lead_l >= 12:
        head, verb, tag = f"{wn} storm back to stun the {ln}", "came back to beat", "COMEBACK WIN"
    elif margin >= 20:
        head, verb, tag = f"{wn} rout the {ln}", "routed", f"{ws}-{ls} ROUT"
    elif margin >= 10:
        head, verb, tag = f"{wn} cruise past the {ln}", "beat", "COMFORTABLE WIN"
    elif margin >= 4:
        head, verb, tag = f"{wn} hold off the {ln}", "held off", f"{wn.upper()} HOLD ON"
    else:
        head, verb, tag = f"{wn} edge the {ln} in a thriller", "edged", f"{margin}-POINT THRILLER"

    rec = record_of(w)
    s = [f"{wcity} {verb} the {ln} {ws}-{ls}" + (f" to move to {rec}" if rec else "") + "."]
    pog = players[0] if players else None
    if pog:
        bits = pog["raw"]
        line = f"{bits['pts']} points, {bits['reb']} rebounds and {bits['ast']} assists"
        s.append(f"{pog['name']} {'led the way' if pog['team'] == w['team']['abbreviation'] else 'starred in defeat'} with {line}"
                 + (f", hitting {bits['tpm']} threes" if bits["tpm"] >= 4 else "") + ".")
    lt = top_scorers(summary, l["team"]["abbreviation"], 1)
    if lt and (not pog or lt[0][1] not in pog["name"]):
        why = ""
        if num(tl.get("threePointFieldGoalPct")) and num(tl.get("threePointFieldGoalPct")) < 32:
            why = f", but the {ln} shot just {tl.get('threePointFieldGoalsMade-threePointFieldGoalsAttempted')} from deep"
        elif num(tl.get("totalTurnovers")) >= 16:
            why = f", but the {ln} gave it away {int(num(tl.get('totalTurnovers')))} times"
        elif num(tw.get("totalRebounds")) - num(tl.get("totalRebounds")) >= 8:
            why = f", but the {ln} were beaten {tw.get('totalRebounds')}-{tl.get('totalRebounds')} on the glass"
        s.append(f"{lt[0][1]} scored {int(lt[0][0])} for the {ln}{why}.")

    bullets = []
    if lead_l >= 10:
        bullets.append(f"{wn} overturned a {lead_l}-point deficit")
    elif lead_w >= 15:
        bullets.append(f"{wn} led by as many as {lead_w}")
    tp_w, tp_l = num(tw.get("threePointFieldGoalPct")), num(tl.get("threePointFieldGoalPct"))
    if tp_w and tp_l and abs(tp_w - tp_l) >= 12:
        hi, lo = (w, l) if tp_w > tp_l else (l, w)
        bullets.append(f"From three: {hi['team'].get('name')} {max(tp_w, tp_l):.0f}% vs {lo['team'].get('name')} {min(tp_w, tp_l):.0f}%")
    tov_w, tov_l = num(tw.get("totalTurnovers")), num(tl.get("totalTurnovers"))
    if abs(tov_w - tov_l) >= 6:
        bullets.append(f"Turnovers: {wn} {int(tov_w)} · {ln} {int(tov_l)}")
    return head, tag, " ".join(s), bullets[:2]


CARD_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{width:880px;height:530px;font-family:Inter,'Helvetica Neue',Arial,sans-serif;color:#fff;background:#0d1220}
.card{position:relative;width:880px;height:530px;overflow:hidden;border-radius:18px;background:#0d1220}
.bg{position:absolute;inset:0;background:linear-gradient(115deg,var(--a) 0%,rgba(13,18,32,.0) 48%),
 linear-gradient(295deg,var(--h) 0%,rgba(13,18,32,0) 48%);opacity:.38}
.split{position:absolute;inset:0;background:linear-gradient(105deg,transparent 49.6%,rgba(255,255,255,.06) 50%,transparent 50.4%)}
.in{position:relative;padding:22px 30px}
.top{display:flex;justify-content:space-between;align-items:center;font-size:13px;letter-spacing:2px;color:#aab3c5;font-weight:600}
.top .lg{display:flex;align-items:center;gap:10px}.top img{width:26px;height:26px;background:#fff;border-radius:6px;padding:2px}
.pill{border:1.5px solid #8b95a8;border-radius:6px;padding:4px 10px;font-size:12px;color:#e6e9ef}
.score{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;margin-top:18px}
.tm{display:flex;align-items:center;gap:16px}.tm.r{justify-content:flex-end}
.tm img{width:86px;height:86px;object-fit:contain;filter:drop-shadow(0 2px 6px rgba(0,0,0,.4))}
.nm{font-size:22px;font-weight:800;letter-spacing:.5px;text-transform:uppercase;line-height:1.1}
.rk{font-size:12.5px;color:#aab3c5;margin-top:5px;font-weight:500;letter-spacing:.3px}
.sc{font-size:12.5px;color:#cfd5e1;margin-top:8px;font-weight:500;white-space:nowrap}
.nums{display:flex;gap:10px;align-items:center}
.box{min-width:104px;height:84px;border-radius:12px;background:rgba(255,255,255,.08);display:flex;align-items:center;justify-content:center;
 font-size:54px;font-weight:800;font-variant-numeric:tabular-nums}
.box.w{background:rgba(255,255,255,.16)}
.dot{width:8px;height:8px;border-radius:50%;background:#8b95a8}
.tag{text-align:center;font-size:12px;letter-spacing:2.5px;font-weight:700;margin-top:8px}
.qs{display:flex;justify-content:center;margin-top:12px}
.qs table{border-collapse:collapse;font-size:12.5px;color:#cfd5e1;font-variant-numeric:tabular-nums}
.qs td,.qs th{padding:2px 9px;text-align:center}.qs th{color:#7d879b;font-weight:600;font-size:11px}
.qs td.t{font-weight:800;color:#fff}.qs td.n{text-align:left;font-weight:700;color:#fff}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:14px}
.tile{background:rgba(255,255,255,.055);border-radius:10px;padding:9px 12px 10px}
.tl{font-size:10.5px;letter-spacing:1.6px;color:#9aa3b5;font-weight:600;text-align:center}
.tv{display:flex;justify-content:center;align-items:baseline;gap:10px;font-size:23px;font-weight:800;margin-top:3px;font-variant-numeric:tabular-nums}
.tv .lo{color:#9aa3b5}.tv i{font-style:normal;color:#5d6679;font-size:16px}
.bars{display:flex;gap:6px;margin-top:6px}.bars div{height:4px;border-radius:2px}
.bot{display:flex;justify-content:space-between;align-items:center;margin-top:14px}
.pog{display:flex;align-items:center;gap:12px}
.pog img{width:58px;height:58px;border-radius:50%;object-fit:cover;object-position:top;background:#1d2436;border:2px solid rgba(255,255,255,.15)}
.pl{font-size:10.5px;letter-spacing:1.6px;color:#9aa3b5;font-weight:600}
.pn{font-size:20px;font-weight:800;margin-top:1px}.ps{font-size:13px;color:#cfd5e1;margin-top:2px}
.chip{background:#1faa59;color:#fff;font-weight:800;font-size:15px;border-radius:6px;padding:3px 8px;margin-left:6px}
.own{font-size:12.5px;color:#cfd5e1;text-align:right;line-height:1.5}
.own b{color:#fff}
"""


def nba_card_html(event, summary, players, tag):
    import html as H
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    ca = bright(away["team"].get("color"))
    ch = bright(home["team"].get("color"))
    if sum(abs(x - y) for x, y in zip(hex_rgb(ca), hex_rgb(ch))) < 120:
        ch = bright(home["team"].get("alternateColor")) if home["team"].get("alternateColor") else "#cfd5e1"
        if sum(abs(x - y) for x, y in zip(hex_rgb(ca), hex_rgb(ch))) < 120:
            ch = "#cfd5e1"
    ta, th = team_stats(summary, away["team"]["abbreviation"]), team_stats(summary, home["team"]["abbreviation"])
    ot = ot_tag(event).strip(" ()")

    def side(t, right):
        rank, conf = conf_rank(summary, t["team"].get("id"))
        rk = (f"{ordinal(rank)} in {conf} · " if rank and conf else "") + (record_of(t) or "")
        sc = " · ".join(f"{H.escape(([w for w in n.split(' ') if w.rstrip('.').lower() not in ('jr', 'sr', 'ii', 'iii', 'iv')] or [n])[-1])} {int(p)}"
                        for p, n in top_scorers(summary, t["team"]["abbreviation"]))
        txt = (f"<div><div class='nm'>{H.escape(t['team'].get('name', ''))}</div>"
               f"<div class='rk'>{H.escape(rk)}</div><div class='sc'>{sc}</div></div>")
        img = f"<img src='{H.escape(t['team'].get('logo', ''))}'>"
        return f"<div class='tm{' r' if right else ''}' style='{'text-align:right' if right else ''}'>" + \
            (txt + img if right else img + txt) + "</div>"

    # quarter table
    la = [str(int(num(x.get("value", x.get("displayValue", 0))))) for x in away.get("linescores", [])]
    lh = [str(int(num(x.get("value", x.get("displayValue", 0))))) for x in home.get("linescores", [])]
    n = max(len(la), len(lh))
    hdr = "".join(f"<th>{q}</th>" for q in (["Q1", "Q2", "Q3", "Q4"] + [f"OT{i}" if i > 1 else "OT" for i in range(1, n - 3)])[:n])
    rows = "".join(f"<tr><td class='n'>{t['team']['abbreviation']}</td>" + "".join(f"<td>{v}</td>" for v in ls)
                   + f"<td class='t'>{sc}</td></tr>" for t, ls, sc in ((away, la, a_s), (home, lh, h_s)))
    qtable = f"<table><tr><th></th>{hdr}<th>T</th></tr>{rows}</table>" if n else ""

    def tile(label, av, hv, fmt=lambda v: f"{v:g}", lower_better=False, show=None):
        a, h = num(av), num(hv)
        tot = (a + h) or 1
        better_a = (a < h) if lower_better else (a > h)
        sa, sh = show or (fmt(a), fmt(h))
        return (f"<div class='tile'><div class='tl'>{label}</div><div class='tv'>"
                f"<span class='{'' if better_a or a == h else 'lo'}'>{sa}</span><i>·</i>"
                f"<span class='{'' if (not better_a) or a == h else 'lo'}'>{sh}</span></div>"
                f"<div class='bars'><div style='flex:{a / tot:.3f};background:{ca}'></div>"
                f"<div style='flex:{h / tot:.3f};background:{ch}'></div></div></div>")

    tiles = "".join([
        tile("FIELD GOAL %", ta.get("fieldGoalPct"), th.get("fieldGoalPct"), lambda v: f"{v:.0f}%"),
        tile("THREE POINTERS", ta.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0").split("-")[0],
             th.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted", "0").split("-")[0]),
        tile("REBOUNDS", ta.get("totalRebounds"), th.get("totalRebounds")),
        tile("ASSISTS", ta.get("assists"), th.get("assists")),
        tile("TURNOVERS", ta.get("totalTurnovers"), th.get("totalTurnovers"), lower_better=True),
        tile("POINTS IN PAINT", ta.get("pointsInPaint"), th.get("pointsInPaint")),
    ])

    pog = players[0] if players else None
    pog_html = own_html = ""
    if pog:
        hs = headshot(summary, pog["name"])
        r = pog["raw"]
        st = f"{r['pts']} PTS · {r['reb']} REB · {r['ast']} AST" + (f" · {r['stl']} STL" if r["stl"] >= 2 else "") + \
             (f" · {r['blk']} BLK" if r["blk"] >= 2 else "")
        pog_html = (f"<div class='pog'>{'<img src=' + chr(39) + H.escape(hs) + chr(39) + '>' if hs else ''}<div>"
                    f"<div class='pl'>PLAYER OF THE GAME</div><div class='pn'>{H.escape(pog['name'])}"
                    f"<span class='chip'>{fmt_pts(pog['fp'])}</span></div><div class='ps'>{st}</div></div></div>")
        o = league_owner("", pog["name"], "nba")
        league = CONFIG["nba"].get("league_name", "League")
        own_html = (f"<div class='own'>{H.escape(league)} league<br><b>{H.escape(o['manager'])}</b>"
                    f"{' · starting' if o['starting'] else ' · bench'}</div>") if o else \
                   f"<div class='own'>{H.escape(league)} league<br><b>Unrostered</b> · waiver pickup?</div>"

    w_col = ca if a_s > h_s else ch
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800&display=swap' rel='stylesheet'>"
            f"<style>{CARD_CSS}</style></head><body><div class='card' style='--a:{ca};--h:{ch}'>"
            f"<div class='bg'></div><div class='split'></div><div class='in'>"
            f"<div class='top'><div class='lg'><img src='{NBA_LOGO}'>NBA</div>"
            f"<div class='pill'>{'FINAL · ' + ot if ot else 'FULL-TIME'}</div></div>"
            f"<div class='score'>{side(away, False)}<div><div class='nums'>"
            f"<div class='box{' w' if a_s > h_s else ''}'>{a_s}</div><div class='dot'></div>"
            f"<div class='box{' w' if h_s > a_s else ''}'>{h_s}</div></div>"
            f"<div class='tag' style='color:{w_col}'>{H.escape(tag)}</div></div>{side(home, True)}</div>"
            f"<div class='qs'>{qtable}</div><div class='tiles'>{tiles}</div>"
            f"<div class='bot'>{pog_html}{own_html}</div></div></div></body></html>")


def render_png(html_text, width=880, height=530):
    """HTML -> PNG with headless Chrome. Returns bytes, or None if Chrome isn't available."""
    import subprocess
    import tempfile
    chrome = find_chrome()
    if not chrome:
        log("No Chrome found: posting without the image card")
        return None
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "card.html", Path(d) / "card.png"
        src.write_text(html_text)
        try:
            subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                            "--force-device-scale-factor=2", f"--window-size={width},{height}",
                            "--virtual-time-budget=8000", "--default-background-color=00000000",
                            f"--screenshot={out}", src.as_uri()],
                           check=True, timeout=60, capture_output=True)
            return out.read_bytes()
        except Exception as e:  # noqa: BLE001
            log(f"card render failed: {e}")
            return None


def post_nba_game(e):
    """One full-time post for a single game: headline, recap, insights and a graphic card."""
    summary = fetch(f"basketball/nba/summary?event={e['id']}")
    players = nba_players(summary)
    away, home = teams_of(e)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    winner = away if a_s > h_s else home
    head, tag, recap, bullets = nba_story(e, summary, players)

    extra = []
    if players:
        p = players[0]
        o = league_owner("", p["name"], "nba")
        extra.append(f"🧢 **{p['name']}** {fmt_pts(p['fp'])} fpts — "
                     + (f"**{o['manager']}**{' ⭐ starting' if o['starting'] else ' (bench)'}" if o else "unrostered 👀"))
        more = [x for x in players[1:] if x["elite"]]
        if more:
            extra.append("🔥 Also elite: " + " · ".join(f"{x['name']} {fmt_pts(x['fp'])}" for x in more[:3]))
    desc = "> " + recap + "\n\n" + "\n".join(f"• {b}" for b in bullets + extra)

    embed = {
        "author": {"name": "NBA", "icon_url": NBA_LOGO},
        "title": f"{head} · {max(a_s, h_s)}-{min(a_s, h_s)}{ot_tag(e)}",
        "url": f"https://www.espn.com/nba/game/_/gameId/{e['id']}",
        "description": desc[:4000],
        "color": color(winner["team"].get("color"), 0xC9082A),
        "footer": {"text": f"fpts = {CONFIG['nba'].get('league_name', 'league')} scoring", "icon_url": NBA_LOGO},
    }
    content = (f"🏁 **FULL-TIME** · {away['team'].get('displayName')} {a_s}–{h_s} {home['team'].get('displayName')}"
               f"{ot_tag(e)}")
    png = render_png(nba_card_html(e, summary, players, tag))
    if png:
        embed["image"] = {"url": "attachment://card.png"}
    return discord(NBA_HOOK, embed, "NBA FT", content=content, files={"card.png": png} if png else None)


# ------------------------------------------------------------ NFL image cards
# Compact graphic cards in the same style as the football bot: short text in the
# message, all the detail in one image.

NFL_CSS = CARD_CSS + """
.formd{display:flex;gap:4px;margin-top:6px}.formd span{width:9px;height:9px;border-radius:50%}
.tm.r .formd{justify-content:flex-end}
.ko{min-width:150px;height:84px;border-radius:12px;background:rgba(255,255,255,.1);display:flex;flex-direction:column;align-items:center;justify-content:center}
.ko b{font-size:40px;font-weight:800;letter-spacing:1px}.ko small{font-size:10.5px;letter-spacing:2px;color:#aab3c5;font-weight:600}
.sub{text-align:center;font-size:12px;color:#aab3c5;margin-top:8px;font-weight:500}
.two{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.col{background:rgba(255,255,255,.055);border-radius:10px;padding:9px 12px}
.ch{font-size:10.5px;letter-spacing:1.6px;color:#9aa3b5;font-weight:600;margin-bottom:5px}
.pr{display:flex;align-items:center;gap:9px;margin-top:5px;font-size:13px}
.pr img{width:30px;height:30px;border-radius:50%;object-fit:cover;object-position:top;background:#1d2436}
.pr b{font-weight:700}.pr span{color:#aab3c5;font-size:12px}
.strip{display:flex;gap:10px;margin-top:10px}
.strip .col{flex:1;font-size:12px;line-height:1.55;color:#cfd5e1}
.strip b{color:#fff}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.chip2{background:rgba(255,255,255,.09);border-radius:6px;padding:2px 7px;font-size:11.5px;color:#e6e9ef}
.chip2 b{color:#fff}
.tdhead{display:flex;align-items:center;gap:18px;margin-top:14px}
.tdhead .hs{width:118px;height:118px;border-radius:14px;object-fit:cover;object-position:top;background:rgba(255,255,255,.08)}
.big{font-size:44px;font-weight:900;letter-spacing:1px;line-height:1}
.who{font-size:24px;font-weight:800;margin-top:8px}.how{font-size:14px;color:#cfd5e1;margin-top:4px}
.mini{margin-left:auto;display:flex;flex-direction:column;gap:6px}
.mrow{display:flex;align-items:center;gap:10px;background:rgba(255,255,255,.07);border-radius:9px;padding:5px 12px;min-width:150px}
.mrow img{width:30px;height:30px;object-fit:contain}.mrow b{margin-left:auto;font-size:26px;font-weight:800}
.mrow.on{background:rgba(255,255,255,.18)}
.tiles4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:14px}
.tv2{font-size:17px;font-weight:800;text-align:center;margin-top:4px}
"""


def _page(body, css_vars, height):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800;900&display=swap' rel='stylesheet'>"
            f"<style>{NFL_CSS}body,.card{{height:{height}px}}</style></head><body>"
            f"<div class='card' style='{css_vars}'><div class='bg'></div><div class='split'></div>"
            f"<div class='in'>{body}</div></div></body></html>")


def nfl_headshot(pid):
    return f"https://a.espncdn.com/i/headshots/nfl/players/full/{pid}.png" if pid and str(pid).isdigit() else ""


def team_cols(away, home):
    ca, ch = bright(away["team"].get("color")), bright(home["team"].get("color"))
    if sum(abs(x - y) for x, y in zip(hex_rgb(ca), hex_rgb(ch))) < 120:
        ch = bright(home["team"].get("alternateColor")) if home["team"].get("alternateColor") else "#cfd5e1"
        if sum(abs(x - y) for x, y in zip(hex_rgb(ca), hex_rgb(ch))) < 120:
            ch = "#cfd5e1"
    return ca, ch


def form_dots(summary, abbr):
    for t in summary.get("lastFiveGames") or []:
        if t.get("team", {}).get("abbreviation") == abbr:
            evs = sorted(t.get("events") or [], key=lambda e: e.get("gameDate", ""))
            c = {"W": "#2fbf71", "L": "#e5484d", "T": "#8b95a8"}
            return "<div class='formd'>" + "".join(
                f"<span style='background:{c.get(e.get('gameResult'), '#8b95a8')}'></span>" for e in evs) + "</div>"
    return ""


def nfl_side(t, right, extra=""):
    import html as H
    txt = (f"<div><div class='nm'>{H.escape(t['team'].get('name', ''))}</div>"
           f"<div class='rk'>{H.escape(record_of(t) or '')}</div>{extra}</div>")
    img = f"<img src='{H.escape(t['team'].get('logo', ''))}'>"
    return (f"<div class='tm{' r' if right else ''}' style='{'text-align:right' if right else ''}'>"
            + (txt + img if right else img + txt) + "</div>")


def preview_card_html(event, summary):
    import html as H
    away, home = teams_of(event)
    a_ab, h_ab = away["team"]["abbreviation"], home["team"]["abbreviation"]
    ca, ch = team_cols(away, home)
    kick = parse_time(event["date"]).astimezone(UK)
    gi = summary.get("gameInfo") or {}
    venue = (gi.get("venue") or {}).get("fullName", "")
    week = (summary.get("header") or {}).get("week")

    pc = (summary.get("pickcenter") or [{}])[0]
    try:
        hp = float((summary.get("predictor") or {})["homeTeam"]["gameProjection"])
    except (KeyError, TypeError, ValueError):
        hp = None
    tiles = [f"<div class='tile'><div class='tl'>SPREAD</div><div class='tv'>{H.escape(pc.get('details') or '—')}</div></div>",
             f"<div class='tile'><div class='tl'>OVER / UNDER</div><div class='tv'>{pc.get('overUnder', '—')}</div></div>"]
    if hp is not None:
        tiles.append(f"<div class='tile'><div class='tl'>ESPN WIN CHANCE</div><div class='tv'>"
                     f"<span class='{'' if hp < 50 else 'lo'}'>{100 - hp:.0f}%</span><i>·</i>"
                     f"<span class='{'' if hp >= 50 else 'lo'}'>{hp:.0f}%</span></div>"
                     f"<div class='bars'><div style='flex:{(100 - hp) / 100:.3f};background:{ca}'></div>"
                     f"<div style='flex:{hp / 100:.3f};background:{ch}'></div></div></div>")
    else:
        tiles.append("<div class='tile'><div class='tl'>VENUE</div><div class='tv' style='font-size:15px'>"
                     f"{H.escape(venue)}</div></div>")

    out_ids = {str(i["athlete"].get("id")) for tt in summary.get("injuries") or []
               for i in tt.get("injuries") or [] if i.get("status") in ("Out", "Injured Reserve", "Doubtful")}
    season = ((summary.get("header") or {}).get("season") or {}).get("year") or now_utc().year
    unit = {"passingYards": "pass yds", "rushingYards": "rush yds", "receivingYards": "rec yds"}

    def watch(t):
        """Season leaders (pass / rush / rec), skipping anyone out — next healthy player instead."""
        ab, tid = t["team"]["abbreviation"], t["team"].get("id")
        top = {}
        for tt in summary.get("leaders") or []:
            if tt.get("team", {}).get("abbreviation") == ab:
                for cat in tt.get("leaders") or []:
                    if cat.get("leaders"):
                        top[cat.get("name")] = cat["leaders"][0]
        deeper = {}
        try:
            core = fetch_url(f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/{season}"
                             f"/types/2/teams/{tid}/leaders", tries=2)
            for c in core.get("categories") or []:
                deeper[c.get("name")] = c.get("leaders") or []
        except Exception as e:  # noqa: BLE001
            log(f"team leaders unavailable: {e}")
        rows, used = [], set()
        for cat in ("passingYards", "rushingYards", "receivingYards"):
            pick = None
            x = top.get(cat)
            if x and str(x["athlete"].get("id")) not in out_ids and str(x["athlete"].get("id")) not in used:
                dv = x.get("displayValue", "")
                if cat == "passingYards":
                    dv = ", ".join(dv.split(", ")[1:]) or dv
                pick = (str(x["athlete"].get("id")), x["athlete"]["displayName"],
                        (x["athlete"].get("position") or {}).get("abbreviation", ""),
                        dv.replace(" YDS", " yds").replace(" CAR", " car").replace(" REC", " rec"))
            else:
                for l in deeper.get(cat, []):
                    pid = (l.get("athlete") or {}).get("$ref", "").split("/athletes/")[-1].split("?")[0]
                    if not pid or pid in out_ids or pid in used:
                        continue
                    try:
                        ath = fetch_url(f"https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/seasons/"
                                        f"{season}/athletes/{pid}", tries=2)
                    except Exception:  # noqa: BLE001
                        continue
                    pick = (pid, ath.get("displayName", ""), (ath.get("position") or {}).get("abbreviation", ""),
                            f"{l.get('displayValue', '')} {unit[cat]}")
                    break
            if not pick:
                continue
            pid, name, pos, dv = pick
            used.add(pid)
            hs = nfl_headshot(pid)
            rows.append(f"<div class='pr'>{'<img src=' + chr(39) + H.escape(hs) + chr(39) + '>' if hs else ''}"
                        f"<div><b>{H.escape(name)}</b> <span>{pos}</span><br>"
                        f"<span>{H.escape(dv)}</span></div></div>")
        return f"<div class='col'><div class='ch'>{H.escape(t['team'].get('name', ab).upper())} TO WATCH</div>{''.join(rows)}</div>"

    inj = []
    for t in (away, home):
        rows = [r for r in injury_lines(summary, t["team"]["abbreviation"], 6) if "(Q)" not in r][:3]
        if rows:
            inj.append(f"<b>{t['team']['abbreviation']}</b> " + " · ".join(H.escape(r) for r in rows))
    hurt = {name_key(i["athlete"]["displayName"]) for t in summary.get("injuries") or []
            for i in t.get("injuries") or [] if i.get("status") in ("Out", "Injured Reserve", "Doubtful")}
    teams = {SLEEPER_TEAM.get(x, x) for x in (a_ab, h_ab)}
    by_mgr, seen = {}, set()
    for o in league_index("nfl").values():
        if id(o) in seen:
            continue
        seen.add(id(o))
        if o["team"] in teams and o["starting"] and name_key(o["name"]) not in hurt:
            by_mgr.setdefault(o["manager"], []).append(o["name"].split(" ")[-1] if " " in o["name"] else o["name"])
    chips = "".join(f"<span class='chip2'><b>{H.escape(m)}</b> {H.escape(', '.join(ps))}</span>"
                    for m, ps in sorted(by_mgr.items(), key=lambda kv: -len(kv[1])))
    league = CONFIG["nfl"].get("league_name", "League")

    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL{' · WEEK ' + str(week) if week else ''}</div>"
            f"<div class='pill'>PRIME TIME</div></div>"
            f"<div class='score'>{nfl_side(away, False, form_dots(summary, a_ab))}"
            f"<div><div class='ko'><small>KICK-OFF UK</small><b>{kick.strftime('%H:%M')}</b></div>"
            f"<div class='sub'>{kick.strftime('%a %-d %b')} · {H.escape(venue)}</div></div>"
            f"{nfl_side(home, True, form_dots(summary, h_ab))}</div>"
            f"<div class='tiles'>{''.join(tiles)}</div>"
            f"<div class='two'>{watch(away)}{watch(home)}</div>"
            f"<div class='strip'>"
            + (f"<div class='col'><div class='ch'>INJURIES</div>{'<br>'.join(inj)}</div>" if inj else "")
            + (f"<div class='col'><div class='ch'>{H.escape(league.upper())} · STARTERS IN THIS GAME</div>"
               f"<div class='chips'>{chips}</div></div>" if chips else "")
            + "</div>")
    return _page(body, f"--a:{ca};--h:{ch}", 548)


def post_nfl_preview(event, summary):
    away, home = teams_of(event)
    a, h = away["team"], home["team"]
    kick = parse_time(event["date"]).astimezone(UK)
    comp = event["competitions"][0]
    tv = ", ".join(n for b in comp.get("broadcasts") or [] for n in b.get("names", []))
    head = (summary.get("article") or {}).get("headline") or f"{a.get('name')} visit the {h.get('name')}"
    week = (summary.get("header") or {}).get("week")
    pc = (summary.get("pickcenter") or [{}])[0]
    lines = [f"📺 {tv + ' (US) · ' if tv else ''}DAZN (UK)"]
    if pc.get("details"):
        lines.append(f"🎲 {pc['details']} · O/U {pc.get('overUnder', '—')}")
    embed = {
        "author": {"name": f"NFL{' · Week ' + str(week) if week else ''}", "icon_url": NFL_LOGO},
        "title": head,
        "url": f"https://www.espn.com/nfl/game/_/gameId/{event['id']}",
        "description": "\n".join(lines),
        "color": color(h.get("color")),
    }
    content = f"🏈 **PRIME TIME** · {a.get('displayName')} @ {h.get('displayName')} · kick-off {kick.strftime('%H:%M')} UK"
    png = render_png(preview_card_html(event, summary), 880, 548)
    if png:
        embed["image"] = {"url": "attachment://preview.png"}
    return discord(NFL_HOOK, embed, "NFL preview", content=content, files={"preview.png": png} if png else None)


def boxscore_id(summary, name):
    for team in summary.get("boxscore", {}).get("players", []):
        for cat in team.get("statistics", []):
            for a in cat.get("athletes", []):
                if a["athlete"].get("displayName") == name:
                    return str(a["athlete"].get("id", ""))
    o = league_owner("", name)
    return ""


def td_card_html(event, play, summary, td, tag, qtxt, clock, drive, wp_txt, down_text, fan):
    import html as H
    away, home = teams_of(event)
    abbr = play.get("team", {}).get("abbreviation", "")
    team = away if away["team"]["abbreviation"] == abbr else home
    ca, ch = team_cols(away, home)
    tc = ca if team is away else ch
    pid = boxscore_id(summary, td["scorer"])
    hs = nfl_headshot(pid) or team["team"].get("logo", "")
    yds = td["yds"]
    if td["kind"] == "pass":
        who = f"{H.escape(td['qb'])} ➜ {H.escape(td['scorer'])}"
        how = f"{yds}-yard touchdown pass"
    elif td["kind"] in TD_KINDS:
        who, how = H.escape(td["scorer"]), f"{yds}-yard {TD_KINDS[td['kind']][0]}"
    else:
        who, how = H.escape(td["scorer"]), "Touchdown"
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))

    def mrow(t, s):
        return (f"<div class='mrow{' on' if t is team else ''}'><img src='{H.escape(t['team'].get('logo', ''))}'>"
                f"<span>{t['team']['abbreviation']}</span><b>{s}</b></div>")

    pat = td["pat"]
    pat_txt = ((pat["mark"].replace("✅ ", "✓ ").replace("❌ ", "✗ ")) + (f" · {pat['who']}" if pat["who"] else "")) if pat else "—"
    tiles = [("SITUATION", down_text.split(" at ")[0] if down_text else "—"),
             ("DRIVE", (drive or "—").replace(" plays", " pl").replace(" yards", " yds").replace(", ", " · ")),
             ("WIN PROBABILITY", wp_txt or "—"),
             ("2-PT TRY" if pat and "2-pt" in pat["label"] else "EXTRA POINT", pat_txt)]
    tiles_html = "".join(f"<div class='tile'><div class='tl'>{l}</div><div class='tv2'>{H.escape(v)}</div></div>"
                         for l, v in tiles)
    fan_html = "".join(f"<span class='chip2'>{x}</span>" for x in fan)
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL · {qtxt} {clock}</div>"
            f"<div class='pill' style='border-color:{tc};color:{tc}'>{H.escape(tag)}</div></div>"
            f"<div class='tdhead'><img class='hs' src='{H.escape(hs)}'>"
            f"<div><div class='big' style='color:{tc}'>TOUCHDOWN</div><div class='who'>{who}</div>"
            f"<div class='how'>{H.escape(how)}</div></div>"
            f"<div class='mini'>{mrow(away, a_s)}{mrow(home, h_s)}</div></div>"
            f"<div class='tiles4'>{tiles_html}</div>"
            + (f"<div class='strip'><div class='col'><div class='ch'>{H.escape(CONFIG['nfl'].get('league_name', 'League').upper())}"
               f" · TONIGHT SO FAR</div><div class='chips'>{fan_html}</div></div></div>" if fan else ""))
    return _page(body, f"--a:{ca};--h:{ch}", 362 if fan else 292)


def ft_card_html(event, summary, ff, tag):
    import html as H
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    ca, ch = team_cols(away, home)
    ta, th = team_stats(summary, away["team"]["abbreviation"]), team_stats(summary, home["team"]["abbreviation"])
    ot = ot_tag(event).strip(" ()")
    la = [str(int(num(x.get("value", x.get("displayValue", 0))))) for x in away.get("linescores", [])]
    lh = [str(int(num(x.get("value", x.get("displayValue", 0))))) for x in home.get("linescores", [])]
    n = max(len(la), len(lh))
    hdr = "".join(f"<th>{q}</th>" for q in (["Q1", "Q2", "Q3", "Q4"] + ["OT"] * 3)[:n])
    rows = "".join(f"<tr><td class='n'>{t['team']['abbreviation']}</td>" + "".join(f"<td>{v}</td>" for v in ls)
                   + f"<td class='t'>{sc}</td></tr>" for t, ls, sc in ((away, la, a_s), (home, lh, h_s)))
    qtable = f"<table><tr><th></th>{hdr}<th>T</th></tr>{rows}</table>" if n else ""

    def tile(label, av, hv, lower_better=False, show=None):
        a, h = num(str(av).split("-")[0]), num(str(hv).split("-")[0])
        tot = (a + h) or 1
        better_a = (a < h) if lower_better else (a > h)
        sa, sh = show or (str(av), str(hv))
        return (f"<div class='tile'><div class='tl'>{label}</div><div class='tv'>"
                f"<span class='{'' if better_a or a == h else 'lo'}'>{H.escape(sa)}</span><i>·</i>"
                f"<span class='{'' if (not better_a) or a == h else 'lo'}'>{H.escape(sh)}</span></div>"
                f"<div class='bars'><div style='flex:{a / tot:.3f};background:{ca}'></div>"
                f"<div style='flex:{h / tot:.3f};background:{ch}'></div></div></div>")

    def mins(v):
        try:
            m, s = str(v).split(":")
            return int(m) * 60 + int(s)
        except ValueError:
            return 0
    tiles = ""
    if ta and th:
        tiles = "".join([
            tile("TOTAL YARDS", ta.get("totalYards", 0), th.get("totalYards", 0)),
            tile("PASSING", ta.get("netPassingYards", 0), th.get("netPassingYards", 0)),
            tile("RUSHING", ta.get("rushingYards", 0), th.get("rushingYards", 0)),
            tile("TURNOVERS", ta.get("turnovers", 0), th.get("turnovers", 0), lower_better=True),
            tile("3RD DOWN", ta.get("thirdDownEff", "0-0"), th.get("thirdDownEff", "0-0")),
            tile("POSSESSION", mins(ta.get("possessionTime")), mins(th.get("possessionTime")),
                 show=(ta.get("possessionTime", "—"), th.get("possessionTime", "—"))),
        ])
        tiles = f"<div class='tiles'>{tiles}</div>"

    pog_html = own_html = ""
    if ff:
        p = ff[0]
        pog_html = (f"<div class='pog'><img src='{H.escape(nfl_headshot(p.get('id')))}'><div>"
                    f"<div class='pl'>PLAYER OF THE GAME</div><div class='pn'>{H.escape(p['name'])}"
                    f"<span class='chip'>{fmt_pts(p['pts'])}</span></div>"
                    f"<div class='ps'>{H.escape(p['line'])}</div></div></div>")
        by_mgr = {}
        for x in ff:
            o = league_owner(x.get("id"), x["name"])
            if o and o["starting"]:
                by_mgr.setdefault(o["manager"], 0.0)
                by_mgr[o["manager"]] += x["pts"]
        if by_mgr:
            top = sorted(by_mgr.items(), key=lambda kv: -kv[1])[:3]
            own_html = (f"<div class='own'>{H.escape(CONFIG['nfl'].get('league_name', 'League'))} starters<br>"
                        + "<br>".join(f"<b>{H.escape(m)}</b> {fmt_pts(v)}" for m, v in top) + "</div>")

    w_col = ca if a_s > h_s else ch
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL</div>"
            f"<div class='pill'>{'FINAL · ' + ot if ot else 'FULL-TIME'}</div></div>"
            f"<div class='score'>{nfl_side(away, False)}<div><div class='nums'>"
            f"<div class='box{' w' if a_s > h_s else ''}'>{a_s}</div><div class='dot'></div>"
            f"<div class='box{' w' if h_s > a_s else ''}'>{h_s}</div></div>"
            f"<div class='tag' style='color:{w_col}'>{H.escape(tag)}</div></div>{nfl_side(home, True)}</div>"
            f"<div class='qs'>{qtable}</div>{tiles}<div class='bot'>{pog_html}{own_html}</div>")
    return _page(body, f"--a:{ca};--h:{ch}", 530 if tiles else 420)


def nfl_headline(event):
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    if a_s == h_s:
        return f"{away['team'].get('name')} and {home['team'].get('name')} tie {a_s}-{h_s}", "TIE GAME"
    w, l = (away, home) if a_s > h_s else (home, away)
    wn, ln, m = w["team"].get("name"), l["team"].get("name"), abs(a_s - h_s)
    if ot_tag(event):
        return f"{wn} beat the {ln} in overtime", "OVERTIME WIN"
    if m >= 21:
        return f"{wn} rout the {ln}", "ROUT"
    if m >= 10:
        return f"{wn} take care of the {ln}", "COMFORTABLE WIN"
    if m >= 4:
        return f"{wn} hold off the {ln}", f"{wn.upper()} HOLD ON"
    return f"{wn} edge the {ln} in a thriller", f"{m}-POINT THRILLER"


# ------------------------------------------------------ NFL slates (grouped)
# Several games kicking off together (e.g. Sunday 18:00 UK) get 3 posts in total:
# pre-game, half-time and full-time graphics, built around the Sleeper league.

_SLP = {}


def sleeper_players(sport="nfl"):
    if sport not in _SLP:
        _SLP[sport] = fetch_url(f"{SLEEPER}/players/{sport}", tries=2)
    return _SLP[sport]


def sl_team(espn_abbr):
    return SLEEPER_TEAM.get(espn_abbr, espn_abbr)


def short_name(full):
    parts = [w for w in (full or "").split(" ") if w.rstrip(".").lower() not in ("jr", "sr", "ii", "iii", "iv", "v")]
    return parts[-1] if parts else full


def league_live(sport="nfl", week=None):
    """Live Sleeper league data: week, rosters (manager, starters) and this week's matchups with points."""
    lid = str(CONFIG[sport].get("sleeper_league_id") or "").strip()
    if not lid:
        return None
    try:
        week = week or fetch_url(f"{SLEEPER}/state/{sport}").get("week")
        users = {u["user_id"]: u for u in fetch_url(f"{SLEEPER}/league/{lid}/users")}
        rosters = fetch_url(f"{SLEEPER}/league/{lid}/rosters")
        mus = fetch_url(f"{SLEEPER}/league/{lid}/matchups/{week}") if week else []
    except Exception as e:  # noqa: BLE001
        log(f"Sleeper live data unavailable: {e}")
        return None
    rmap = {}
    for r in rosters:
        u = users.get(r.get("owner_id")) or {}
        rmap[r["roster_id"]] = {"manager": u.get("display_name", "?"), "starters": [], "players": [], "pp": {},
                                "points": 0.0}
    for m in mus:
        if m.get("roster_id") in rmap:
            x = rmap[m["roster_id"]]
            x["starters"] = [p for p in (m.get("starters") or []) if p and p != "0"]
            x["players"] = m.get("players") or []
            x["pp"] = m.get("players_points") or {}
            x["points"] = float(m.get("points") or 0)
            x["matchup_id"] = m.get("matchup_id")
    pairs = {}
    for rid, x in rmap.items():
        if x.get("matchup_id"):
            pairs.setdefault(x["matchup_id"], []).append(rid)
    return {"week": week, "rosters": rmap, "pairs": [v for k, v in sorted(pairs.items()) if len(v) == 2]}


def slot_players(live, teams):
    """Every league-rostered player on the given NFL teams: (name, pos, team, pts, manager, starting)."""
    players = sleeper_players()
    out = []
    for rid, x in live["rosters"].items():
        for pid in x["players"]:
            p = players.get(pid) or {}
            if p.get("team") in teams:
                nm = p.get("full_name") or (f"{p.get('last_name', pid)} D/ST" if p.get("position") == "DEF" else pid)
                out.append({"pid": pid, "name": nm, "pos": p.get("position", ""),
                            "team": p.get("team"), "pts": float(x["pp"].get(pid) or 0), "manager": x["manager"],
                            "starting": pid in x["starters"], "rid": rid})
    return out


def nfl_slots(events):
    gap = CONFIG["nfl"].get("slot_minutes", 30) * 60
    slots, cur, first = [], [], None
    for e in sorted(events, key=lambda x: x["date"]):
        t = parse_time(e["date"])
        if cur and (t - first).total_seconds() > gap:
            slots.append(cur)
            cur = []
        if not cur:
            first = t
        cur.append(e)
    if cur:
        slots.append(cur)
    return slots


def game_status(e):
    st = e["status"]
    t = st["type"]
    if t.get("completed"):
        return "FINAL" + (" · OT" if ot_tag(e) else "")
    if t.get("state") == "pre":
        return parse_time(e["date"]).astimezone(UK).strftime("%H:%M")
    if t.get("name") == "STATUS_HALFTIME":
        return "HALF"
    q = st.get("period", 0)
    return f"{'Q' + str(q) if q <= 4 else 'OT'} {st.get('displayClock', '')}".strip()


SLOT_CSS = """
.rows{margin-top:12px;display:flex;flex-direction:column;gap:6px}
.grow{display:grid;grid-template-columns:318px 74px 1fr;align-items:center;gap:12px;background:rgba(255,255,255,.055);
 border-radius:10px;padding:7px 12px;height:52px}
.gm{display:grid;grid-template-columns:30px 44px 1fr 18px 1fr 44px 30px;align-items:center;gap:4px;font-variant-numeric:tabular-nums}
.gm img{width:30px;height:30px;object-fit:contain}
.gm .ab{font-weight:800;font-size:16px}.gm .ab.r{text-align:right}
.gm .s{font-size:24px;font-weight:800;text-align:center}.gm .s.lo{color:#8b95a8}
.gm .d{color:#5d6679;text-align:center}
.gm .rc{font-size:12.5px;color:#9aa3b5;text-align:center}
.st{font-size:11px;font-weight:700;letter-spacing:1px;text-align:center;border-radius:6px;padding:4px 0;background:rgba(255,255,255,.08);color:#e6e9ef}
.st.live{background:rgba(229,72,77,.22);color:#ffb4b6}.st.fin{background:rgba(255,255,255,.14)}
.info{font-size:15px;color:#cfd5e1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.info b{color:#fff}.info .m{color:#9aa3b5}
.lg2{margin-top:12px;background:rgba(255,255,255,.055);border-radius:10px;padding:9px 12px}
.mus{display:grid;grid-template-columns:repeat(2,1fr);gap:6px 28px;margin-top:7px}
.mu{display:grid;grid-template-columns:1fr auto auto 12px auto auto 1fr;gap:6px;align-items:center;font-size:14px;font-variant-numeric:tabular-nums}
.mu .c{font-size:10.5px;font-weight:700;color:#cfd5e1;background:rgba(255,255,255,.1);border-radius:5px;padding:1px 5px}
.mu .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#cfd5e1}.mu .n.r{text-align:right}
.mu .p{font-weight:800}.mu .p.lo{color:#8b95a8;font-weight:600}.mu i{color:#5d6679;font-style:normal;text-align:center}
.mu .w{color:#fff;font-weight:700}
.foot{display:flex;gap:10px;margin-top:10px}
.foot .col{flex:1;font-size:14px;color:#cfd5e1}.foot b{color:#fff}
"""


def slot_card_html(mode, slot, rows_info, live, foot=None, title=""):
    """mode: PRE-GAME / HALF-TIME / FULL-TIME. rows_info: per event a short HTML fantasy line."""
    import html as H
    rows = []
    for e, info in zip(slot, rows_info):
        away, home = teams_of(e)
        a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
        stt = game_status(e)
        cls = "fin" if stt.startswith("FINAL") else ("" if e["status"]["type"].get("state") == "pre" else "live")
        if mode == "PRE-GAME":
            mid = (f"<span class='rc'>{H.escape(record_of(away) or '')}</span><span class='d'>@</span>"
                   f"<span class='rc'>{H.escape(record_of(home) or '')}</span>")
        else:
            mid = (f"<span class='s{' lo' if a_s < h_s else ''}'>{a_s}</span><span class='d'>–</span>"
                   f"<span class='s{' lo' if h_s < a_s else ''}'>{h_s}</span>")
        rows.append(f"<div class='grow'><div class='gm'><img src='{H.escape(away['team'].get('logo', ''))}'>"
                    f"<span class='ab'>{away['team']['abbreviation']}</span>{mid}"
                    f"<span class='ab r'>{home['team']['abbreviation']}</span>"
                    f"<img src='{H.escape(home['team'].get('logo', ''))}'></div>"
                    f"<div class='st {cls}'>{H.escape(stt)}</div><div class='info'>{info}</div></div>")

    lg = ""
    if live and live["pairs"]:
        league = CONFIG["nfl"].get("league_name", "League")
        teams = {sl_team(t["team"]["abbreviation"]) for e in slot for t in teams_of(e)}
        players = sleeper_players()
        cells = []
        for ra, rb in live["pairs"]:
            A, B = live["rosters"][ra], live["rosters"][rb]
            if mode == "PRE-GAME":
                la = sum(1 for p in A["starters"] if (players.get(p) or {}).get("team") in teams)
                lb = sum(1 for p in B["starters"] if (players.get(p) or {}).get("team") in teams)
                cells.append(f"<div class='mu'><span class='n'>{H.escape(A['manager'])}</span>"
                             f"<span class='c'>{la}</span><span class='p'>{A['points']:.1f}</span><i>·</i>"
                             f"<span class='p'>{B['points']:.1f}</span><span class='c'>{lb}</span>"
                             f"<span class='n r'>{H.escape(B['manager'])}</span></div>")
            else:
                aw = A["points"] >= B["points"]
                cells.append(f"<div class='mu'><span class='n{' w' if aw else ''}'>{H.escape(A['manager'])}</span><span></span>"
                             f"<span class='p{'' if aw else ' lo'}'>{A['points']:.1f}</span><i>·</i>"
                             f"<span class='p{' lo' if aw else ''}'>{B['points']:.1f}</span><span></span>"
                             f"<span class='n r{'' if aw else ' w'}'>{H.escape(B['manager'])}</span></div>")
        head = (f"{H.escape(league.upper())} · WEEK {live['week']} MATCHUPS"
                + (" · LIVE" if mode == "HALF-TIME" else (" · POINTS SO FAR  ·  BADGE = STARTERS IN THIS SLATE" if mode == "PRE-GAME" else "")))
        lg = f"<div class='lg2'><div class='ch'>{head}</div><div class='mus'>{''.join(cells)}</div></div>"

    foot_html = ""
    if foot:
        foot_html = "<div class='foot'>" + "".join(f"<div class='col'><div class='ch'>{H.escape(k)}</div>{v}</div>"
                                                   for k, v in foot) + "</div>"
    n = len(slot)
    pairs = len(live["pairs"]) if live else 0
    mu_rows = (pairs + 1) // 2
    height = 70 + n * 58 + (44 + mu_rows * 26 if lg else 0) + (80 if foot else 0) - 4
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL{' · WEEK ' + str(live['week']) if live else ''}"
            f" · {H.escape(title.upper())}</div><div class='pill'>{mode}</div></div>"
            f"<div class='rows'>{''.join(rows)}</div>{lg}{foot_html}")
    html_text = (f"<!doctype html><html><head><meta charset='utf-8'>"
                 f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800;900&display=swap' rel='stylesheet'>"
                 f"<style>{NFL_CSS}{SLOT_CSS}body,.card{{height:{height}px}}</style></head><body>"
                 f"<div class='card' style='--a:#5b7bd5;--h:#d50a0a'><div class='bg'></div>"
                 f"<div class='in'>{body}</div></div></body></html>")
    return html_text, height


def slot_title(slot):
    k = parse_time(slot[0]["date"]).astimezone(UK)
    return f"{k.strftime('%A')} {k.strftime('%H:%M')} slate"


def best_line(e, live, summary=None):
    """One fantasy line for a game: best league-rostered player so far (Sleeper points), with owner."""
    import html as H
    away, home = teams_of(e)
    teams = {sl_team(away["team"]["abbreviation"]), sl_team(home["team"]["abbreviation"])}
    if live:
        ps = sorted(slot_players(live, teams), key=lambda p: -p["pts"])
        if ps and ps[0]["pts"] > 0:
            p = ps[0]
            return (f"⭐ <b>{H.escape(p['name'])}</b> {p['pts']:.1f} <span class='m'>· {H.escape(p['manager'])}"
                    f"{'' if p['starting'] else ' (bench)'}</span>")
    if summary:
        ff = nfl_fantasy(summary)
        if ff:
            p = ff[0]
            o = league_owner(p.get("id"), p["name"])
            return (f"⭐ <b>{H.escape(p['name'])}</b> {fmt_pts(p['pts'])} <span class='m'>· "
                    f"{H.escape(o['manager']) if o else 'unrostered'}</span>")
    return "<span class='m'>No fantasy points yet</span>"


def pre_line(e, live):
    import html as H
    away, home = teams_of(e)
    teams = {sl_team(away["team"]["abbreviation"]), sl_team(home["team"]["abbreviation"])}
    odds = ""
    try:
        o = (e["competitions"][0].get("odds") or [{}])[0]
        odds = o.get("details") or ""
    except (IndexError, AttributeError):
        pass
    starters = [p for p in slot_players(live, teams) if p["starting"]] if live else []
    starters.sort(key=lambda p: p["pos"] == "DEF")
    names = ", ".join(H.escape(p["name"] if p["pos"] == "DEF" else short_name(p["name"])) for p in starters[:4]) + ("…" if len(starters) > 4 else "")
    s = f"<span class='m'>{H.escape(odds)}</span>" if odds else ""
    if starters:
        s += f"{' · ' if s else ''}<b>{len(starters)}</b> <span class='m'>starters:</span> {names}"
    return s or "<span class='m'>No league starters</span>"


def post_slot(mode, slot, live):
    title = slot_title(slot)
    if mode == "PRE-GAME":
        infos = [pre_line(e, live) for e in slot]
        foot = None
    else:
        infos, sums = [], []
        for e in slot:
            try:
                summ = fetch(f"football/nfl/summary?event={e['id']}")
            except Exception:  # noqa: BLE001
                summ = None
            sums.append(summ)
            infos.append(best_line(e, live, summ))
        foot = None
        if mode == "FULL-TIME" and live:
            import html as H
            teams = {sl_team(t["team"]["abbreviation"]) for e in slot for t in teams_of(e)}
            ps = slot_players(live, teams)
            mvp = max(ps, key=lambda p: p["pts"], default=None)
            bench = max((p for p in ps if not p["starting"]), key=lambda p: p["pts"], default=None)
            foot = []
            if mvp and mvp["pts"] > 0:
                foot.append(("SLATE MVP", f"<b>{H.escape(mvp['name'])}</b> {mvp['pts']:.1f} fpts · {H.escape(mvp['manager'])}"))
            if bench and bench["pts"] >= 10:
                foot.append(("BENCH BLUNDER", f"<b>{H.escape(bench['name'])}</b> {bench['pts']:.1f} fpts left on "
                                              f"{H.escape(bench['manager'])}'s bench"))
            foot = foot or None
    html_text, height = slot_card_html(mode, slot, infos, live, foot, title)
    png = render_png(html_text, 880, height)
    emoji = {"PRE-GAME": "🏈", "HALF-TIME": "⏸️", "FULL-TIME": "🏁"}[mode]
    content = f"{emoji} **{mode}** · {title} · {len(slot)} games"
    lines = []
    if live and live["pairs"] and mode != "PRE-GAME":
        diffs = []
        for ra, rb in live["pairs"]:
            A, B = live["rosters"][ra], live["rosters"][rb]
            diffs.append((abs(A["points"] - B["points"]), A, B))
        close = min(diffs, key=lambda d: d[0])
        lines.append(f"🔥 Closest matchup: **{close[1]['manager']}** {close[1]['points']:.1f} – "
                     f"{close[2]['points']:.1f} **{close[2]['manager']}**")
    if not png:
        for e in slot:
            away, home = teams_of(e)
            lines.append(f"{away['team']['abbreviation']} {away.get('score', '')} – {home.get('score', '')} "
                         f"{home['team']['abbreviation']} ({game_status(e)})")
    embed = {"color": 0x013369, "author": {"name": f"NFL · {title}", "icon_url": NFL_LOGO}}
    if lines:
        embed["description"] = "\n".join(lines)
    if png:
        embed["image"] = {"url": "attachment://slate.png"}
    return discord(NFL_HOOK, embed, f"NFL slate {mode}", content=content, files={"slate.png": png} if png else None)


def passed_half(e):
    t = e["status"]["type"]
    return t.get("state") == "post" or t.get("name") == "STATUS_HALFTIME" or e["status"].get("period", 0) >= 3


def nfl_slot(slot, st, force=False):
    """Handle a group of games kicking off together. Returns True when fast polling is needed."""
    key = slot[0]["date"]
    s = st.setdefault("nfl_slots", {}).setdefault(key, {"date": key[:10]})
    for e in slot:                      # keep per-game state so single-game logic never double-posts
        st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})["final"] = True
    age = (now_utc() - parse_time(key)).total_seconds() / 3600
    if age > STALE_HOURS and not force:
        s.update(pre=True, half=True, ft=True)
        return False
    states = [e["status"]["type"]["state"] for e in slot]
    live = None

    def L():
        nonlocal live
        if live is None:
            live = league_live("nfl") or {}
        return live or None

    if not s.get("pre"):
        if force or (-0.8 <= age < -0.03 and all(x == "pre" for x in states)):
            if post_slot("PRE-GAME", slot, L()):
                s["pre"] = True
        elif age >= -0.03:
            s["pre"] = True
    done = all(e["status"]["type"].get("completed") or e["status"]["type"]["state"] == "post" for e in slot)
    if not s.get("half") and not done and all(x != "pre" for x in states) and all(passed_half(e) for e in slot):
        if post_slot("HALF-TIME", slot, L()):
            s["half"] = True
    if not s.get("ft") and done:
        if post_slot("FULL-TIME", slot, L()):
            s.update(ft=True, half=True)
    return any(x == "in" for x in states) or -0.8 <= age < 0.2


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
