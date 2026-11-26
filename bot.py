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
            atts = []
            if not embeds:
                import tempfile as _tf2
                for name, data in (files or {}).items():
                    fp = Path(_tf2.mkdtemp()) / name
                    fp.write_bytes(data)
                    atts.append(fp.as_uri())
            CAPTURE.append({"content": content, "embeds": ems, "attachments": atts})
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
    who = CONFIG.get("bot") or {}
    payload = {"username": who.get("name", "Donald Claude"), "embeds": embeds[:10]}
    if who.get("avatar_url"):
        payload["avatar_url"] = who["avatar_url"]
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
                    if p.get("position") == "DEF":         # team defence: keyed 'def:LAR'
                        idx[f"def:{pid}"] = {"manager": u.get("display_name", "?"), "starting": pid in starters,
                                             "name": f"{p.get('last_name', pid)} D/ST", "pos": "DEF", "team": pid,
                                             "team_name": (u.get("metadata") or {}).get("team_name") or ""}
                        continue
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
                if stl >= 1: parts.append(f"{int(stl)} stl")
                if blk >= 1: parts.append(f"{int(blk)} blk")
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
        st = f"{r['pts']} PTS · {r['reb']} REB · {r['ast']} AST" + (f" · {r['stl']} STL" if r["stl"] >= 1 else "") + \
             (f" · {r['blk']} BLK" if r["blk"] >= 1 else "")
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


# ------------------------------------------------------ weekly league recap
# One graphic per finished Sleeper gameweek (NFL: Tuesday 13:00 UK, NBA: Monday 13:00 UK):
# results, awards, standings and simulated playoff / title odds.

import random as _random

RECAP_CSS = """
.sec{font-size:10.5px;letter-spacing:1.6px;color:#9aa3b5;font-weight:600;margin:12px 0 0}
.res{display:grid;grid-template-columns:1fr 1fr;gap:6px 10px;margin-top:6px}
.rr{display:grid;grid-template-columns:1fr auto 14px auto 1fr;align-items:center;gap:8px;background:rgba(255,255,255,.055);
 border-radius:10px;padding:8px 12px;font-variant-numeric:tabular-nums}
.rr .n{font-size:15px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.rr .n.r{text-align:right}
.rr .n.lo{color:#8b95a8;font-weight:600}
.rr .p{font-size:19px;font-weight:800}.rr .p.lo{color:#8b95a8;font-weight:700}.rr i{color:#5d6679;font-style:normal;text-align:center}
.aw{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:6px}
.at{background:rgba(255,255,255,.055);border-radius:10px;padding:8px 12px;height:72px}
.at .k{font-size:10.5px;letter-spacing:1.4px;color:#9aa3b5;font-weight:600}
.at .v{font-size:16px;font-weight:800;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.at .x{font-size:12.5px;color:#cfd5e1;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tbl{margin-top:6px;background:rgba(255,255,255,.055);border-radius:10px;padding:6px 12px}
.tr{display:grid;grid-template-columns:26px 1fr 62px 70px 150px 150px;align-items:center;gap:10px;height:27px;
 font-size:14px;font-variant-numeric:tabular-nums}
.tr.h{height:22px;font-size:10.5px;letter-spacing:1.3px;color:#9aa3b5;font-weight:600}
.tr.cut{border-bottom:1.5px dashed rgba(255,255,255,.25)}
.tr .rk{color:#9aa3b5;font-weight:700}.tr .m{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tr .o{display:grid;grid-template-columns:40px 1fr;align-items:center;gap:8px;font-weight:800}
.bar{height:6px;border-radius:3px;background:rgba(255,255,255,.08);overflow:hidden}.bar div{height:6px;border-radius:3px}
.gw{margin-top:10px;display:flex;gap:10px}
.gw .col{flex:1;font-size:13.5px;color:#cfd5e1;background:rgba(255,255,255,.055);border-radius:10px;padding:8px 12px}
.gw b{color:#fff}
"""


def league_settings(sport):
    lid = str(CONFIG[sport].get("sleeper_league_id") or "").strip()
    return fetch_url(f"{SLEEPER}/league/{lid}") if lid else None


def season_history(sport, upto):
    """Weekly points and results for weeks 1..upto from Sleeper matchups."""
    lid = str(CONFIG[sport].get("sleeper_league_id")).strip()
    scores, rec = {}, {}
    for wk in range(1, upto + 1):
        try:
            ms = fetch_url(f"{SLEEPER}/league/{lid}/matchups/{wk}", tries=2)
        except Exception:  # noqa: BLE001
            continue
        by = {}
        for m in ms:
            rid = m["roster_id"]
            pts = float(m.get("points") or 0)
            scores.setdefault(rid, []).append(pts)
            rec.setdefault(rid, {"w": 0, "l": 0, "t": 0, "pf": 0.0})
            rec[rid]["pf"] += pts
            if m.get("matchup_id"):
                by.setdefault(m["matchup_id"], []).append((rid, pts))
        for pair in by.values():
            if len(pair) != 2:
                continue
            (a, pa), (b, pb) = pair
            if pa > pb:
                rec[a]["w"] += 1; rec[b]["l"] += 1  # noqa: E702
            elif pb > pa:
                rec[b]["w"] += 1; rec[a]["l"] += 1  # noqa: E702
            else:
                rec[a]["t"] += 1; rec[b]["t"] += 1  # noqa: E702
    return scores, rec


def simulate_odds(sport, settings, scores, rec, upto, sims=4000):
    """Monte Carlo: rest of the regular season (real schedule) + playoffs. Returns {rid: (playoff%, title%)}."""
    lid = str(CONFIG[sport].get("sleeper_league_id")).strip()
    s = settings.get("settings", {})
    po_start = int(s.get("playoff_week_start") or 15)
    n_po = int(s.get("playoff_teams") or 6)
    rids = list(rec.keys())
    allpts = [p for v in scores.values() for p in v if p > 0]
    lg_mean = sum(allpts) / len(allpts) if allpts else 100
    sd = (sum((p - lg_mean) ** 2 for p in allpts) / max(1, len(allpts) - 1)) ** 0.5 if allpts else 20
    mean = {}
    for r in rids:
        v = [p for p in scores.get(r, []) if p > 0]
        mean[r] = (sum(v) + 2 * lg_mean) / (len(v) + 2)
    sd = max(sd * 0.85, 1.0)
    sched = []
    for wk in range(upto + 1, po_start):
        try:
            ms = fetch_url(f"{SLEEPER}/league/{lid}/matchups/{wk}", tries=2)
        except Exception:  # noqa: BLE001
            ms = []
        by = {}
        for m in ms:
            if m.get("matchup_id"):
                by.setdefault(m["matchup_id"], []).append(m["roster_id"])
        pairs = [p for p in by.values() if len(p) == 2]
        if not pairs:                               # schedule not published: random pairings
            pool = rids[:]
            _random.shuffle(pool)
            pairs = [pool[i:i + 2] for i in range(0, len(pool) - 1, 2)]
        sched.append(pairs)
    rnd = _random.Random(42)
    po_count = {r: 0 for r in rids}
    title = {r: 0 for r in rids}

    def game(a, b):
        return a if rnd.gauss(mean[a], sd) >= rnd.gauss(mean[b], sd) else b

    for _ in range(sims):
        w = {r: rec[r]["w"] + 0.5 * rec[r]["t"] for r in rids}
        pf = {r: rec[r]["pf"] for r in rids}
        for pairs in sched:
            for a, b in pairs:
                sa, sb = rnd.gauss(mean[a], sd), rnd.gauss(mean[b], sd)
                pf[a] += sa
                pf[b] += sb
                w[a if sa >= sb else b] += 1
        seeds = sorted(rids, key=lambda r: (-w[r], -pf[r]))[:n_po]
        for r in seeds:
            po_count[r] += 1
        if n_po == 6:
            q1, q2 = game(seeds[2], seeds[5]), game(seeds[3], seeds[4])
            s1 = game(seeds[0], q2 if seeds.index(q2) > seeds.index(q1) else q1)
            s2 = game(seeds[1], q1 if seeds.index(q2) > seeds.index(q1) else q2)
            champ = game(s1, s2)
        else:                                        # generic single-elimination by seed
            alive = seeds[:]
            while len(alive) > 1:
                alive = [game(alive[i], alive[-1 - i]) for i in range(len(alive) // 2)] + \
                        ([alive[len(alive) // 2]] if len(alive) % 2 else [])
            champ = alive[0]
        title[champ] += 1
    return {r: (100 * po_count[r] / sims, 100 * title[r] / sims) for r in rids}, po_start, n_po


def recap_card_html(sport, week, live, settings):
    import html as H
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    league = CONFIG[sport].get("league_name", "League")
    players = sleeper_players(sport)
    names = {rid: x["manager"] for rid, x in live["rosters"].items()}

    games, starters, bench = [], [], []
    for ra, rb in live["pairs"]:
        A, B = live["rosters"][ra], live["rosters"][rb]
        games.append((A, B) if A["points"] >= B["points"] else (B, A))
    for rid, x in live["rosters"].items():
        for pid in x["players"]:
            p = players.get(pid) or {}
            nm = p.get("full_name") or (f"{p.get('last_name', pid)} D/ST" if p.get("position") == "DEF" else pid)
            row = {"name": nm, "pts": float(x["pp"].get(pid) or 0), "manager": x["manager"]}
            (starters if pid in x["starters"] else bench).append(row)

    scores, rec = season_history(sport, week)
    odds, po_start, n_po = simulate_odds(sport, settings, scores, rec, week)
    table = sorted(rec.keys(), key=lambda r: (-(rec[r]["w"] + 0.5 * rec[r]["t"]), -rec[r]["pf"]))

    # ---- results + awards
    rows = "".join(
        f"<div class='rr'><span class='n'>{H.escape(w['manager'])}</span><span class='p'>{w['points']:.1f}</span><i>·</i>"
        f"<span class='p lo'>{l['points']:.1f}</span><span class='n r lo'>{H.escape(l['manager'])}</span></div>"
        for w, l in games)
    allr = [x for x in live["rosters"].values() if x.get("matchup_id")]
    top = max(allr, key=lambda x: x["points"])
    blow = max(games, key=lambda g: g[0]["points"] - g[1]["points"])
    close = min(games, key=lambda g: g[0]["points"] - g[1]["points"])
    pow_ = max(starters, key=lambda p: p["pts"], default=None)
    bb = max(bench, key=lambda p: p["pts"], default=None)
    unlucky = max((l for _, l in games), key=lambda x: x["points"])
    fav = max(odds, key=lambda r: odds[r][1])
    tiles = [
        ("🏆 TOP SCORE", top["manager"], f"{top['points']:.1f} pts"),
        ("⭐ PLAYER OF THE WEEK", pow_["name"] if pow_ else "—",
         f"{pow_['pts']:.1f} pts · {pow_['manager']}" if pow_ else ""),
        ("💥 BIGGEST WIN", blow[0]["manager"], f"by {blow[0]['points'] - blow[1]['points']:.1f} over {blow[1]['manager']}"),
        ("😬 CLOSEST FINISH", close[0]["manager"], f"by {close[0]['points'] - close[1]['points']:.1f} over {close[1]['manager']}"),
        ("🪑 BENCH BLUNDER", bb["name"] if bb else "—", f"{bb['pts']:.1f} pts on {bb['manager']}'s bench" if bb else ""),
        ("🥶 UNLUCKIEST LOSS", unlucky["manager"], f"{unlucky['points']:.1f} pts and still lost"),
    ]
    tiles_html = "".join(f"<div class='at'><div class='k'>{H.escape(k)}</div><div class='v'>{H.escape(v)}</div>"
                         f"<div class='x'>{H.escape(x)}</div></div>" for k, v, x in tiles)

    # ---- standings + odds
    def bar(pct, col):
        return f"<div class='bar'><div style='width:{max(pct, 1.5):.1f}%;background:{col}'></div></div>"
    trs = ["<div class='tr h'><span>#</span><span>MANAGER</span><span>W-L</span><span>PF</span>"
           "<span>PLAYOFFS</span><span>TITLE</span></div>"]
    for i, r in enumerate(table, 1):
        x = rec[r]
        wl = f"{x['w']}-{x['l']}" + (f"-{x['t']}" if x["t"] else "")
        po, ti = odds[r]
        trs.append(f"<div class='tr{' cut' if i == n_po else ''}'><span class='rk'>{i}</span>"
                   f"<span class='m'>{H.escape(names.get(r, '?'))}</span><span>{wl}</span><span>{x['pf']:.0f}</span>"
                   f"<span class='o'><span>{po:.0f}%</span>{bar(po, 'linear-gradient(90deg,#d97757,#f2a77f)')}</span>"
                   f"<span class='o'><span>{ti:.0f}%</span>{bar(ti * 2.5, 'linear-gradient(90deg,#e8b13c,#ffe08a)')}</span></div>")

    # ---- next week + context
    nxt = ""
    try:
        lid = str(CONFIG[sport].get("sleeper_league_id")).strip()
        ms = fetch_url(f"{SLEEPER}/league/{lid}/matchups/{week + 1}", tries=2) if week + 1 < po_start else []
        by = {}
        for m in ms:
            if m.get("matchup_id"):
                by.setdefault(m["matchup_id"], []).append(m["roster_id"])
        pairs = [p for p in by.values() if len(p) == 2]
        if pairs:
            a, b = max(pairs, key=lambda p: min(odds[p[0]][0], odds[p[1]][0]) + 0.3 * (odds[p[0]][0] + odds[p[1]][0]))
            wl = lambda r: f"{rec[r]['w']}-{rec[r]['l']}"  # noqa: E731
            nxt = (f"<b>{H.escape(names[a])}</b> ({wl(a)}) vs <b>{H.escape(names[b])}</b> ({wl(b)})"
                   f" — playoff odds {odds[a][0]:.0f}% & {odds[b][0]:.0f}%")
    except Exception as e:  # noqa: BLE001
        log(f"recap next week unavailable: {e}")
    left = max(0, po_start - week - 1)
    ctx = (f"Playoffs start week <b>{po_start}</b> · top <b>{n_po}</b> of {len(table)} qualify · "
           f"<b>{left}</b> regular-season week{'s' if left != 1 else ''} left") if left else \
          f"Playoffs start week <b>{po_start}</b> · top <b>{n_po}</b> qualify"
    gw = (f"<div class='gw'>{'<div class=' + chr(39) + 'col' + chr(39) + '><div class=' + chr(39) + 'ch' + chr(39) + '>GAME TO WATCH NEXT WEEK</div>' + nxt + '</div>' if nxt else ''}"
          f"<div class='col'><div class='ch'>THE RACE</div>{ctx}<br>Title favourite: <b>{H.escape(names[fav])}</b> "
          f"{odds[fav][1]:.0f}%</div></div>")

    res_rows = (len(games) + 1) // 2
    height = 64 + 22 + res_rows * 49 + 22 + 2 * 80 + 22 + 12 + (len(table) + 1) * 27 + 74 + 30
    body = (f"<div class='top'><div class='lg'><img src='{logo}'>{H.escape(league.upper())} · WEEK {week}</div>"
            f"<div class='pill'>WEEKLY RECAP</div></div>"
            f"<div class='sec'>RESULTS</div><div class='res'>{rows}</div>"
            f"<div class='sec'>AWARDS</div><div class='aw'>{tiles_html}</div>"
            f"<div class='sec'>TITLE RACE · SIMULATED {4000:,} TIMES</div><div class='tbl'>{''.join(trs)}</div>{gw}")
    html_text = (f"<!doctype html><html><head><meta charset='utf-8'>"
                 f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800;900&display=swap' rel='stylesheet'>"
                 f"<style>{NFL_CSS}{RECAP_CSS}body,.card{{height:{height}px}}</style></head><body>"
                 f"<div class='card' style='--a:#1faa59;--h:#5b7bd5'><div class='bg'></div>"
                 f"<div class='in'>{body}</div></div></body></html>")
    lines = [f"🏆 **{top['manager']}** top-scored with {top['points']:.1f}",
             f"😬 Closest: **{close[0]['manager']}** beat {close[1]['manager']} by {close[0]['points'] - close[1]['points']:.1f}",
             f"📈 Title favourite: **{names[fav]}** {odds[fav][1]:.0f}%"]
    return html_text, height, lines


def post_weekly_recap(sport, week, live, settings):
    html_text, height, lines = recap_card_html(sport, week, live, settings)
    league = CONFIG[sport].get("league_name", "League")
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    hook = NFL_HOOK if sport == "nfl" else NBA_HOOK
    html_text = html_text.replace("body,.card{height:", "body,.card{height:auto!important;min-height:").replace(
        "</body>", "<script>addEventListener('load',()=>document.body.setAttribute('data-h',"
                   "Math.ceil(document.querySelector('.card').getBoundingClientRect().height)))</script></body>")
    embed = {"author": {"name": f"{league} · Week {week}", "icon_url": logo}, "color": 0x1FAA59,
             "img_title": f"Week {week} recap", "img_desc": "\n".join(lines)}
    emoji = "🏈" if sport == "nfl" else "🏀"
    return _post_card(hook, html_text, f"📊 **WEEK {week} RECAP** · {emoji} {league}", embed, "recap.png")


def maybe_weekly_recap(st, sport, force_week=None):
    """From the recap day/time (UK) until the end of the next day: recap the last week Sleeper has scored."""
    cfg = CONFIG[sport].get("weekly_recap") or {}
    if not cfg.get("enabled", True) or not CONFIG[sport].get("sleeper_league_id"):
        return
    now = datetime.now(UK)
    if not force_week:
        late = (now.weekday() - WEEKDAYS.index(cfg.get("weekday", "Tuesday"))) % 7
        if late > 1 or (late == 0 and now.hour < cfg.get("hour_uk", 13)):
            return
    try:
        settings = league_settings(sport)
        week = force_week or int(settings["settings"].get("last_scored_leg") or 0)
    except Exception as e:  # noqa: BLE001
        log(f"{sport} recap: league unavailable: {e}")
        return
    if not week:
        return
    key = f"{sport}-{settings.get('season')}-{week}"
    done = st.setdefault("recaps", [])
    if key in done and not force_week:
        return
    live = league_live(sport, week=week)
    if not live or not live["pairs"]:
        return
    if post_weekly_recap(sport, week, live, settings):
        st["recaps"] = (done + [key])[-40:]


# ------------------------------------------------ NBA fantasy-first posts (concept)

FAN_CSS = """
.sm{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:700;color:#cfd5e1;font-variant-numeric:tabular-nums}
.sm img{width:24px;height:24px;object-fit:contain}.sm .lo{color:#8b95a8}
.hd{display:flex;justify-content:space-between;align-items:flex-end;margin-top:14px}
.big2{font-size:26px;font-weight:900;letter-spacing:.3px}
.hd .s2{font-size:13px;color:#9aa3b5;font-weight:600;letter-spacing:1.2px}
.pl2{margin-top:10px;display:flex;flex-direction:column;gap:6px}
.pr2{display:grid;grid-template-columns:44px 1fr 74px 168px 176px;align-items:center;gap:12px;background:rgba(255,255,255,.055);
 border-radius:10px;padding:6px 12px;height:56px}
.pr2.lead{background:rgba(255,255,255,.12)}
.more{font-size:13px;color:#9aa3b5;margin-top:8px}.more b{color:#e6e9ef}
.pr2 img{width:44px;height:44px;border-radius:50%;object-fit:cover;object-position:top;background:#1d2436}
.pr2 .nm2{font-size:16px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pr2 .nm2 span{font-size:12px;color:#9aa3b5;font-weight:600;margin-left:4px}
.pr2 .ln{font-size:12.5px;color:#cfd5e1;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fp{font-size:20px;font-weight:900;text-align:center;border-radius:8px;padding:5px 0;background:rgba(255,255,255,.1);font-variant-numeric:tabular-nums}
.fp.el{background:#1faa59}.fp small{display:block;font-size:9.5px;letter-spacing:1px;font-weight:700;opacity:.8}
.ow{font-size:14px;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bd{display:inline-block;font-size:9.5px;letter-spacing:1px;font-weight:800;border-radius:4px;padding:2px 5px;margin-top:3px}
.bd.st{background:rgba(47,191,113,.2);color:#7ee2a8}.bd.bn{background:rgba(255,255,255,.1);color:#aab3c5}
.bd.fa{background:rgba(245,197,66,.2);color:#f5c542}
.mt{font-size:12.5px;color:#cfd5e1;text-align:right;font-variant-numeric:tabular-nums;line-height:1.35}
.mt b{font-size:15px}.mt .w{color:#7ee2a8}.mt .l{color:#ff9a9e}
.gl{display:grid;grid-template-columns:250px 66px 1fr;align-items:center;gap:12px;background:rgba(255,255,255,.055);
 border-radius:10px;padding:6px 12px;height:50px}
.gl .sc2{display:grid;grid-template-columns:22px 34px 30px 10px 30px 34px 22px;align-items:center;gap:4px;font-size:13px;
 font-weight:700;color:#aab3c5;font-variant-numeric:tabular-nums}
.gl .sc2 img{width:22px;height:22px;object-fit:contain}.gl .sc2 b{color:#fff}.gl .sc2 .r{text-align:right}
.gl .f1{font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#cfd5e1}
.gl .f1 b{color:#fff}.gl .f1 .chipx{background:#1faa59;color:#fff;border-radius:5px;padding:1px 6px;font-weight:800;margin:0 4px}
.gl .f1 .m{color:#9aa3b5}
"""


def nba_matchup_lookup(live):
    out = {}
    if not live:
        return out
    for ra, rb in live["pairs"]:
        A, B = live["rosters"][ra], live["rosters"][rb]
        out[A["manager"]] = (A["points"], B["manager"], B["points"])
        out[B["manager"]] = (B["points"], A["manager"], A["points"])
    return out


def nba_league_rows(summary):
    """Every player in the game with fpts, owner (or free agent) and headshot."""
    rows = []
    for p in nba_players(summary):
        o = league_owner("", p["name"], "nba")
        rows.append({**p, "owner": o["manager"] if o else None, "starting": bool(o and o["starting"]),
                     "hs": headshot(summary, p["name"])})
    return rows


def _card(css_vars, body, height, extra_css=""):
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800;900&display=swap' rel='stylesheet'>"
            f"<style>{NFL_CSS}{SLOT_CSS}{FAN_CSS}{extra_css}body,.card{{height:{height}px}}</style></head><body>"
            f"<div class='card' style='{css_vars}'><div class='bg'></div><div class='in'>{body}</div></div></body></html>")


def nba_game_fantasy_html(event, summary, live, max_rows=7):
    import html as H
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    league = CONFIG["nba"].get("league_name", "League")
    rows = nba_league_rows(summary)
    owned = [r for r in rows if r["owner"]][:max_rows]
    fa = next((r for r in rows if not r["owner"]), None)
    mu = nba_matchup_lookup(live)
    status = "FULL-TIME" if event["status"]["type"].get("completed") else game_status(event)

    def score_strip():
        return (f"<div class='sm'><img src='{H.escape(away['team'].get('logo', ''))}'>{away['team']['abbreviation']}"
                f" <span class='{'lo' if a_s < h_s else ''}'>{a_s}</span> – <span class='{'lo' if h_s < a_s else ''}'>{h_s}</span> "
                f"{home['team']['abbreviation']}<img src='{H.escape(home['team'].get('logo', ''))}'></div>")

    prs = []
    for i, r in enumerate(owned):
        raw = r["raw"]
        line = f"{raw['pts']} pts · {raw['reb']} reb · {raw['ast']} ast" + \
               (f" · {raw['stl']} stl" if raw["stl"] >= 1 else "") + (f" · {raw['blk']} blk" if raw["blk"] >= 1 else "") + \
               (f" · {raw['tpm']} 3pm" if raw["tpm"] >= 3 else "")
        m = mu.get(r["owner"])
        mt = ""
        if m:
            me, opp, op = m
            mt = (f"<div class='mt'><b class='{'w' if me >= op else 'l'}'>{me:.1f}</b> – {op:.1f}<br>"
                  f"vs {H.escape(opp)}</div>")
        prs.append(f"<div class='pr2{' lead' if i == 0 else ''}'><img src='{H.escape(r['hs'])}'>"
                   f"<div><div class='nm2'>{H.escape(r['name'])}<span>{r['team']}</span></div><div class='ln'>{line}</div></div>"
                   f"<div class='fp{' el' if r['elite'] else ''}'>{fmt_pts(r['fp'])}<small>FPTS</small></div>"
                   f"<div><div class='ow'>{H.escape(r['owner'])}</div>"
                   f"<span class='bd {'st' if r['starting'] else 'bn'}'>{'STARTING' if r['starting'] else 'BENCH'}</span></div>"
                   f"{mt}</div>")
    if fa and fa["fp"] >= 15:
        raw = fa["raw"]
        prs.append(f"<div class='pr2'><img src='{H.escape(fa['hs'])}'>"
                   f"<div><div class='nm2'>{H.escape(fa['name'])}<span>{fa['team']}</span></div>"
                   f"<div class='ln'>{raw['pts']} pts · {raw['reb']} reb · {raw['ast']} ast</div></div>"
                   f"<div class='fp'>{fmt_pts(fa['fp'])}<small>FPTS</small></div>"
                   f"<div><div class='ow'>Nobody</div><span class='bd fa'>FREE AGENT</span></div>"
                   f"<div class='mt'>waiver pickup?</div></div>")
    n = len(prs)
    rest = [r for r in rows if r["owner"]][max_rows:]
    more = ("<div class='more'>Also: " + " · ".join(f"<b>{H.escape(short_name(r['name']))}</b> {fmt_pts(r['fp'])} ({H.escape(r['owner'])})"
                                                   for r in rest[:5]) + "</div>") if rest else ""
    ca, ch = team_cols(away, home)
    body = (f"<div class='top'><div class='lg'><img src='{NBA_LOGO}'>NBA · {H.escape(league.upper())} LEAGUE</div>"
            f"<div class='pill'>{status}</div></div>"
            f"<div class='hd'><div><div class='s2'>YOUR LEAGUE'S PLAYERS IN THIS GAME</div>"
            f"<div class='big2'>{len([r for r in rows if r['owner']])} league players · "
            f"{fmt_pts(sum(r['fp'] for r in rows if r['owner'] and r['starting']))} fpts from starters</div></div>"
            f"{score_strip()}</div><div class='pl2'>{''.join(prs)}</div>{more}")
    return _card(f"--a:{ca};--h:{ch}", body, 64 + 70 + n * 62 + (28 if more else 0) + 8)


def nba_batch_html(mode, events, summaries, live, title):
    """Several games ending / at half-time together: 1 fantasy line per game + league matchups."""
    import html as H
    league = CONFIG["nba"].get("league_name", "League")
    lines = []
    for e, summ in zip(events, summaries):
        away, home = teams_of(e)
        a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
        rows = nba_league_rows(summ) if summ else []
        owned = [r for r in rows if r["owner"]]
        f1 = "<span class='m'>No league players</span>"
        if owned:
            t = owned[0]
            f1 = (f"⭐ <b>{H.escape(t['name'])}</b><span class='chipx'>{fmt_pts(t['fp'])}</span>"
                  f"<span class='m'>{H.escape(t['owner'])}{'' if t['starting'] else ' (bench)'}</span>")
            if len(owned) > 1:
                s2 = owned[1]
                f1 += f" <span class='m'>· {H.escape(short_name(s2['name']))} {fmt_pts(s2['fp'])}</span>"
        stt = "FINAL" if e["status"]["type"].get("completed") else game_status(e)
        lines.append(f"<div class='gl'><div class='sc2'><img src='{H.escape(away['team'].get('logo', ''))}'>"
                     f"<span>{away['team']['abbreviation']}</span><b class='r' style='{'' if a_s > h_s else 'color:#8b95a8'}'>{a_s}</b>"
                     f"<span>–</span><b style='{'' if h_s > a_s else 'color:#8b95a8'}'>{h_s}</b>"
                     f"<span class='r'>{home['team']['abbreviation']}</span><img src='{H.escape(home['team'].get('logo', ''))}'></div>"
                     f"<div class='st {'fin' if stt == 'FINAL' else 'live'}'>{H.escape(stt)}</div><div class='f1'>{f1}</div></div>")
    lg = ""
    if live and live["pairs"]:
        cells = []
        for ra, rb in live["pairs"]:
            A, B = live["rosters"][ra], live["rosters"][rb]
            aw = A["points"] >= B["points"]
            cells.append(f"<div class='mu'><span class='n{' w' if aw else ''}'>{H.escape(A['manager'])}</span><span></span>"
                         f"<span class='p{'' if aw else ' lo'}'>{A['points']:.1f}</span><i>·</i>"
                         f"<span class='p{' lo' if aw else ''}'>{B['points']:.1f}</span><span></span>"
                         f"<span class='n r{'' if aw else ' w'}'>{H.escape(B['manager'])}</span></div>")
        lg = (f"<div class='lg2'><div class='ch'>{H.escape(league.upper())} · WEEK {live['week']} MATCHUPS · LIVE</div>"
              f"<div class='mus'>{''.join(cells)}</div></div>")
    rows_mu = ((len(live["pairs"]) + 1) // 2) if lg else 0
    body = (f"<div class='top'><div class='lg'><img src='{NBA_LOGO}'>NBA · {H.escape(title.upper())}</div>"
            f"<div class='pill'>{mode}</div></div><div class='rows' style='gap:6px'>{''.join(lines)}</div>{lg}")
    return _card("--a:#c9082a;--h:#17408b", body, 64 + len(events) * 56 + (44 + rows_mu * 26 if lg else 0) + 26)


def _nba_post(html_text, content, embed, name):
    m = re.search(r"body,\.card\{height:(\d+)px", html_text)
    png = render_png(html_text, 880, int(m.group(1)) if m else 600)
    if png:
        embed["image"] = {"url": f"attachment://{name}"}
    return discord(NBA_HOOK, embed, f"NBA {name}", content=content, files={name: png} if png else None)


def closest_matchup_line(live):
    if not live or not live.get("pairs"):
        return ""
    best = min(live["pairs"], key=lambda p: abs(live["rosters"][p[0]]["points"] - live["rosters"][p[1]]["points"]))
    A, B = live["rosters"][best[0]], live["rosters"][best[1]]
    return f"🔥 Closest matchup: **{A['manager']}** {A['points']:.1f} – {B['points']:.1f} **{B['manager']}**"


def post_nba_single(e, live):
    """One game finished on its own: fantasy-first card."""
    summ = fetch(f"basketball/nba/summary?event={e['id']}")
    rows = nba_league_rows(summ)
    away, home = teams_of(e)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    owned = [r for r in rows if r["owner"]]
    lines = []
    if owned:
        t = owned[0]
        title = f"{t['name']} drops {fmt_pts(t['fp'])} for {t['owner']}"
    elif rows:
        title = f"{rows[0]['name']} {fmt_pts(rows[0]['fp'])} fpts — and he's a free agent"
    else:
        title = "Full-time"
    fa = next((r for r in rows if not r["owner"]), None)
    if fa and fa["fp"] >= 15:
        lines.append(f"🆓 {fa['name']} ({fmt_pts(fa['fp'])}) is a free agent")
    by_mgr = {}
    for r in owned:
        if r["starting"]:
            by_mgr.setdefault(r["owner"], []).append(r)
    duo = max(((m, ps) for m, ps in by_mgr.items() if len(ps) >= 2), key=lambda kv: sum(p["fp"] for p in kv[1]),
              default=None)
    if duo:
        m, ps = duo
        lines.append(f"👥 {m} got {fmt_pts(sum(p['fp'] for p in ps))} from "
                     + " + ".join(short_name(p["name"]) for p in ps[:3]))
    embed = {"author": {"name": f"NBA · {CONFIG['nba'].get('league_name', 'League')} league", "icon_url": NBA_LOGO},
             "title": title, "url": f"https://www.espn.com/nba/game/_/gameId/{e['id']}", "color": 0x17408B}
    if lines:
        embed["description"] = "\n".join(lines)
    content = (f"🏁 **FULL-TIME** · {away['team'].get('name')} {a_s}–{h_s} {home['team'].get('name')}{ot_tag(e)}")
    return _nba_post(nba_game_fantasy_html(e, summ, live), content, embed, "game.png")


def post_nba_group(mode, events, live):
    """Several games finishing (or at half-time) together: 1 fantasy line per game."""
    events = sorted(events, key=lambda x: x["date"])
    sums = []
    for e in events:
        try:
            sums.append(fetch(f"basketball/nba/summary?event={e['id']}"))
        except Exception:  # noqa: BLE001
            sums.append(None)
    t0 = parse_time(events[0]["date"]).astimezone(UK).strftime("%H:%M")
    t1 = parse_time(events[-1]["date"]).astimezone(UK).strftime("%H:%M")
    title = f"{t0}{'–' + t1 if t1 != t0 else ''} UK tip-offs · {len(events)} games"
    best = None
    for s in sums:
        for r in (nba_league_rows(s) if s else []):
            if r["owner"] and (best is None or r["fp"] > best["fp"]):
                best = r
    embed = {"author": {"name": f"NBA · {CONFIG['nba'].get('league_name', 'League')} league", "icon_url": NBA_LOGO},
             "color": 0x17408B}
    if best:
        embed["title"] = f"Best {'so far' if mode == 'HALF-TIME' else 'of the bunch'}: {best['name']} " \
                         f"{fmt_pts(best['fp'])} ({best['owner']})"
    cl = closest_matchup_line(live)
    if cl:
        embed["description"] = cl
    emoji = "⏸️" if mode == "HALF-TIME" else "🏁"
    content = f"{emoji} **{mode}** · NBA · {len(events)} games"
    return _nba_post(nba_batch_html(mode, events, sums, live, title), content, embed,
                     "half.png" if mode == "HALF-TIME" else "batch.png")


def clock_secs(e):
    try:
        return float(e["status"].get("clock", 0))
    except (TypeError, ValueError):
        return 0.0


def run_nba(st, dates=None, force=False):
    """Fantasy-first NBA posts, grouped by when games actually finish / reach half-time."""
    if not NBA_HOOK and not DRY_RUN and not SILENT and not force:
        return
    if dates is None:
        today = datetime.now(ET).date()
        dates = [(today - timedelta(days=1)).strftime("%Y%m%d"), today.strftime("%Y%m%d")]
    events, seen = [], set()
    for d in dates:
        for e in fetch(f"basketball/nba/scoreboard?dates={d}").get("events", []):
            if e["id"] not in seen:
                seen.add(e["id"])
                events.append(e)
    G = lambda e: st["nba"].setdefault(e["id"], {"date": e["date"][:10]})  # noqa: E731
    now_s = time.time()
    live_games = [e for e in events if e["status"]["type"]["state"] == "in"]

    # ---------------------------------------------------------------- full time
    finals = []
    for e in events:
        t = e["status"]["type"]
        if t["state"] != "post" or G(e).get("posted"):
            continue
        age_h = (now_utc() - parse_time(e["date"])).total_seconds() / 3600
        if not t.get("completed") or (age_h > STALE_HOURS and not force):
            G(e).update(posted=True, ht=True)        # postponed / too old
            continue
        finals.append(e)
    if finals:
        first = st.setdefault("nba_ft_wait", now_s)
        closing = [e for e in live_games if e["status"].get("period", 0) >= 4]
        if force or not closing or now_s - first >= 25 * 60:
            if SILENT:
                ok = True
            else:
                live = league_live("nba")
                ok = post_nba_single(finals[0], live) if len(finals) == 1 else post_nba_group("FULL-TIME", finals, live)
            if ok:
                for e in finals:
                    G(e).update(posted=True, ht=True)
                st.pop("nba_ft_wait", None)
    else:
        st.pop("nba_ft_wait", None)

    # ---------------------------------------------------------------- half time
    at_half, approaching = [], []
    for e in live_games:
        g = G(e)
        if g.get("ht"):
            continue
        per, name = e["status"].get("period", 0), e["status"]["type"].get("name")
        if name == "STATUS_HALFTIME" or (per == 3 and clock_secs(e) >= 360):
            at_half.append(e)
        elif per >= 3:
            g["ht"] = True                           # missed the window: no half-time post for this one
        elif per == 2 and clock_secs(e) <= 180:
            approaching.append(e)
    if len(at_half) >= 2 and not approaching:
        if SILENT or post_nba_group("HALF-TIME", at_half, league_live("nba")):
            for e in at_half:
                G(e)["ht"] = True


# ------------------------------------------------ NFL fantasy-first posts + live outlook

_HIST = {}


def _history(sport, upto):
    k = (sport, upto)
    if k not in _HIST:
        _HIST[k] = season_history(sport, upto)
    return _HIST[k]


def week_outlook(sport, week, live, teams_left=None, cur_override=None, sims=4000, frac_override=None, left_override=None):
    """Monte Carlo for the current week + rest of season.
    teams_left: NFL teams whose games are still to come/in progress (None = whole week still to play).
    Returns {rid: dict(win, po, po_w, po_l, title, cur, left, base_po)}."""
    settings = league_settings(sport)
    s = settings.get("settings", {})
    po_start, n_po = int(s.get("playoff_week_start") or 15), int(s.get("playoff_teams") or 6)
    scores, rec = _history(sport, week - 1)
    rids = list(live["rosters"].keys())
    for r in rids:
        rec.setdefault(r, {"w": 0, "l": 0, "t": 0, "pf": 0.0})
    allp = [p for v in scores.values() for p in v if p > 0] or [100.0]
    lg = sum(allp) / len(allp)
    sd = max(0.85 * (sum((p - lg) ** 2 for p in allp) / max(1, len(allp) - 1)) ** 0.5, 1.0)
    mean = {r: (sum(p for p in scores.get(r, []) if p > 0) + 2 * lg) / (len([p for p in scores.get(r, []) if p > 0]) + 2)
            for r in rids}
    players = sleeper_players(sport)
    frac, cur, left = {}, {}, {}
    for r in rids:
        x = live["rosters"][r]
        st_ = x["starters"] or []
        if teams_left is None:
            lft = list(st_)
        else:
            lft = [p for p in st_ if (players.get(p) or {}).get("team") in teams_left]
        left[r] = (left_override or {}).get(r, lft)
        frac[r] = (frac_override or {}).get(r, len(lft) / len(st_) if st_ else 1.0)
        cur[r] = max(0.0, (cur_override or {}).get(r, x["points"] if teams_left is not None else 0.0))
    lid = str(CONFIG[sport].get("sleeper_league_id")).strip()
    sched = []
    for wk in range(week + 1, po_start):
        try:
            ms = fetch_url(f"{SLEEPER}/league/{lid}/matchups/{wk}", tries=2)
        except Exception:  # noqa: BLE001
            ms = []
        by = {}
        for m in ms:
            if m.get("matchup_id"):
                by.setdefault(m["matchup_id"], []).append(m["roster_id"])
        sched.append([p for p in by.values() if len(p) == 2])
    rnd = _random.Random(7)
    cnt = {r: {"win": 0, "po": 0, "po_w": 0, "w": 0, "po_l": 0, "l": 0, "title": 0} for r in rids}

    def game(a, b):
        return a if rnd.gauss(mean[a], sd) >= rnd.gauss(mean[b], sd) else b

    for _ in range(sims):
        w = {r: rec[r]["w"] + 0.5 * rec[r]["t"] for r in rids}
        pf = {r: rec[r]["pf"] for r in rids}
        won = {}
        for a, b in live["pairs"]:
            fa = cur[a] + rnd.gauss(mean[a] * frac[a], sd * frac[a] ** 0.5)
            fb = cur[b] + rnd.gauss(mean[b] * frac[b], sd * frac[b] ** 0.5)
            pf[a] += fa
            pf[b] += fb
            win = a if fa >= fb else b
            w[win] += 1
            won[a], won[b] = win == a, win == b
        for pairs in sched:
            for a, b in pairs:
                sa, sb = rnd.gauss(mean[a], sd), rnd.gauss(mean[b], sd)
                pf[a] += sa
                pf[b] += sb
                w[a if sa >= sb else b] += 1
        seeds = sorted(rids, key=lambda r: (-w[r], -pf[r]))[:n_po]
        for r in rids:
            c = cnt[r]
            made = r in seeds
            c["po"] += made
            if won.get(r):
                c["win"] += 1
                c["w"] += 1
                c["po_w"] += made
            else:
                c["l"] += 1
                c["po_l"] += made
        if n_po == 6 and len(seeds) == 6:
            q1, q2 = game(seeds[2], seeds[5]), game(seeds[3], seeds[4])
            lo, hi = (q2, q1) if seeds.index(q2) > seeds.index(q1) else (q1, q2)
            champ = game(game(seeds[0], lo), game(seeds[1], hi))
            cnt[champ]["title"] += 1
    out = {}
    for r in rids:
        c = cnt[r]
        out[r] = {"win": 100 * c["win"] / sims, "po": 100 * c["po"] / sims, "title": 100 * c["title"] / sims,
                  "po_w": 100 * c["po_w"] / c["w"] if c["w"] else None,
                  "po_l": 100 * c["po_l"] / c["l"] if c["l"] else None,
                  "cur": cur[r], "left": left[r], "rec": rec[r]}
    return out


OUT_CSS = """
.mrw{display:grid;grid-template-columns:1fr 150px 1fr;align-items:center;gap:10px;background:rgba(255,255,255,.055);
 border-radius:10px;padding:7px 12px;height:66px}
.mrw .mg{min-width:0}.mrw .mg.r{text-align:right}
.mrw .mn{font-size:16px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mrw .pl{font-size:12px;color:#cfd5e1;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mrw .pl b{color:#fff}
.mrw .st2{font-size:11px;color:#9aa3b5;margin-top:2px;white-space:nowrap}
.mrw .st2 .g{color:#7ee2a8;font-weight:700}.mrw .st2 .b{color:#ff9a9e;font-weight:700}
.mrw .mid{text-align:center;font-variant-numeric:tabular-nums}
.mrw .sc3{font-size:19px;font-weight:900}.mrw .sc3 .lo{color:#8b95a8}
.wc{display:flex;height:6px;border-radius:3px;overflow:hidden;margin-top:5px;background:rgba(255,255,255,.08)}
.wc div{height:6px}.wcl{font-size:10.5px;color:#9aa3b5;margin-top:3px;letter-spacing:.5px}
.gstrip{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.gs{display:flex;align-items:center;gap:5px;background:rgba(255,255,255,.07);border-radius:8px;padding:4px 8px;font-size:12.5px;font-weight:700}
.gs img{width:20px;height:20px;object-fit:contain}
.tdh{display:grid;grid-template-columns:120px 1fr 190px;gap:16px;align-items:center;margin-top:14px}
.tdh .hs2{width:120px;height:120px;border-radius:14px;object-fit:cover;object-position:top;background:rgba(255,255,255,.08)}
.gain{font-size:46px;font-weight:900;color:#7ee2a8;line-height:1}
.gname{font-size:24px;font-weight:800;margin-top:6px}.gplay{font-size:14px;color:#cfd5e1;margin-top:3px}
.ownbox{background:rgba(255,255,255,.07);border-radius:10px;padding:9px 12px;font-size:13px;color:#cfd5e1;line-height:1.5}
.ownbox b{color:#fff;font-size:15px}
.po2{display:grid;grid-template-columns:1fr 1fr;gap:5px 24px;margin-top:6px}
.por{display:grid;grid-template-columns:1fr 54px 92px;align-items:center;gap:8px;font-size:14px;font-variant-numeric:tabular-nums}
.por .n{font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.por .v{font-weight:900;text-align:right}
.por .d{font-size:12px;font-weight:800}.por .d.u{color:#7ee2a8}.por .d.dn{color:#ff9a9e}.por .d.z{color:#8b95a8}
"""


def _pct(v):
    return "—" if v is None else f"{v:.0f}%"


def matchup_rows_html(live, ol, focus_teams=None, show_left=True):
    """One row per league matchup: who's playing, score, live win chance, playoff stakes."""
    import html as H
    players = sleeper_players("nfl")
    rows = []
    for a, b in live["pairs"]:
        A, B = live["rosters"][a], live["rosters"][b]
        oa, ob = ol[a], ol[b]

        def blk(x, o, right):
            names = [short_name((players.get(p) or {}).get("full_name") or
                                f"{(players.get(p) or {}).get('last_name', p)} D/ST") for p in o["left"]]
            pl = (f"<b>{len(names)}</b> playing: " + ", ".join(H.escape(n) for n in names[:4]) +
                  ("…" if len(names) > 4 else "")) if names else "nobody left to play"
            return (f"<div class='mg{' r' if right else ''}'><div class='mn'>{H.escape(x['manager'])}</div>"
                    f"<div class='pl'>{pl}</div>"
                    f"<div class='st2'>playoffs: win <span class='g'>{_pct(o['po_w'])}</span> · lose "
                    f"<span class='b'>{_pct(o['po_l'])}</span></div></div>")
        lead_a = oa["cur"] >= ob["cur"]
        mid = (f"<div class='mid'><div class='sc3'><span class='{'' if lead_a else 'lo'}'>{oa['cur']:.1f}</span> – "
               f"<span class='{'lo' if lead_a else ''}'>{ob['cur']:.1f}</span></div>"
               f"<div class='wc'><div style='width:{oa['win']:.0f}%;background:#5b7bd5'></div>"
               f"<div style='width:{100 - oa['win']:.0f}%;background:#d9534f'></div></div>"
               f"<div class='wcl'>WIN CHANCE {oa['win']:.0f}% · {ob['win']:.0f}%</div></div>")
        rows.append(f"<div class='mrw'>{blk(A, oa, False)}{mid}{blk(B, ob, True)}</div>")
    return "".join(rows)


def playoff_panel_html(live, ol, base, title="PLAYOFF ODDS NOW · CHANGE SINCE THE WEEK STARTED"):
    import html as H
    rids = sorted(ol, key=lambda r: -ol[r]["po"])
    cells = []
    for r in rids:
        d = ol[r]["po"] - base[r]["po"]
        cls = "u" if d >= 1 else ("dn" if d <= -1 else "z")
        arrow = f"▲{d:.0f}" if d >= 1 else (f"▼{abs(d):.0f}" if d <= -1 else "–")
        cells.append(f"<div class='por'><span class='n'>{H.escape(live['rosters'][r]['manager'])}</span>"
                     f"<span class='v'>{ol[r]['po']:.0f}%</span><span class='d {cls}'>{arrow}"
                     f"<span style='color:#9aa3b5;font-weight:600'> · title {ol[r]['title']:.0f}%</span></span></div>")
    return f"<div class='lg2'><div class='ch'>{title}</div><div class='po2'>{''.join(cells)}</div></div>"


def nfl_preview_fantasy_html(events, live, ol, title):
    import html as H
    league = CONFIG["nfl"].get("league_name", "League")
    k = parse_time(events[0]["date"]).astimezone(UK)
    strip = "".join(
        f"<div class='gs'><img src='{H.escape(teams_of(e)[0]['team'].get('logo', ''))}'>{teams_of(e)[0]['team']['abbreviation']}"
        f" @ {teams_of(e)[1]['team']['abbreviation']}<img src='{H.escape(teams_of(e)[1]['team'].get('logo', ''))}'></div>"
        for e in events)
    stakes = max(ol, key=lambda r: (ol[r]["po_w"] or 0) - (ol[r]["po_l"] or 0))
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL · WEEK {live['week']} · {H.escape(title.upper())}</div>"
            f"<div class='pill'>KICK-OFF {k.strftime('%H:%M')} UK</div></div>"
            f"<div class='gstrip'>{strip}</div>"
            f"<div class='sec'>{H.escape(league.upper())} MATCHUPS · SCORE SO FAR · WHO'S STILL TO PLAY · WHAT'S AT STAKE</div>"
            f"<div class='rows' style='gap:6px;margin-top:6px'>{matchup_rows_html(live, ol)}</div>"
            f"<div class='foot'><div class='col'><div class='ch'>BIGGEST STAKES</div><b>{H.escape(live['rosters'][stakes]['manager'])}</b>"
            f" — playoffs {_pct(ol[stakes]['po_w'])} with a win, {_pct(ol[stakes]['po_l'])} with a loss</div></div>")
    n_strip_rows = (len(events) + 4) // 5
    h = 64 + n_strip_rows * 36 + 30 + len(live["pairs"]) * 72 + 86
    return _card("--a:#5b7bd5;--h:#d50a0a", body, h, OUT_CSS + RECAP_CSS)


def play_fpts(td, pos=""):
    """Fantasy points a touchdown play is worth to the scorer and the passer (league scoring)."""
    sc = league_scoring("nfl")
    v = lambda k: float(sc.get(k) or 0)  # noqa: E731
    yds = td["yds"] or 0
    if td["kind"] == "pass":
        bonus = v({"TE": "bonus_rec_te", "RB": "bonus_rec_rb", "WR": "bonus_rec_wr"}.get(pos, "-"))
        return v("rec_td") + yds * v("rec_yd") + v("rec") + bonus, v("pass_td") + yds * v("pass_yd")
    if td["kind"] == "rush":
        return v("rush_td") + yds * v("rush_yd"), 0.0
    return v("st_td") or 6.0, 0.0


def nfl_td_fantasy_html(event, play, summary, live, ol, base, tag):
    import html as H
    away, home = teams_of(event)
    td = td_parse(play)
    pid = boxscore_id(summary, td["scorer"])
    o = league_owner(pid, td["scorer"])
    gain, qb_gain = play_fpts(td, (o or {}).get("pos", ""))
    qo = league_owner(boxscore_id(summary, td["qb"]), td["qb"]) if td.get("qb") else None
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    q = play.get("period", {}).get("number", 0)
    how = (f"{td['yds']}-yd TD catch from {td['qb']}" if td["kind"] == "pass" else
           f"{td['yds']}-yd TD run" if td["kind"] == "rush" else "defensive / return TD")
    own = (f"<b>{H.escape(o['manager'])}</b> {'⭐ starting' if o['starting'] else '(bench — ouch)'}" if o
           else "<b>Unrostered</b> — waiver alert")
    qline = (f"<br>QB {H.escape(td['qb'])} +{qb_gain:.1f} → <b>{H.escape(qo['manager'])}</b>"
             f"{'' if qo['starting'] else ' (bench)'}") if qo else ""
    rid = next((r for r, x in live["rosters"].items() if o and x["manager"] == o["manager"]), None)
    mu = ""
    if rid:
        opp = next((b if a == rid else a for a, b in live["pairs"] if rid in (a, b)), None)
        if opp:
            me, op = ol[rid], ol[opp]
            d = me["po"] - base[rid]["po"]
            mu = (f"<div class='lg2'><div class='ch'>{H.escape(o['manager'].upper())}'S MATCHUP</div>"
                  f"<div class='mrw' style='background:none;padding:0;height:auto'>"
                  f"<div class='mg'><div class='mn'>{H.escape(live['rosters'][rid]['manager'])}</div>"
                  f"<div class='st2'>playoffs now <span class='g'>{me['po']:.0f}%</span> "
                  f"({'▲' if d >= 0 else '▼'}{abs(d):.0f} this week)</div></div>"
                  f"<div class='mid'><div class='sc3'>{me['cur']:.1f} – <span class='lo'>{op['cur']:.1f}</span></div>"
                  f"<div class='wc'><div style='width:{me['win']:.0f}%;background:#5b7bd5'></div>"
                  f"<div style='width:{100 - me['win']:.0f}%;background:#d9534f'></div></div>"
                  f"<div class='wcl'>WIN CHANCE {me['win']:.0f}%</div></div>"
                  f"<div class='mg r'><div class='mn'>{H.escape(live['rosters'][opp]['manager'])}</div>"
                  f"<div class='st2'>{len(op['left'])} starters still to play</div></div></div></div>")
    hs = nfl_headshot(pid) or (away if play["team"]["abbreviation"] == away["team"]["abbreviation"] else home)["team"].get("logo", "")
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>TOUCHDOWN · Q{q} {play.get('clock', {}).get('displayValue', '')}"
            f" · {away['team']['abbreviation']} {a_s}–{h_s} {home['team']['abbreviation']}</div>"
            f"<div class='pill'>{H.escape(tag)}</div></div>"
            f"<div class='tdh'><img class='hs2' src='{H.escape(hs)}'>"
            f"<div><div class='gain'>+{gain:.1f} FPTS</div><div class='gname'>{H.escape(td['scorer'])}</div>"
            f"<div class='gplay'>{H.escape(how)}</div></div>"
            f"<div class='ownbox'>{own}{qline}</div></div>{mu}")
    return _card("--a:#1faa59;--h:#5b7bd5", body, 64 + 140 + (120 if mu else 0) + 20, OUT_CSS + RECAP_CSS)


def nfl_game_fantasy_html(event, summary, live, ol, base, max_rows=7):
    import html as H
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    ff = nfl_fantasy(summary)
    rows = []
    for p in ff:
        o = league_owner(p.get("id"), p["name"])
        rows.append({**p, "owner": o["manager"] if o else None, "starting": bool(o and o["starting"])})
    owned = [r for r in rows if r["owner"]][:max_rows]
    fa = next((r for r in rows if not r["owner"]), None)
    mu = nba_matchup_lookup(live)
    prs = []
    for i, r in enumerate(owned):
        m = mu.get(r["owner"])
        mt = (f"<div class='mt'><b class='{'w' if m[0] >= m[2] else 'l'}'>{m[0]:.1f}</b> – {m[2]:.1f}<br>vs {H.escape(m[1])}</div>"
              if m else "")
        prs.append(f"<div class='pr2{' lead' if i == 0 else ''}'><img src='{H.escape(nfl_headshot(r.get('id')))}'>"
                   f"<div><div class='nm2'>{H.escape(r['name'])}<span>{r['team']} · {r.get('pos', '')}</span></div>"
                   f"<div class='ln'>{H.escape(r['line'])}</div></div>"
                   f"<div class='fp{' el' if r['elite'] else ''}'>{fmt_pts(r['pts'])}<small>FPTS</small></div>"
                   f"<div><div class='ow'>{H.escape(r['owner'])}</div>"
                   f"<span class='bd {'st' if r['starting'] else 'bn'}'>{'STARTING' if r['starting'] else 'BENCH'}</span></div>{mt}</div>")
    if fa and fa["pts"] >= 12:
        prs.append(f"<div class='pr2'><img src='{H.escape(nfl_headshot(fa.get('id')))}'>"
                   f"<div><div class='nm2'>{H.escape(fa['name'])}<span>{fa['team']} · {fa.get('pos', '')}</span></div>"
                   f"<div class='ln'>{H.escape(fa['line'])}</div></div><div class='fp'>{fmt_pts(fa['pts'])}<small>FPTS</small></div>"
                   f"<div><div class='ow'>Nobody</div><span class='bd fa'>FREE AGENT</span></div><div class='mt'>waiver pickup?</div></div>")
    ca, ch = team_cols(away, home)
    strip = (f"<div class='sm'><img src='{H.escape(away['team'].get('logo', ''))}'>{away['team']['abbreviation']} "
             f"<span class='{'lo' if a_s < h_s else ''}'>{a_s}</span> – <span class='{'lo' if h_s < a_s else ''}'>{h_s}</span> "
             f"{home['team']['abbreviation']}<img src='{H.escape(home['team'].get('logo', ''))}'></div>")
    involved = {r["owner"] for r in owned}
    rid_inv = [r for r, x in live["rosters"].items() if x["manager"] in involved]
    sub = {r: ol[r] for r in rid_inv}
    panel = playoff_panel_html(live, sub, base, "PLAYOFF ODDS FOR THESE MANAGERS · CHANGE SINCE THE WEEK STARTED") if sub else ""
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL · WEEK {live['week']} · "
            f"{H.escape(CONFIG['nfl'].get('league_name', 'League').upper())}</div><div class='pill'>FULL-TIME</div></div>"
            f"<div class='hd'><div><div class='s2'>YOUR LEAGUE'S PLAYERS IN THIS GAME</div>"
            f"<div class='big2'>{len([r for r in rows if r['owner']])} league players · "
            f"{fmt_pts(sum(r['pts'] for r in rows if r['owner'] and r['starting']))} fpts from starters</div></div>{strip}</div>"
            f"<div class='pl2'>{''.join(prs)}</div>{panel}")
    return _card(f"--a:{ca};--h:{ch}", body, 64 + 70 + len(prs) * 62 + (60 + ((len(sub) + 1) // 2) * 24 if panel else 0) + 14,
                 OUT_CSS + RECAP_CSS)


# ------------------------------------------------ v2: auto-height cards + Sunday live updates

def _card(css_vars, body, height=None, extra_css="", width=880):
    """Card page that measures itself (no cropping). height is ignored, kept for old callers."""
    wide = f"body,.card{{width:{width}px!important}}" if width != 880 else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@500;600;700;800;900&display=swap' rel='stylesheet'>"
            f"<style>{NFL_CSS}{SLOT_CSS}{FAN_CSS}{RECAP_CSS}{OUT_CSS}{V2_CSS}{globals().get('V4_CSS', '')}{extra_css}"
            f"body,.card{{height:auto!important}}.card{{padding-bottom:6px}}{wide}</style></head><body>"
            f"<div class='card' style='{css_vars}'><div class='bg'></div><div class='in'>{body}</div></div>"
            f"<script>addEventListener('load',()=>document.body.setAttribute('data-h',"
            f"Math.ceil(document.querySelector('.card').getBoundingClientRect().height)))</script></body></html>")


def render_card(html_text, width=None):
    """Render a self-measuring card: measure its height first, then screenshot exactly that."""
    import subprocess
    import tempfile
    chrome = find_chrome()
    if not chrome:
        log("No Chrome found: posting without the image card")
        return None
    if width is None:
        m = re.search(r"body,\.card\{width:(\d+)px", html_text)
        width = int(m.group(1)) if m else 880
    with tempfile.TemporaryDirectory() as d:
        src, out = Path(d) / "card.html", Path(d) / "card.png"
        src.write_text(html_text)
        base = [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
                "--virtual-time-budget=9000"]
        try:
            dom = subprocess.run(base + [f"--window-size={width},3000", "--dump-dom", src.as_uri()],
                                 check=True, timeout=60, capture_output=True, text=True).stdout
            m = re.search(r'data-h="(\d+)"', dom)
            height = int(m.group(1)) if m else 900
            subprocess.run(base + ["--force-device-scale-factor=2", f"--window-size={width},{height}",
                                   "--default-background-color=00000000", f"--screenshot={out}", src.as_uri()],
                           check=True, timeout=60, capture_output=True)
            return out.read_bytes()
        except Exception as e:  # noqa: BLE001
            log(f"card render failed: {e}")
            return None


V2_CSS = """
.mrw{height:auto!important;padding:9px 14px!important;grid-template-columns:1fr 170px 1fr!important;align-items:start!important}
.mrw .mn{font-size:17px!important;white-space:normal!important}
.mrw .pl{font-size:14px!important;white-space:normal!important;line-height:1.5;margin-top:5px;letter-spacing:0!important;color:#cfd5e1}
.mrw .pl.none{font-size:12.5px!important;color:#6f788b!important;font-style:italic}
.mrw .pc{display:inline-block;background:#f5c542;color:#1b1f2a;font-weight:800;border-radius:6px;padding:1px 8px;margin:2px 3px 2px 0;font-size:14px}
.mrw .pc small{font-size:11.5px;font-weight:800;opacity:.72;margin-left:6px}
.mrw.hot{background:rgba(245,197,66,.10)!important;box-shadow:inset 3px 0 0 #f5c542}
.mrw.hot2{background:rgba(245,197,66,.10)!important;box-shadow:inset -3px 0 0 #f5c542}
.mrw.hot.hot2{box-shadow:inset 3px 0 0 #f5c542,inset -3px 0 0 #f5c542}
.mrw .st2{letter-spacing:0!important}
.rows.mu2{display:grid!important;grid-template-columns:1fr 1fr;gap:10px!important}
.mrw{grid-template-columns:1fr 300px 1fr!important;align-items:center!important;padding:12px 16px!important;border-radius:12px!important}
.mrw .mn{font-size:19px!important;letter-spacing:0!important}
.pps{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}.mg.r .pps{justify-content:flex-end}
.pp{font-size:11.5px;font-weight:700;letter-spacing:.6px;color:#aab3c5;background:rgba(255,255,255,.07);border-radius:6px;padding:3px 7px}
.pp b{color:#fff;font-size:13px;margin-left:2px}.pp.w b{color:#7ee2a8}.pp.l b{color:#ff9a9e}
.pp .dl{margin-left:5px}
.mid .sc3{font-size:26px!important;letter-spacing:0!important;display:flex;justify-content:center;gap:8px}
.mid .dash{color:#5d6679}
.wcb{display:grid;grid-template-columns:42px 1fr 42px;align-items:center;gap:7px;margin-top:8px}
.wc2{display:flex;height:12px;border-radius:6px;overflow:hidden;background:rgba(255,255,255,.08)}
.wc2 div{height:12px}
.wa{color:#9cb3ff;font-weight:900;font-size:16px;text-align:right}.wb{color:#ffc56b;font-weight:900;font-size:16px;text-align:left}
.wcb.big3{grid-template-columns:58px 1fr 58px}.wcb.big3 .wa,.wcb.big3 .wb{font-size:24px}.wcb.big3 .wc2{height:14px}
.po3{font-size:22px;font-weight:900;margin-top:8px;letter-spacing:0}.po3 .lbl{font-size:11px;letter-spacing:1.4px;color:#9aa3b5;font-weight:700}
.po3.out{font-size:13px;font-weight:600;color:#6f788b;font-style:italic}
.wl{font-size:13.5px;color:#aab3c5;margin-top:2px;font-weight:600}.wl .g{color:#7ee2a8;font-weight:800}.wl .b{color:#ff9a9e;font-weight:800}
.pps{display:block!important}
.cmp{display:grid;grid-template-columns:1fr 1fr;gap:6px 24px;margin-top:6px}
.cm{display:grid;grid-template-columns:1fr 46px 90px 46px 1fr;gap:8px;align-items:center;font-size:14px;color:#cfd5e1;
 background:rgba(255,255,255,.045);border-radius:8px;padding:6px 10px}
.cm .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:700}.cm .n.r{text-align:right}
.cm .a{color:#9cb3ff;font-weight:800;text-align:right}.cm .b2{color:#ffc56b;font-weight:800}
.mid .wcl{font-size:10.5px!important;letter-spacing:1.4px;color:#8b95a8;margin-top:5px;font-weight:700}
.mrw .st2{font-size:12.5px!important;white-space:normal!important;margin-top:4px}
.mrw .sc3{font-size:22px!important}.wcl{font-size:11.5px!important}
.mrw .dl{font-size:12.5px;font-weight:800;margin-left:4px}.mrw .dl.u{color:#7ee2a8}.mrw .dl.dn{color:#ff9a9e}
.gstrip .gs{font-size:13.5px!important;padding:5px 9px!important}
.gs .gst{color:#ffb4b6;font-size:11.5px;font-weight:700;margin-left:2px}.gs .gst.f{color:#aab3c5}
.gs .lo2{color:#8b95a8}
.spl{display:grid;grid-template-columns:28px 1fr auto 190px;align-items:center;gap:10px;background:rgba(255,255,255,.055);
 border-radius:9px;padding:7px 12px}
.spl img{width:28px;height:28px;object-fit:contain}
.spl .w1{font-size:15px;font-weight:800}.spl .w2{font-size:12.5px;color:#cfd5e1;margin-top:1px}
.spl .gp{font-size:16px;font-weight:900;color:#fff;background:#1faa59;border-radius:6px;padding:3px 8px;text-align:center}
.spl .gp.z{background:rgba(255,255,255,.12);color:#cfd5e1}
.spl .o2{font-size:13.5px;font-weight:700;text-align:right}.spl .o2 small{display:block;font-size:11px;color:#9aa3b5;font-weight:600}
.sw{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:6px}
.sw .col{background:rgba(255,255,255,.055);border-radius:10px;padding:9px 12px;font-size:14px;color:#cfd5e1;line-height:1.5}
.sw b{color:#fff}.sw .u{color:#7ee2a8;font-weight:800}.sw .dn{color:#ff9a9e;font-weight:800}
.pr2{height:auto!important;min-height:56px}
.pr2 .nm2,.pr2 .ln{white-space:normal!important}
"""


def matchup_rows_html(live, ol, base=None, delta=None, players_label="playing", max_names=5, show_teams=None,
                      proj=None):
    """v2: one row per matchup, nothing clipped. delta = points gained since last update per roster."""
    import html as H
    players = sleeper_players("nfl")
    rows, hot = [], {}
    for a, b in live["pairs"]:
        A, B = live["rosters"][a], live["rosters"][b]
        oa, ob = ol[a], ol[b]

        def blk(x, o, rid, right):
            names = []
            for p in o["left"]:
                pp = players.get(p) or {}
                if show_teams is not None and pp.get("team") not in show_teams:
                    continue
                nm = pp.get("full_name") and short_name(pp["full_name"]) or f"{pp.get('last_name', p)} D/ST"
                if proj is not None:
                    pos = pp.get("position") or "DEF"
                    pj = proj.get(str(p))
                    nm = (nm, f"{pos} · proj {pj:.1f}" if pj else pos)
                names.append(nm)
            cap = max_names or len(names)
            if names:
                pl = (f"<div class='pl'><b>{len(names)}</b> {players_label}<br>" +
                      "".join(f"<span class='pc'>{H.escape(n[0])}<small>{H.escape(n[1])}</small></span>" if isinstance(n, tuple)
                              else f"<span class='pc'>{H.escape(n)}</span>" for n in names[:cap]) +
                      (f" <span style='color:#9aa3b5'>+{len(names) - cap} more</span>" if len(names) > cap else "") + "</div>")
            else:
                pl = f"<div class='pl none'>{'nobody left to play' if show_teams is None else 'nobody in this game'}</div>"
            hot[rid] = bool(names)
            mv = ""
            if base:
                d = o["po"] - base[rid]["po"]
                if abs(d) >= 1:
                    mv = f"<span class='dl {'u' if d > 0 else 'dn'}'>{'▲' if d > 0 else '▼'}{abs(d):.0f}</span>"
            if o["po"] < 0.5 and (o["po_w"] or 0) < 0.5:
                pills = "<div class='po3 out'>out of the playoff race</div>"
            else:
                pills = f"<div class='po3'><span class='lbl'>PLAYOFFS</span> <b>{o['po']:.0f}%</b>{mv}</div>"
                if o["po_w"] is not None and o["po_l"] is not None and abs(o["po_w"] - o["po_l"]) >= 3:
                    pills += (f"<div class='wl'>win → <span class='g'>{_pct(o['po_w'])}</span> · "
                              f"lose → <span class='b'>{_pct(o['po_l'])}</span></div>")
            return (f"<div class='mg{' r' if right else ''}'><div class='mn'>{H.escape(x['manager'])}</div>"
                    f"{pl}<div class='pps'>{pills}</div></div>")
        lead_a = oa["cur"] >= ob["cur"]
        da = f"<div class='wcl'>LAST 30 MIN +{delta[a]:.1f} · +{delta[b]:.1f}</div>" if delta else ""
        wca = "WIN CHANCE"
        if base:
            dw = oa["win"] - base[a]["win"]
            if abs(dw) >= 3:
                wca += f" · {'▲' if dw > 0 else '▼'}{abs(dw):.0f} / {'▼' if dw > 0 else '▲'}{abs(dw):.0f}"
        any_pts = oa["cur"] > 0 or ob["cur"] > 0
        score = (f"<div class='sc3'><span class='{'' if lead_a else 'lo'}'>{oa['cur']:.1f}</span>"
                 f"<span class='dash'>–</span><span class='{'lo' if lead_a else ''}'>{ob['cur']:.1f}</span></div>") if any_pts else ""
        mid = (f"<div class='mid'>{score}"
               f"{meter_html(oa['win'], '' if any_pts else 'big')}"
               f"<div class='wcl'>{wca}</div>{da}</div>")
        left_html, right_html = blk(A, oa, a, False), blk(B, ob, b, True)
        cls = ("hot" if hot.get(a) else "") + (" hot2" if hot.get(b) else "")
        rows.append(f"<div class='mrw {cls}'>{left_html}{mid}{right_html}</div>")
    return "".join(rows)


def games_strip_html(events, state_at=None, pre=False):
    """Small chips for every game: logos, score and status (state_at: {event id: (away, home, status)})."""
    import html as H
    out = []
    for e in events:
        aw, hm = teams_of(e)
        if state_at and e["id"] in state_at:
            a_s, h_s, stt = state_at[e["id"]]
        elif pre or state_at is not None or e["status"]["type"]["state"] == "pre":
            a_s = h_s = None
            stt = parse_time(e["date"]).astimezone(UK).strftime("%H:%M")
        else:
            a_s, h_s, stt = int(num(aw.get("score"))), int(num(hm.get("score"))), game_status(e)
        sc = (f" <span class='{'lo2' if a_s < h_s else ''}'>{a_s}</span>–<span class='{'lo2' if h_s < a_s else ''}'>{h_s}</span> "
              if a_s is not None else " @ ")
        out.append(f"<div class='gs'><img src='{H.escape(aw['team'].get('logo', ''))}'>{aw['team']['abbreviation']}{sc}"
                   f"{hm['team']['abbreviation']}<img src='{H.escape(hm['team'].get('logo', ''))}'>"
                   f"<span class='gst{' f' if str(stt).startswith('FINAL') or ':' in str(stt) and a_s is None else ''}'>{H.escape(str(stt))}</span></div>")
    return f"<div class='gstrip'>{''.join(out)}</div>"


def _preview_rows(live, ol, show_teams, league):
    """Matchups with players in this window as full rows; the rest compact."""
    import html as H
    players = sleeper_players("nfl")

    def n_in(r):
        return sum(1 for p in ol[r]["left"] if show_teams is None or (players.get(p) or {}).get("team") in show_teams)
    pairs = sorted(live["pairs"], key=lambda p: -(n_in(p[0]) + n_in(p[1])))
    hot = [p for p in pairs if n_in(p[0]) + n_in(p[1]) > 0]
    cold = [p for p in pairs if p not in hot]
    sub = dict(live, pairs=hot)
    html_rows = (f"<div class='sec'>{H.escape(league.upper())} · MATCHUPS WITH PLAYERS IN THIS GAME</div>"
                 f"<div class='rows' style='gap:8px;margin-top:8px'>"
                 f"{matchup_rows_html(sub, ol, max_names=None, show_teams=show_teams, players_label='playing' if show_teams is None else 'in this game', proj=nfl_projections(live['week']))}</div>")
    if cold:
        html_rows += f"<div class='sec'>OTHER MATCHUPS · WIN CHANCE</div>{compact_matchups_html(live, ol, cold)}"
    return html_rows


def compact_matchups_html(live, ol, pairs):
    import html as H
    out = []
    for a, b in pairs:
        A, B = live["rosters"][a], live["rosters"][b]
        w = ol[a]["win"]
        out.append(f"<div class='cm' style='grid-template-columns:1fr 200px 1fr'><span class='n'>{H.escape(A['manager'])}</span>"
                   f"{meter_html(w, 'sm')}<span class='n r'>{H.escape(B['manager'])}</span></div>")
    return f"<div class='cmp'>{''.join(out)}</div>"


def nfl_preview_fantasy_html(events, live, ol, title, show_teams=None):
    """v2 preview (30 min before kick-off) — same layout for every window."""
    import html as H
    league = CONFIG["nfl"].get("league_name", "League")
    k = parse_time(events[0]["date"]).astimezone(UK)
    stakes = max(ol, key=lambda r: (ol[r]["po_w"] or 0) - (ol[r]["po_l"] or 0))
    fav = max(live["pairs"], key=lambda p: abs(ol[p[0]]["win"] - 50))
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL · WEEK {live['week']} · {H.escape(title.upper())}</div>"
            f"<div class='pill'>{'KICK-OFF IMMINENT' if now_utc() > parse_time(events[0]['date']) else 'KICK-OFF ' + k.strftime('%H:%M') + ' UK'}</div></div>"
            f"{games_strip_html(events, pre=True)}"
            + _preview_rows(live, ol, show_teams, league) +
            f"<div class='sw' style='grid-template-columns:1fr'>"
            f"<div class='col'><div class='ch'>SAFEST LEAD THIS WEEK</div><b>{H.escape(live['rosters'][fav[0] if ol[fav[0]]['win'] > 50 else fav[1]]['manager'])}</b>"
            f" — {max(ol[fav[0]]['win'], ol[fav[1]]['win']):.0f}% to win this week</div></div>")
    return _card("--a:#5b7bd5;--h:#d50a0a", body, width=1000)


def kicker_fpts(yds):
    sc = league_scoring("nfl")
    for lo, hi, k in ((0, 19, "fgm_0_19"), (20, 29, "fgm_20_29"), (30, 39, "fgm_30_39"), (40, 49, "fgm_40_49"),
                      (50, 59, "fgm_50_59"), (60, 99, "fgm_60p")):
        if lo <= yds <= hi:
            return float(sc.get(k) or sc.get("fgm") or 3)
    return 3.0


def scoring_line_html(p):
    """p: dict(logo, who, what, pts, owner, starting)."""
    import html as H
    own = (f"{H.escape(p['owner'])}<small>{'starting' if p['starting'] else 'bench'}</small>" if p["owner"]
           else "Unrostered<small>free agent</small>")
    return (f"<div class='spl'><img src='{H.escape(p['logo'])}'><div><div class='w1'>{H.escape(p['who'])}</div>"
            f"<div class='w2'>{H.escape(p['what'])}</div></div>"
            f"<div class='gp{'' if p['owner'] and p['starting'] else ' z'}'>+{p['pts']:.1f}</div><div class='o2'>{own}</div></div>")


def nfl_live_update_html(stamp, title, events, state_at, scoring, live, ol, base, prev, delta):
    """Sunday window: everything since the last update, fantasy first."""
    import html as H
    league = CONFIG["nfl"].get("league_name", "League")
    sc_html = "".join(scoring_line_html(p) for p in scoring) or "<div class='spl'><div></div><div class='w2'>No scores in the last 30 minutes</div></div>"
    swings = sorted(ol, key=lambda r: -abs(ol[r]["win"] - prev[r]["win"]))[:3]
    sw = "<br>".join(
        f"<b>{H.escape(live['rosters'][r]['manager'])}</b> {prev[r]['win']:.0f}% → "
        f"<span class='{'u' if ol[r]['win'] >= prev[r]['win'] else 'dn'}'>{ol[r]['win']:.0f}%</span>" for r in swings)
    pom = sorted(ol, key=lambda r: -abs(ol[r]["po"] - prev[r]["po"]))[:3]
    pm = "<br>".join(
        f"<b>{H.escape(live['rosters'][r]['manager'])}</b> {prev[r]['po']:.0f}% → "
        f"<span class='{'u' if ol[r]['po'] >= prev[r]['po'] else 'dn'}'>{ol[r]['po']:.0f}%</span>" for r in pom)
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL · WEEK {live['week']} · {H.escape(title.upper())}</div>"
            f"<div class='pill' style='border-color:#ff9a9e;color:#ffb4b6'>LIVE · {stamp} UPDATE</div></div>"
            f"{games_strip_html(events, state_at)}"
            f"<div class='sec'>EVERY SCORE IN THE LAST 30 MINUTES · FANTASY POINTS TO {H.escape(league.upper())}</div>"
            f"<div class='rows mu2' style='gap:6px!important;margin-top:6px'>{sc_html}</div>"
            f"<div class='sec'>MATCHUPS NOW</div><div class='rows mu2' style='margin-top:8px'>"
            f"{matchup_rows_html(live, ol, base, delta, 'still to play')}</div>"
            f"<div class='sw'><div class='col'><div class='ch'>WIN CHANCE SWINGS (LAST 30 MIN)</div>{sw}</div>"
            f"<div class='col'><div class='ch'>PLAYOFF ODDS MOVERS (LAST 30 MIN)</div>{pm}</div></div>")
    return _card("--a:#d9534f;--h:#5b7bd5", body, width=1560)


# ------------------------------------------------ live wiring: NFL fantasy posts + NBA playoff odds

AU_NFL = lambda: {"name": f"NFL · {CONFIG['nfl'].get('league_name', 'League')}", "icon_url": NFL_LOGO}  # noqa: E731
AU_NBA = lambda: {"name": f"NBA · {CONFIG['nba'].get('league_name', 'League')} league", "icon_url": NBA_LOGO}  # noqa: E731


HL_CSS = """
.hl{margin:12px 0 4px;padding:12px 16px;border-radius:12px;background:rgba(255,255,255,.07);border-left:4px solid #d97757}
.hl .h1{font-size:22px;font-weight:900;line-height:1.25;letter-spacing:0}
.hl .h2{font-size:15.5px;color:#dbe0ea;margin-top:5px;line-height:1.45;letter-spacing:0}
.hl .h2 b{color:#fff}.hl .h2 i{color:#e9ecf3}
"""


def _md_inline(text):
    import html as H
    t = H.escape(text or "")
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", t)
    return t.replace("\n", "<br>")


def _inject_headline(html_text, embed):
    """Put the post's headline (and extra lines) inside the card, just under its top bar."""
    title = embed.get("img_title", embed.get("title"))
    desc = embed.get("img_desc", embed.get("description"))
    if not title and not desc:
        return html_text
    block = ("<div class='hl'>" + (f"<div class='h1'>{_md_inline(title)}</div>" if title else "") +
             (f"<div class='h2'>{_md_inline(desc)}</div>" if desc else "") + "</div>")
    i = html_text.find("<div class='top'>")
    j = html_text.find("</div></div>", i) if i >= 0 else -1
    if j < 0:
        i = html_text.find("<div class='in'>")
        return html_text[:i + 16] + block + html_text[i + 16:] if i >= 0 else html_text
    j += len("</div></div>")
    html_text = html_text[:j] + block + html_text[j:]
    return html_text.replace("</style>", HL_CSS + "</style>", 1)


def _post_card(hook, html_text, content, embed, name):
    if (CONFIG.get("bot") or {}).get("image_only", True):
        png = render_card(_inject_headline(html_text, embed))
        if png:
            return discord(hook, [], name, files={name: png})
    png = render_card(html_text)
    if png:
        embed["image"] = {"url": f"attachment://{name}"}
    return discord(hook, embed, name, content=content, files={name: png} if png else None)


def game_elapsed(e):
    """Share of an ESPN game already played (0..1)."""
    t = e["status"]["type"]
    if t.get("completed") or t["state"] == "post":
        return 1.0
    if t["state"] == "pre":
        return 0.0
    per = e["status"].get("period", 1)
    try:
        clk = float(e["status"].get("clock", 0))
    except (TypeError, ValueError):
        clk = 0.0
    if per > 4:
        return 0.97
    return min(0.99, ((per - 1) * 900 + (900 - clk)) / 3600)


def nfl_ctx(st, events):
    """Live league context: Sleeper week, matchups, live outlook and the week-start baseline."""
    live = league_live("nfl")
    if not live or not live.get("pairs"):
        return None
    players = sleeper_players("nfl")
    gofteam = {}
    for e in events:
        for t in teams_of(e):
            gofteam[sl_team(t["team"]["abbreviation"])] = e
    frac, left = {}, {}
    for r, x in live["rosters"].items():
        sts = x["starters"] or []
        share, lft = 0.0, []
        for p in sts:
            e = gofteam.get((players.get(p) or {}).get("team"))
            if e is None:
                continue
            rem = 1 - game_elapsed(e)
            if rem > 0.001:
                share += rem
                lft.append(p)
        frac[r] = share / len(sts) if sts else 0.0
        left[r] = lft
    week = live["week"]
    teams_left = {k for k, e in gofteam.items() if game_elapsed(e) < 1}
    ol = week_outlook("nfl", week, live, teams_left=teams_left, frac_override=frac, left_override=left)
    bases = st.setdefault("nfl_base", {})
    if str(week) not in bases:
        b = week_outlook("nfl", week, live, teams_left=None)
        bases.clear()
        bases[str(week)] = {str(r): {"po": v["po"], "win": v["win"], "title": v["title"]} for r, v in b.items()}
    base = {r: bases[str(week)].get(str(r), ol[r]) for r in ol}
    return {"live": live, "ol": ol, "base": base, "week": week}


# ---------------------------------------------------------------- prime-time windows

def post_prime_preview(events, st, ctx):
    k = parse_time(events[0]["date"]).astimezone(UK)
    teams = {sl_team(t["team"]["abbreviation"]) for e in events for t in teams_of(e)}
    live, ol = ctx["live"], ctx["ol"]
    players = sleeper_players("nfl")
    involved = [r for r in ol if any((players.get(p) or {}).get("team") in teams for p in live["rosters"][r]["starters"])]
    title_bits = " · ".join(f"{teams_of(e)[0]['team'].get('name')} @ {teams_of(e)[1]['team'].get('name')}" for e in events)
    day = k.strftime("%A")
    embed = {"author": AU_NFL(), "color": 0x013369, "title": f"Who needs what from {title_bits}"}
    if involved:
        s = max(involved, key=lambda r: (ol[r]["po_w"] or 0) - (ol[r]["po_l"] or 0))
        if ol[s]["po_w"] is not None and ol[s]["po_l"] is not None:
            embed["description"] = (f"🔥 Biggest stakes: **{live['rosters'][s]['manager']}** — playoffs "
                                    f"{ol[s]['po_w']:.0f}% with a win, {ol[s]['po_l']:.0f}% with a loss")
    html_text = nfl_preview_fantasy_html(events, live, ol, f"{day} night" if len(events) == 1 else f"{day} {k:%H:%M}",
                                         show_teams=teams)
    when = "kick-off imminent" if now_utc() > parse_time(events[0]["date"]) else f"kick-off {k:%H:%M} UK"
    return _post_card(NFL_HOOK, html_text, f"🏈 **PRE-GAME** · {title_bits} · {when}", embed, "preview.png")


def post_td_fantasy(event, play, summary, ctx):
    away, home = teams_of(event)
    td = td_parse(play)
    pid = boxscore_id(summary, td["scorer"])
    o = league_owner(pid, td["scorer"])
    gain, _ = play_fpts(td, (o or {}).get("pos", ""))
    abbr = play.get("team", {}).get("abbreviation", "")
    team = away if away["team"]["abbreviation"] == abbr else home
    nick = team["team"].get("name") or abbr
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    scored = 6 + (1 if td["pat"] and "Good" in td["pat"]["mark"] and "Extra" in td["pat"]["label"] else
                  2 if td["pat"] and "Good" in td["pat"]["mark"] else 0)
    a0, h0 = (a_s - scored, h_s) if team is away else (a_s, h_s - scored)
    lead_txt, tag = lead_context(team is away, a_s, h_s, a0, h0)
    drive, detail = play_detail(summary, play.get("id"))
    down_text = ((detail or {}).get("start") or {}).get("downDistanceText", "")
    seed = int(re.sub(r"\D", "", str(play.get("id", "0")))[-6:] or 0)
    call = commentary(td, (detail or {}).get("text", ""), down_text, nick, lead_txt, seed)
    q = play.get("period", {}).get("number", 0)
    qtxt = f"Q{q}" if q <= 4 else "OT"
    now = player_now(summary, td["scorer"])
    who = (f"{td['scorer']} +{gain:.1f}" + (f" (now {fmt_pts(now)})" if now is not None else "") +
           (f" for {o['manager']}" + ("" if o["starting"] else " (bench)") if o else " — unrostered!"))
    if td.get("qb"):
        qo = league_owner(boxscore_id(summary, td["qb"]), td["qb"])
        qn = player_now(summary, td["qb"])
        _, qg = play_fpts(td)
        who += (f" · QB {short_name(td['qb'])} +{qg:.1f}" + (f" (now {fmt_pts(qn)})" if qn is not None else "") +
                (f" for {qo['manager']}" if qo else ""))
    embed = {"author": AU_NFL(), "color": color(team["team"].get("color")), "title": who,
             "img_title": f"{td['scorer']} scores for the {nick}!" if td['kind'] != 'pass'
             else f"{td['qb']} finds {td['scorer']} — touchdown {nick}!"}
    if call:
        embed["description"] = call
    content = (f"🏈 **TOUCHDOWN {nick.upper()}** · {away['team']['abbreviation']} {a_s}–{h_s} "
               f"{home['team']['abbreviation']} · {qtxt} {play.get('clock', {}).get('displayValue', '')}")
    html_text = nfl_td_fantasy_html(event, play, summary, ctx["live"], ctx["ol"], ctx["base"], tag)
    return _post_card(NFL_HOOK, html_text, content, embed, "td.png")


def post_prime_final(event, summary, ctx):
    away, home = teams_of(event)
    a, h = int(num(away.get("score"))), int(num(home.get("score")))
    ff = nfl_fantasy(summary)
    owned = []
    for p in ff:
        o = league_owner(p.get("id"), p["name"])
        if o:
            owned.append((p, o))
    title = (f"{owned[0][0]['name']} drops {fmt_pts(owned[0][0]['pts'])} for {owned[0][1]['manager']}" if owned
             else nfl_headline(event)[0])
    lines = []
    bench = [(p, o) for p, o in owned if not o["starting"] and p["pts"] >= 12]
    if bench:
        p, o = bench[0]
        lines.append(f"🪑 {p['name']} put up {fmt_pts(p['pts'])} on {o['manager']}'s bench")
    fa = next((p for p in ff if not league_owner(p.get("id"), p["name"]) and p["pts"] >= 12), None)
    if fa:
        lines.append(f"🆓 {fa['name']} ({fmt_pts(fa['pts'])}) is a free agent")
    embed = {"author": AU_NFL(), "color": color((away if a > h else home)["team"].get("color")), "title": title,
             "url": f"https://www.espn.com/nfl/game/_/gameId/{event['id']}"}
    if lines:
        embed["description"] = "\n".join(lines)
    content = (f"🏁 **FULL-TIME** · {away['team'].get('name')} {a}–{h} {home['team'].get('name')}{ot_tag(event)}")
    return _post_card(NFL_HOOK, nfl_game_fantasy_html(event, summary, ctx["live"], ctx["ol"], ctx["base"]),
                      content, embed, "ft.png")


def nfl_prime_slot(slot, st, getctx, force=False):
    """A prime-time window (single game, or games kicking off together outside Sunday 18:00-24:00)."""
    key = "p:" + slot[0]["date"]
    s = st.setdefault("nfl_slots", {}).setdefault(key, {"date": slot[0]["date"][:10]})
    age = (now_utc() - parse_time(slot[0]["date"])).total_seconds() / 3600
    if age > STALE_HOURS and not force:
        for e in slot:
            st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})["final"] = True
        s["pre"] = True
        return False
    states = [e["status"]["type"]["state"] for e in slot]
    if not s.get("pre"):
        if all(x == "pre" for x in states) and -0.5 <= age < -0.02:
            ctx = getctx()
            if ctx is None or post_prime_preview(slot, st, ctx):
                s["pre"] = True
        elif age >= -0.02 or any(x != "pre" for x in states):
            s["pre"] = True
    active = -0.6 <= age < 0.5
    for e in slot:
        g = st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})
        g["preview"] = True
        stype = e["status"]["type"]
        if stype["state"] == "in":
            active = True
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            plays = [p for p in summary.get("scoringPlays", [])
                     if p.get("type", {}).get("abbreviation") == "TD"
                     or "touchdown" in (p.get("type", {}).get("text", "").lower())]
            for i, p in enumerate(plays):
                if p["id"] in g["tds"]:
                    continue
                has_pat = "(" in (p.get("text") or "")
                first = g["pending"].setdefault(p["id"], time.time())
                if not has_pat and i == len(plays) - 1 and time.time() - first < TD_PAT_WAIT:
                    continue
                ctx = getctx()
                ok = post_td_fantasy(e, p, summary, ctx) if ctx else post_touchdown(e, p, summary)
                if ok:
                    g["tds"].append(p["id"])
                    g["pending"].pop(p["id"], None)
        elif stype["state"] == "post" and not g.get("final"):
            if not stype.get("completed"):
                g["final"] = True
                continue
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            ctx = getctx()
            if (post_prime_final(e, summary, ctx) if ctx else post_nfl_final(e, summary, True)):
                g["final"] = True
    return active


# ---------------------------------------------------------------- Sunday 18:00 window

_NAMEPID = {}


def nfl_name_pid():
    if not _NAMEPID:
        for pid, p in sleeper_players("nfl").items():
            if p.get("full_name") and p.get("team"):
                _NAMEPID[(name_key(p["full_name"]), p["team"])] = pid
    return _NAMEPID


def play_credit(sp):
    """Fantasy credit for a scoring play: [(sleeper pid or team for D/ST, pts, role)]."""
    team = sl_team(sp["team"]["abbreviation"])
    sc = league_scoring("nfl")
    if sp.get("type", {}).get("abbreviation") == "SF" or "safety" in sp.get("type", {}).get("text", "").lower():
        return [(team, float(sc.get("safe") or 2), "DEF")]
    if re.search(r"interception return|fumble return|fumble recovery|blocked", sp.get("text", ""), re.I):
        return [(team, float(sc.get("def_td") or 6), "DEF")]
    npid = nfl_name_pid()
    m = re.match(r"^(.+?) (\d+) Yd Field Goal", sp.get("text", ""))
    if m:
        pid = npid.get((name_key(m.group(1)), team))
        return [(pid, kicker_fpts(int(m.group(2))), "K")] if pid else []
    td = td_parse(sp)
    out = []
    pid = npid.get((name_key(td["scorer"]), team))
    g, qg = play_fpts(td, (sleeper_players("nfl").get(pid) or {}).get("position", ""))
    if pid:
        out.append((pid, g, "scorer"))
    if td.get("qb"):
        q = npid.get((name_key(td["qb"]), team))
        if q:
            out.append((q, qg, "qb"))
    return out


def scoring_since(events, since, until):
    """Every scoring play (all window games) between two times, as card lines."""
    live_rosters = None
    out = []
    for e in events:
        if e["status"]["type"]["state"] == "pre":
            continue
        try:
            s = fetch(f"football/nfl/summary?event={e['id']}")
        except Exception:  # noqa: BLE001
            continue
        sp = {p["id"]: p for p in s.get("scoringPlays", [])}
        drives = list((s.get("drives") or {}).get("previous") or [])
        if (s.get("drives") or {}).get("current"):
            drives.append(s["drives"]["current"])
        for d in drives:
            for p in d.get("plays") or []:
                if p["id"] not in sp or not p.get("wallclock"):
                    continue
                t = datetime.strptime(p["wallclock"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if not (since < t <= until):
                    continue
                spl = sp[p["id"]]
                team = next(x for x in teams_of(e) if x["team"]["abbreviation"] == spl["team"]["abbreviation"])
                cr = play_credit(spl)
                main = next((c for c in cr if c[2] in ("scorer", "DEF")), None) or next((c for c in cr if c[2] == "K"), None)
                td = td_parse(spl)
                fg = re.match(r"^(.+?) (\d+) Yd Field Goal", spl.get("text", ""))
                if main and main[2] == "DEF":
                    if live_rosters is None:
                        live_rosters = (league_live("nfl") or {}).get("rosters", {})
                    own = next((x for x in live_rosters.values() if main[0] in x["players"]), None)
                    out.append({"t": t, "logo": team["team"].get("logo", ""), "who": f"{team['team'].get('name')} D/ST",
                                "what": "safety" if main[1] < 6 else "defensive touchdown", "pts": main[1],
                                "owner": own["manager"] if own else None,
                                "starting": bool(own and main[0] in own["starters"])})
                    continue
                if fg:
                    who, what = fg.group(1), f"{fg.group(2)}-yd field goal"
                else:
                    who = td["scorer"]
                    what = (f"{td['yds']}-yd TD catch from {td['qb']}" if td["kind"] == "pass" else
                            f"{td['yds']}-yd TD run" if td["kind"] == "rush" else "touchdown")
                    q = next((c for c in cr if c[2] == "qb"), None)
                    if q:
                        qo = league_owner("", td["qb"])
                        qn = player_now(s, td["qb"])
                        what += (f"  ·  QB {short_name(td['qb'])} +{q[1]:.1f}" + (f" (now {fmt_pts(qn)})" if qn is not None else "")
                                 + (f" → {qo['manager']}" if qo else ""))
                o = league_owner("", who)
                out.append({"t": t, "logo": team["team"].get("logo", ""), "who": who, "what": what,
                            "pts": main[1] if main else (3.0 if fg else 6.0), "owner": o["manager"] if o else None,
                            "starting": bool(o and o["starting"]), "now": player_now(s, who) if not fg else None})
    return sorted(out, key=lambda x: x["t"])


def nfl_sunday_window(win, st, getctx, force=False):
    """Sunday 18:00-24:00 UK: one preview at 17:30, then an update every 30 min until 00:30."""
    first = parse_time(win[0]["date"])
    key = "sun:" + win[0]["date"][:10]
    s = st.setdefault("nfl_slots", {}).setdefault(key, {"date": win[0]["date"][:10]})
    for e in win:                                    # no per-game posts for these games
        st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}}).update(final=True, preview=True)
    now = now_utc()
    age = (now - first).total_seconds() / 3600
    if age > STALE_HOURS and not force:
        s.update(pre=True, done=True)
        return False
    teams = {sl_team(t["team"]["abbreviation"]) for e in win for t in teams_of(e)}
    if not s.get("pre"):
        if -0.5 <= age < -0.02:
            ctx = getctx()
            if ctx:
                live, ol = ctx["live"], ctx["ol"]
                stake = max(ol, key=lambda r: (ol[r]["po_w"] or 0) - (ol[r]["po_l"] or 0))
                embed = {"author": AU_NFL(), "color": 0x013369,
                         "title": f"Biggest stakes today: {live['rosters'][stake]['manager']}",
                         "description": f"Playoffs {_pct(ol[stake]['po_w'])} with a win, {_pct(ol[stake]['po_l'])} with a loss"}
                html_text = nfl_preview_fantasy_html(win, live, ol, f"Sunday window · {len(win)} games", show_teams=teams)
                if _post_card(NFL_HOOK, html_text, f"🏈 **PRE-GAME** · Sunday window · {len(win)} games · first kick-off "
                                                    f"{first.astimezone(UK):%H:%M} UK", embed, "preview.png"):
                    s["pre"] = True
                    s["last"] = first.strftime("%Y-%m-%dT%H:%M:%SZ")
                    s["snap"] = {str(r): x["points"] for r, x in live["rosters"].items()}
                    s["prev"] = {str(r): {"win": v["win"], "po": v["po"]} for r, v in ol.items()}
        elif age >= -0.02:
            s["pre"] = True
    if s.get("done"):
        return False
    # half-hour marks from 18:30 to 00:30 UK
    loc = now.astimezone(UK)
    start_loc = first.astimezone(UK)
    end_loc = start_loc.replace(hour=0, minute=30) + timedelta(days=1) if start_loc.hour >= 12 else start_loc
    mark = loc.replace(minute=30 if loc.minute >= 30 else 0, second=0, microsecond=0)
    all_done = all(e["status"]["type"]["state"] == "post" for e in win)
    if mark <= start_loc or (mark.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") <= s.get("last", "")):
        return age > -0.6
    if mark > end_loc and not all_done:
        mark = end_loc
    ctx = getctx()
    if not ctx:
        return True
    live, ol = ctx["live"], ctx["ol"]
    since = datetime.strptime(s.get("last") or first.strftime("%Y-%m-%dT%H:%M:%SZ"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    scoring = scoring_since(win, since, now)
    snap = s.get("snap") or {}
    prev_raw = s.get("prev") or {}
    delta = {r: x["points"] - float(snap.get(str(r), x["points"])) for r, x in live["rosters"].items()}
    prev = {r: prev_raw.get(str(r), {"win": ol[r]["win"], "po": ol[r]["po"]}) for r in ol}
    state_at = {e["id"]: (int(num(teams_of(e)[0].get("score"))), int(num(teams_of(e)[1].get("score"))), game_status(e))
                for e in win if e["status"]["type"]["state"] != "pre"}
    final = all_done or mark >= end_loc
    stamp = mark.strftime("%H:%M")
    html_text = nfl_live_update_html(stamp if not final else "FINAL", "Sunday window", win, state_at, scoring, live, ol,
                                     ctx["base"], prev, delta)
    swing = max(ol, key=lambda r: abs(ol[r]["win"] - prev[r]["win"]))
    best = max(delta, key=lambda r: delta[r])
    embed = {"author": AU_NFL(), "color": 0xD9534F,
             "title": f"{live['rosters'][swing]['manager']}: win chance {prev[swing]['win']:.0f}% → {ol[swing]['win']:.0f}%",
             "description": f"📈 Best 30 minutes: **{live['rosters'][best]['manager']}** +{delta[best]:.1f}\n"
                            f"🏈 {len(scoring)} score{'s' if len(scoring) != 1 else ''} since the last update"}
    head = "🏁 **FINAL UPDATE**" if final else f"🔴 **LIVE · {stamp}**"
    if _post_card(NFL_HOOK, html_text, f"{head} · Sunday window", embed, "live.png"):
        s["last"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        s["snap"] = {str(r): x["points"] for r, x in live["rosters"].items()}
        s["prev"] = {str(r): {"win": v["win"], "po": v["po"]} for r, v in ol.items()}
        if final:
            s["done"] = True
    return True


def run_nfl(st, date=None, force=False):
    """v3: Sunday 18:00-24:00 window grouped (preview + 30-min updates); every other window prime-time."""
    path = "football/nfl/scoreboard" + (f"?dates={date}" if date else "")
    events = fetch(path).get("events", [])
    cache = {}

    def getctx():
        if "ctx" not in cache:
            try:
                cache["ctx"] = nfl_ctx(st, events) if not SILENT else None
            except Exception as ex:  # noqa: BLE001
                log(f"league context unavailable: {ex}")
                cache["ctx"] = None
        return cache["ctx"]

    active = False
    sunday = [e for e in events if not is_prime(parse_time(e["date"]))]
    if sunday:
        try:
            active |= bool(nfl_sunday_window(sorted(sunday, key=lambda e: e["date"]), st, getctx, force))
        except Exception as ex:  # noqa: BLE001
            log(f"NFL Sunday window error: {ex}")
    prime = [e for e in events if is_prime(parse_time(e["date"]))]
    for slot in nfl_slots(prime):
        try:
            active |= bool(nfl_prime_slot(slot, st, getctx, force))
        except Exception as ex:  # noqa: BLE001
            log(f"NFL prime slot {slot[0].get('date')} error: {ex}")
    return active


# ---------------------------------------------------------------- NBA: playoff odds on posts

def nba_ctx(st):
    live = league_live("nba")
    if not live or not live.get("pairs"):
        return None, None, None
    now = datetime.now(ET)
    rem = max(0.0, (7 - (now.weekday() + now.hour / 24)) / 7)          # Sleeper NBA weeks run Mon-Sun
    frac = {r: rem for r in live["rosters"]}
    ol = week_outlook("nba", live["week"], live, teams_left=set(), frac_override=frac,
                      left_override={r: [] for r in live["rosters"]}, sims=3000)
    bases = st.setdefault("nba_base", {})
    if str(live["week"]) not in bases:
        b = week_outlook("nba", live["week"], live, teams_left=None,
                         cur_override={r: 0.0 for r in live["rosters"]}, sims=3000)
        bases.clear()
        bases[str(live["week"])] = {str(r): {"po": v["po"], "win": v["win"], "title": v["title"]} for r, v in b.items()}
    base = {r: bases[str(live["week"])].get(str(r), ol[r]) for r in ol}
    return live, ol, base


_nba_game_html_v1 = nba_game_fantasy_html
_nba_batch_html_v1 = nba_batch_html
_NBA_ODDS = {}


def _inject_panel(html_text, panel):
    i = html_text.rfind("</div></div><script>")
    return html_text[:i] + panel + html_text[i:] if i > 0 and panel else html_text


def nba_game_fantasy_html(event, summary, live, max_rows=7):
    html_text = _nba_game_html_v1(event, summary, live, max_rows)
    ol, base = _NBA_ODDS.get("ol"), _NBA_ODDS.get("base")
    if not (ol and live):
        return html_text
    owners = {r["owner"] for r in nba_league_rows(summary) if r["owner"]}
    sub = {r: ol[r] for r, x in live["rosters"].items() if x["manager"] in owners and r in ol}
    return _inject_panel(html_text, playoff_panel_html(live, sub, base, "PLAYOFF ODDS FOR THESE MANAGERS · CHANGE SINCE THE WEEK STARTED") if sub else "")


def nba_batch_html(mode, events, summaries, live, title):
    html_text = _nba_batch_html_v1(mode, events, summaries, live, title)
    ol, base = _NBA_ODDS.get("ol"), _NBA_ODDS.get("base")
    if not (ol and live):
        return html_text
    return _inject_panel(html_text, playoff_panel_html(live, ol, base, "PLAYOFF ODDS NOW · CHANGE SINCE THE WEEK STARTED"))


_post_nba_single_v1 = post_nba_single
_post_nba_group_v1 = post_nba_group
_NBA_STATE = {}


def nba_odds_due(st):
    """NBA playoff odds: on every post in the last 6 regular-season weeks; before that only on the
    first NBA post on the days in config (default Wednesday + Friday; Monday's recap has them too)."""
    cfg = CONFIG["nba"].get("playoff_odds") or {}
    try:
        pws = int(league_settings("nba").get("settings", {}).get("playoff_week_start") or 0)
        week = int(fetch_url(f"{SLEEPER}/state/nba").get("week") or 0)
        if pws and week >= pws - int(cfg.get("every_post_weeks_before_playoffs", 6)):
            return True
    except Exception as e:  # noqa: BLE001
        log(f"NBA week unavailable: {e}")
    now = datetime.now(UK)
    return (WEEKDAYS[now.weekday()] in cfg.get("days", ["Wednesday", "Friday"])
            and st.get("nba_odds_day") != now.strftime("%Y-%m-%d"))


def _with_nba_odds(fn):
    def wrapped(*a, **k):
        try:
            st = _NBA_STATE.setdefault("st", {})
            _NBA_ODDS.clear()
            if nba_odds_due(st):
                live, ol, base = nba_ctx(st)
                _NBA_ODDS.update(ol=ol, base=base)
                st["nba_odds_day"] = datetime.now(UK).strftime("%Y-%m-%d")
        except Exception as ex:  # noqa: BLE001
            log(f"NBA odds unavailable: {ex}")
            _NBA_ODDS.clear()
        return fn(*a, **k)
    return wrapped


post_nba_single = _with_nba_odds(_post_nba_single_v1)
post_nba_group = _with_nba_odds(_post_nba_group_v1)


def _nba_post(html_text, content, embed, name):           # v2 renderer (auto height)
    return _post_card(NBA_HOOK, html_text, content, embed, name)



_run_nba_v1 = run_nba


def run_nba(st, dates=None, force=False):
    _NBA_STATE["st"] = st
    return _run_nba_v1(st, dates, force)


# ------------------------------------------------ v4: win-chance meter, running totals, QB pairing

V4_CSS = """
.meter{display:grid;grid-template-columns:52px 1fr 52px;align-items:center;gap:10px;margin-top:8px}
.meter .pa,.meter .pb{font-size:17px;font-weight:900;font-variant-numeric:tabular-nums}
.meter .pa{text-align:right}.meter .pb{text-align:left}
.meter .fav{color:#ffffff}.meter .dog{color:#6f788b}
.track{position:relative;height:10px;border-radius:999px;background:linear-gradient(90deg,rgba(255,255,255,.07),rgba(255,255,255,.12),rgba(255,255,255,.07))}
.track .mid0{position:absolute;left:50%;top:-3px;width:2px;height:16px;margin-left:-1px;background:rgba(255,255,255,.25);border-radius:1px}
.track .fill{position:absolute;top:0;height:10px;border-radius:999px;background:linear-gradient(90deg,#d97757,#f2a77f)}
.track .fill.r{background:linear-gradient(90deg,#f2a77f,#d97757)}
.track .knob{position:absolute;top:50%;width:20px;height:20px;margin:-10px 0 0 -10px;border-radius:50%;background:#fff;
 box-shadow:0 0 0 4px rgba(217,119,87,.45),0 0 16px 4px rgba(242,167,127,.55)}
.meter.big .pa,.meter.big .pb{font-size:26px}.meter.big{grid-template-columns:70px 1fr 70px}
.meter.sm{grid-template-columns:40px 1fr 40px;gap:8px;margin:0}.meter.sm .pa,.meter.sm .pb{font-size:14px}
.meter.sm .track{height:6px}.meter.sm .track .fill{height:6px}.meter.sm .knob{width:12px;height:12px;margin:-6px 0 0 -6px}
.meter.sm .mid0{top:-2px;height:10px}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:14px}
.pair.one{grid-template-columns:1fr}
.pp2{display:grid;grid-template-columns:96px 1fr;gap:14px;align-items:center;background:rgba(255,255,255,.06);border-radius:14px;padding:12px}
.pp2 img{width:96px;height:96px;border-radius:12px;object-fit:cover;object-position:top;background:rgba(255,255,255,.08)}
.pp2 .role{font-size:11px;letter-spacing:1.5px;color:#9aa3b5;font-weight:700}
.pp2 .nm{font-size:21px;font-weight:900;margin-top:2px;line-height:1.1}
.pp2 .gn{display:flex;align-items:baseline;gap:10px;margin-top:6px}
.pp2 .gn b{font-size:30px;font-weight:900;color:#7ee2a8}.pp2 .gn span{font-size:15px;color:#cfd5e1;font-weight:700}
.pp2 .gn span em{font-style:normal;color:#fff;font-size:19px;font-weight:900}
.pp2 .ow3{font-size:13.5px;color:#cfd5e1;margin-top:5px}.pp2 .ow3 b{color:#fff}
.spl .now{display:block;font-size:11.5px;color:#cfd5e1;font-weight:700;margin-top:2px;text-align:center}
"""


def meter_html(win_a, size=""):
    """Win-chance meter: soft track, glowing marker that slides toward the favourite."""
    wa = max(0.0, min(100.0, win_a))
    pos = 100 - wa                                  # knob near the left when the left manager is favoured
    lo, hi = sorted((50.0, pos))
    fav_left = wa >= 50
    return (f"<div class='meter {size}'><span class='pa {'fav' if fav_left else 'dog'}'>{wa:.0f}%</span>"
            f"<div class='track'><div class='mid0'></div>"
            f"<div class='fill{' r' if not fav_left else ''}' style='left:{lo:.1f}%;width:{hi - lo:.1f}%'></div>"
            f"<div class='knob' style='left:{pos:.1f}%'></div></div>"
            f"<span class='pb {'dog' if fav_left else 'fav'}'>{100 - wa:.0f}%</span></div>")


def player_now(summary, name):
    p = player_tonight(summary, name) if summary else None
    return p["pts"] if p else None


def nfl_td_fantasy_html(event, play, summary, live, ol, base, tag):
    """v4 touchdown card: scorer + QB as a pairing, points gained and running totals."""
    import html as H
    away, home = teams_of(event)
    td = td_parse(play)
    pid = boxscore_id(summary, td["scorer"])
    o = league_owner(pid, td["scorer"])
    gain, qb_gain = play_fpts(td, (o or {}).get("pos", ""))
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    q = play.get("period", {}).get("number", 0)
    scorer_team = away if play["team"]["abbreviation"] == away["team"]["abbreviation"] else home

    def panel(role, name, pid_, gain_, owner):
        hs = nfl_headshot(pid_) or scorer_team["team"].get("logo", "")
        now = player_now(summary, name)
        own = (f"<b>{H.escape(owner['manager'])}</b> {'⭐ starting' if owner['starting'] else '· bench'}" if owner
               else "<b>Unrostered</b> — free agent")
        tot = f"<span>now <em>{fmt_pts(now)}</em> pts</span>" if now is not None else ""
        return (f"<div class='pp2'><img src='{H.escape(hs)}'><div><div class='role'>{role}</div>"
                f"<div class='nm'>{H.escape(name)}</div><div class='gn'><b>+{gain_:.1f}</b>{tot}</div>"
                f"<div class='ow3'>{own}</div></div></div>")

    if td["kind"] == "pass":
        what = f"{td['yds']}-yard touchdown pass"
        qpid = boxscore_id(summary, td["qb"])
        qo = league_owner(qpid, td["qb"])
        panels = (panel("RECEIVER", td["scorer"], pid, gain, o) + panel("QUARTERBACK", td["qb"], qpid, qb_gain, qo))
        pair_cls = "pair"
    else:
        what = (f"{td['yds']}-yard touchdown run" if td["kind"] == "rush" else "defensive / return touchdown")
        panels = panel("SCORER", td["scorer"], pid, gain, o)
        pair_cls = "pair one"

    mu = ""
    rid = next((r for r, x in live["rosters"].items() if o and x["manager"] == o["manager"]), None)
    if rid:
        opp = next((b if a == rid else a for a, b in live["pairs"] if rid in (a, b)), None)
        if opp:
            me, op = ol[rid], ol[opp]
            d = me["po"] - base[rid]["po"]
            mv = f" ({'▲' if d > 0 else '▼'}{abs(d):.0f} this week)" if abs(d) >= 1 else ""
            mu = (f"<div class='lg2'><div class='ch'>{H.escape(o['manager'].upper())}'S MATCHUP</div>"
                  f"<div class='mrw' style='background:none!important;padding:4px 0 0!important'>"
                  f"<div class='mg'><div class='mn'>{H.escape(live['rosters'][rid]['manager'])}</div>"
                  f"<div class='po3'><span class='lbl'>PLAYOFFS</span> <b>{me['po']:.0f}%</b>"
                  f"<span style='font-size:13px;color:#9aa3b5;font-weight:600'>{mv}</span></div></div>"
                  f"<div class='mid'><div class='sc3'><span>{me['cur']:.1f}</span><span class='dash'>–</span>"
                  f"<span class='lo'>{op['cur']:.1f}</span></div>{meter_html(me['win'])}"
                  f"<div class='wcl'>WIN CHANCE</div></div>"
                  f"<div class='mg r'><div class='mn'>{H.escape(live['rosters'][opp]['manager'])}</div>"
                  f"<div class='wl'>{len(op['left'])} starters still to play</div></div></div></div>")
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>TOUCHDOWN · Q{q} "
            f"{play.get('clock', {}).get('displayValue', '')} · {away['team']['abbreviation']} {a_s}–{h_s} "
            f"{home['team']['abbreviation']}</div><div class='pill'>{H.escape(tag)}</div></div>"
            f"<div class='sec' style='margin-top:10px'>{H.escape(what.upper())}</div>"
            f"<div class='{pair_cls}'>{panels}</div>{mu}")
    return _card("--a:#1faa59;--h:#5b7bd5", body, extra_css=V4_CSS)


def scoring_line_html(p):
    """v4: points gained plus the player's total now."""
    import html as H
    own = (f"{H.escape(p['owner'])}<small>{'starting' if p['starting'] else 'bench'}</small>" if p["owner"]
           else "Unrostered<small>free agent</small>")
    now = f"<span class='now'>now {fmt_pts(p['now'])}</span>" if p.get("now") is not None else ""
    return (f"<div class='spl'><img src='{H.escape(p['logo'])}'><div><div class='w1'>{H.escape(p['who'])}</div>"
            f"<div class='w2'>{H.escape(p['what'])}</div></div>"
            f"<div><div class='gp{'' if p['owner'] and p['starting'] else ' z'}'>+{p['pts']:.1f}</div>{now}</div>"
            f"<div class='o2'>{own}</div></div>")


# ------------------------------------------------ NFL daily playoff race (14:00 UK, day after games, not Tue)

RACE_CSS = """
.rt{margin-top:10px;background:rgba(255,255,255,.05);border-radius:12px;padding:8px 14px}
.rr2{display:grid;grid-template-columns:30px 1fr 64px 64px 260px 200px;align-items:center;gap:12px;height:40px;
 font-size:16px;font-variant-numeric:tabular-nums}
.rr2.h{height:26px;font-size:11px;letter-spacing:1.4px;color:#9aa3b5;font-weight:700}
.rr2.cut{border-bottom:2px dashed rgba(245,197,66,.55)}
.rr2 .k{color:#9aa3b5;font-weight:800}.rr2 .m{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rr2 .o{display:grid;grid-template-columns:48px 1fr 46px;align-items:center;gap:8px;font-weight:900}
.rr2 .o2{display:grid;grid-template-columns:44px 1fr;align-items:center;gap:8px;font-weight:900}
.rb{height:8px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden}
.rb div{height:8px;border-radius:999px}
.mv{font-size:13px;font-weight:800}.mv.u{color:#7ee2a8}.mv.dn{color:#ff9a9e}.mv.z{color:#5d6679}
.race-note{font-size:13px;color:#9aa3b5;margin-top:8px}
"""


def playoff_race_html(ctx, prev, title_date):
    import html as H
    live, ol, base, week = ctx["live"], ctx["ol"], ctx["base"], ctx["week"]
    league = CONFIG["nfl"].get("league_name", "League")
    settings = league_settings("nfl").get("settings", {})
    po_start, n_po = int(settings.get("playoff_week_start") or 15), int(settings.get("playoff_teams") or 6)
    _, rec = _history("nfl", week - 1)
    order = sorted(ol, key=lambda r: -ol[r]["po"])
    rows = ["<div class='rr2 h'><span>#</span><span>MANAGER</span><span>W-L</span><span>PF</span>"
            "<span>PLAYOFF CHANCE · SINCE YESTERDAY</span><span>TITLE CHANCE</span></div>"]
    for i, r in enumerate(order, 1):
        x = rec.get(r, {"w": 0, "l": 0, "t": 0, "pf": 0})
        po, ti = ol[r]["po"], ol[r]["title"]
        d = po - prev.get(str(r), po)
        mv = (f"<span class='mv u'>▲{d:.0f}</span>" if d >= 1 else f"<span class='mv dn'>▼{abs(d):.0f}</span>" if d <= -1
              else "<span class='mv z'>–</span>")
        rows.append(f"<div class='rr2{' cut' if i == n_po else ''}'><span class='k'>{i}</span>"
                    f"<span class='m'>{H.escape(live['rosters'][r]['manager'])}</span>"
                    f"<span>{x['w']}-{x['l']}{'-' + str(x['t']) if x['t'] else ''}</span><span>{x['pf']:.0f}</span>"
                    f"<span class='o'><span>{po:.0f}%</span><div class='rb'><div style='width:{max(po, 1.5):.1f}%;"
                    f"background:linear-gradient(90deg,#d97757,#f2a77f)'></div></div>{mv}</span>"
                    f"<span class='o2'><span>{ti:.0f}%</span><div class='rb'><div style='width:{min(100, max(ti * 2.5, 1.5)):.1f}%;"
                    f"background:linear-gradient(90deg,#e8b13c,#ffe08a)'></div></div></span></div>")
    left = max(0, po_start - week)
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>{H.escape(league.upper())} · PLAYOFF RACE · "
            f"{H.escape(title_date.upper())}</div><div class='pill'>WEEK {week} IN PROGRESS</div></div>"
            f"<div class='rt'>{''.join(rows)}</div>"
            f"<div class='race-note'>Top {n_po} make the playoffs (dashed line) · playoffs start week {po_start} · "
            f"{left} regular-season week{'s' if left != 1 else ''} to go, including this one · "
            f"odds simulate the rest of this week and every week after, 4,000 times</div>"
            f"<div class='sec'>THIS WEEK'S MATCHUPS · WIN CHANCE RIGHT NOW</div>{compact_matchups_html(live, ol, live['pairs'])}")
    return _card("--a:#d97757;--h:#5b7bd5", body, extra_css=RACE_CSS, width=1100)


def maybe_daily_race(st):
    """14:00 UK on the day after NFL games (never Tuesday — the weekly recap covers that)."""
    cfg = CONFIG["nfl"].get("daily_race") or {}
    if not cfg.get("enabled", True):
        return
    now = datetime.now(UK)
    if WEEKDAYS[now.weekday()] == cfg.get("skip_weekday", "Tuesday") or now.hour < cfg.get("hour_uk", 14):
        return
    today = now.strftime("%Y-%m-%d")
    if st.get("race_day") == today:
        return
    events = fetch("football/nfl/scoreboard").get("events", [])
    since = now_utc() - timedelta(hours=36)
    played = [e for e in events if e["status"]["type"].get("completed") and parse_time(e["date"]) >= since]
    if not played:
        return
    ctx = nfl_ctx(st, events)
    if not ctx:
        return
    prev = st.get("race_snap") or {}
    ol, live = ctx["ol"], ctx["live"]
    mover = max(ol, key=lambda r: abs(ol[r]["po"] - prev.get(str(r), ol[r]["po"])))
    d = ol[mover]["po"] - prev.get(str(mover), ol[mover]["po"])
    fav = max(ol, key=lambda r: ol[r]["title"])
    embed = {"author": AU_NFL(), "color": 0xD97757,
             "title": (f"Biggest mover: {live['rosters'][mover]['manager']} {'▲' if d >= 0 else '▼'}{abs(d):.0f} "
                       f"to {ol[mover]['po']:.0f}%") if abs(d) >= 1 else "Playoff race update",
             "description": f"🏆 Title favourite: **{live['rosters'][fav]['manager']}** {ol[fav]['title']:.0f}%"}
    html_text = playoff_race_html(ctx, prev, now.strftime("%a %-d %b"))
    if _post_card(NFL_HOOK, html_text, f"📈 **PLAYOFF RACE** · {CONFIG['nfl'].get('league_name', 'League')} · "
                                       f"{now:%A %-d %B}", embed, "race.png"):
        st["race_day"] = today
        st["race_snap"] = {str(r): v["po"] for r, v in ol.items()}


# ------------------------------------------------ kickers in the player lists, half-time post, projections

def nfl_kickers(summary):
    """Kickers who have kicked in this game, with league fantasy points (same shape as nfl_fantasy rows)."""
    sc = league_scoring("nfl")
    dists = {}
    for p in summary.get("scoringPlays", []):
        m = re.match(r"^(.+?) (\d+) Yd Field Goal", p.get("text") or "")
        if m:
            dists.setdefault(name_key(m.group(1)), []).append(int(m.group(2)))
    out = []
    for team in summary.get("boxscore", {}).get("players", []):
        cat = next((c for c in team.get("statistics", []) if c.get("name") == "kicking"), None)
        for a in (cat or {}).get("athletes") or []:
            s = dict(zip(cat.get("keys", []), a.get("stats", [])))
            fgm, fga = (int(num(x)) for x in (str(s.get("fieldGoalsMade/fieldGoalAttempts", "0/0")) + "/0").split("/")[:2])
            xpm, xpa = (int(num(x)) for x in (str(s.get("extraPointsMade/extraPointAttempts", "0/0")) + "/0").split("/")[:2])
            if not (fga or xpa):
                continue
            ds = dists.get(name_key(a["athlete"]["displayName"]), [])
            pts = sum(kicker_fpts(d) for d in ds) + max(0, fgm - len(ds)) * kicker_fpts(40)
            pts += xpm * float(sc.get("xpm") or 0) + (xpa - xpm) * float(sc.get("xpmiss") or 0)
            pts += (fga - fgm) * float(sc.get("fgmiss") or 0)
            bits = []
            if fga:
                bits.append(f"FG {fgm}/{fga}" + (f" ({', '.join(str(d) for d in ds)} yds)" if ds else ""))
            if xpa:
                bits.append(f"XP {xpm}/{xpa}")
            out.append({"id": str(a["athlete"]["id"]), "name": a["athlete"]["displayName"],
                        "team": team["team"]["abbreviation"], "pos": "K", "pts": pts,
                        "line": " · ".join(bits), "elite": False})
    return out


_nfl_fantasy_base = nfl_fantasy


def nfl_fantasy(summary):
    """Offensive players + kickers, best first (kickers only show where their points earn a place)."""
    out = _nfl_fantasy_base(summary)
    try:
        out = out + [k for k in nfl_kickers(summary) if k["pts"] > 0]
    except Exception as e:  # noqa: BLE001
        log(f"kicker points unavailable: {e}")
    out.sort(key=lambda x: -x["pts"])
    return out


def nfl_half_html(event, summary, live, ol, base):
    """Half-time: the full-time card's layout, with everyone's points at the half."""
    h = nfl_game_fantasy_html(event, summary, live, ol, base)
    return (h.replace("<div class='pill'>FULL-TIME</div>", "<div class='pill'>HALF-TIME</div>", 1)
             .replace("YOUR LEAGUE'S PLAYERS IN THIS GAME", "YOUR LEAGUE'S PLAYERS · POINTS AT THE HALF", 1))


def post_prime_half(event, summary, ctx):
    away, home = teams_of(event)
    a, h = int(num(away.get("score"))), int(num(home.get("score")))
    owned = [(p, o) for p in nfl_fantasy(summary) for o in [league_owner(p.get("id"), p["name"])] if o]
    title = (f"Half-time · {owned[0][0]['name']} leads the way with {fmt_pts(owned[0][0]['pts'])} for {owned[0][1]['manager']}"
             if owned else f"Half-time · {away['team'].get('name')} {a}–{h} {home['team'].get('name')}")
    embed = {"author": AU_NFL(), "color": 0xD97757, "title": title}
    content = f"⏸️ **HALF-TIME** · {away['team'].get('name')} {a}–{h} {home['team'].get('name')}"
    return _post_card(NFL_HOOK, nfl_half_html(event, summary, ctx["live"], ctx["ol"], ctx["base"]),
                      content, embed, "half.png")


_nfl_prime_slot_base = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    """Prime-time window + a half-time post for each game."""
    for e in slot:
        if e["status"]["type"].get("name") != "STATUS_HALFTIME":
            continue
        g = st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})
        if g.get("half"):
            continue
        ctx = getctx()
        if ctx is None:
            g["half"] = True
            continue
        summary = fetch(f"football/nfl/summary?event={e['id']}")
        if post_prime_half(e, summary, ctx):
            g["half"] = True
    return _nfl_prime_slot_base(slot, st, getctx, force)


_PROJ = {}


def nfl_projections(week):
    """Sleeper projections for the week, scored with the league's own settings: {sleeper pid: pts}."""
    if week in _PROJ:
        return _PROJ[week]
    out = {}
    try:
        season = fetch_url(f"{SLEEPER}/state/nfl").get("season")
        rows = fetch_url(f"https://api.sleeper.app/projections/nfl/{season}/{week}?season_type=regular")
        sc = dict(league_scoring("nfl"))
        if "fgm_50p" not in sc:
            sc["fgm_50p"] = sc.get("fgm_50_59", 0)
        for r in rows or []:
            s = r.get("stats") or {}
            out[str(r.get("player_id"))] = sum(float(v or 0) * float(sc[k]) for k, v in s.items()
                                               if k in sc and isinstance(v, (int, float)))
    except Exception as e:  # noqa: BLE001
        log(f"projections unavailable: {e}")
    _PROJ[week] = out
    return out


# ------------------------------------------------ team defences (DEF) in the player lists

def nfl_defenses(summary, event=None):
    """Both team defences with league fantasy points (sacks, takeaways, TDs, safeties, points allowed)."""
    sc = league_scoring("nfl")
    v = lambda k: float(sc.get(k) or 0)  # noqa: E731
    box = summary.get("boxscore", {})
    plist = box.get("players", [])
    if len(plist) != 2:
        return []

    def cat_sum(team, cat, key):
        tot = 0.0
        for c in team.get("statistics", []):
            if c.get("name") == cat:
                keys = c.get("keys", [])
                for a in c.get("athletes", []):
                    s = dict(zip(keys, a.get("stats", [])))
                    tot += num(s.get(key, 0))
        return tot

    scores = {}
    for t in (teams_of(event) if event else []):
        scores[t["team"]["abbreviation"]] = int(num(t.get("score")))
    if not scores:
        hdr = (summary.get("header") or {}).get("competitions", [{}])[0].get("competitors", [])
        scores = {c["team"]["abbreviation"]: int(num(c.get("score"))) for c in hdr}
    out = []
    for i, me in enumerate(plist):
        opp = plist[1 - i]
        ab, oab = me["team"]["abbreviation"], opp["team"]["abbreviation"]
        sacks = cat_sum(me, "defensive", "sacks")
        ints = cat_sum(opp, "passing", "interceptions")
        fum_rec = cat_sum(opp, "fumbles", "fumblesLost")
        ff = cat_sum(opp, "fumbles", "fumbles")
        st_td = cat_sum(me, "kickReturns", "kickReturnTouchdowns") + cat_sum(me, "puntReturns", "puntReturnTouchdowns")
        def_td = safeties = 0
        for p in summary.get("scoringPlays", []):
            if (p.get("team") or {}).get("abbreviation") != ab:
                continue
            ty = (p.get("type") or {})
            if ty.get("abbreviation") == "SF" or "safety" in ty.get("text", "").lower():
                safeties += 1
            elif re.search(r"interception return|fumble return|fumble recovery", p.get("text", ""), re.I):
                def_td += 1
        allowed = scores.get(oab)
        pa = 0.0
        if allowed is not None:
            for lo, hi, k in ((0, 0, "pts_allow_0"), (1, 6, "pts_allow_1_6"), (7, 13, "pts_allow_7_13"),
                              (14, 20, "pts_allow_14_20"), (21, 27, "pts_allow_21_27"), (28, 34, "pts_allow_28_34"),
                              (35, 999, "pts_allow_35p")):
                if lo <= allowed <= hi:
                    pa = v(k)
        pts = (sacks * v("sack") + ints * v("int") + fum_rec * v("fum_rec") + ff * v("ff") + safeties * v("safe")
               + def_td * v("def_td") + st_td * v("def_st_td") + pa)
        bits = []
        if sacks: bits.append(f"{sacks:g} sack{'s' if sacks != 1 else ''}")
        if ints: bits.append(f"{int(ints)} INT")
        if fum_rec: bits.append(f"{int(fum_rec)} fumble rec")
        if def_td + st_td: bits.append(f"{int(def_td + st_td)} TD")
        if safeties: bits.append(f"{safeties} safety")
        if allowed is not None: bits.append(f"{allowed} pts allowed")
        sl = sl_team(ab)
        name = f"{(me['team'].get('displayName') or me['team'].get('name') or ab).split()[-1]} D/ST"
        out.append({"id": f"def:{sl}", "name": name, "team": ab, "pos": "DEF", "pts": pts,
                    "line": " · ".join(bits), "elite": pts >= CONFIG["nfl"]["elite_threshold"]["other"]})
    return out


_nfl_fantasy_k = nfl_fantasy


def nfl_fantasy(summary):
    """Players + kickers + team defences, best first."""
    out = _nfl_fantasy_k(summary)
    try:
        out = out + [d for d in nfl_defenses(summary) if d["pts"]]
    except Exception as e:  # noqa: BLE001
        log(f"defence points unavailable: {e}")
    out.sort(key=lambda x: -x["pts"])
    return out


_nfl_headshot_base = nfl_headshot


def nfl_headshot(pid):
    """Player headshot, or the team logo for a defence ('def:LAR')."""
    if str(pid or "").startswith("def:"):
        ab = str(pid)[4:].lower()
        return f"https://a.espncdn.com/i/teamlogos/nfl/500/{ {'was': 'wsh'}.get(ab, ab) }.png"
    return _nfl_headshot_base(pid)


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

    keepalive = os.environ.get("KEEPALIVE") == "1"   # GitHub: stay on for the whole run, idle-poll slowly
    idle = int(os.environ.get("IDLE_SECONDS", "300"))
    start = time.time()
    last_nba = last_recap = last_push = 0.0
    saved = json.dumps(st, sort_keys=True)
    while True:
        if time.time() - last_recap >= 600:       # weekly recaps (NFL Tue, NBA Mon 13:00) + NFL daily race (14:00)
            for sport in ("nfl", "nba"):
                try:
                    maybe_weekly_recap(st, sport)
                except Exception as ex:  # noqa: BLE001
                    log(f"{sport} weekly recap error: {ex}")
            try:
                maybe_daily_race(st)
            except Exception as ex:  # noqa: BLE001
                log(f"daily playoff race error: {ex}")
            last_recap = time.time()
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
        now_saved = json.dumps(st, sort_keys=True)
        if keepalive and now_saved != saved and time.time() - last_push >= 120:
            push_state()                            # so a crash mid-run can't cause repeat posts
            saved, last_push = now_saved, time.time()
        elapsed = time.time() - start
        if elapsed > MAX_RUNTIME or (not active and not keepalive):
            break
        wait = POLL_SECONDS if active else idle
        time.sleep(max(5, min(wait, MAX_RUNTIME - elapsed + 1)))
    log("Done.")


def push_state():
    """Commit state.json back to the repo (GitHub Actions only)."""
    import subprocess
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    cmds = [["git", "config", "user.name", "sports-bot"],
            ["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"],
            ["git", "add", "state.json"],
            ["git", "commit", "-q", "-m", "update state"],
            ["git", "pull", "-q", "--rebase", "-X", "theirs"],
            ["git", "push", "-q"]]
    for c in cmds:
        r = subprocess.run(c, cwd=ROOT, capture_output=True, text=True)
        if r.returncode and c[1] not in ("commit",):
            log(f"state push: {' '.join(c[:2])} failed: {r.stderr.strip()[:200]}")
            return


if __name__ == "__main__":
    main()
