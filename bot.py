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
TD_PAT_WAIT = 0          # seconds to wait for the extra point to be added to a TD (0 = post straight away)

SILENT = False           # True on the very first run: mark everything as seen, post nothing
_POSTER = {}              # per-post webhook name override (NBA posts use their own name)
_LAST_MSG = {}            # id + channel of the last webhook message (Discord returns it with ?wait=true)
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
            CAPTURE.append({"content": content, "embeds": ems, "attachments": atts, "username": _POSTER.get("name")})
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
    payload = {"username": _POSTER.get("name") or who.get("name", "Donald Claude"), "embeds": embeds[:10]}
    if who.get("avatar_url"):
        payload["avatar_url"] = who["avatar_url"]
    if content:
        payload["content"] = content
        roles = re.findall(r"<@&(\d+)>", content)
        payload["allowed_mentions"] = {"parse": [], "roles": roles}
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
            hook + ("&" if "?" in hook else "?") + "wait=true", data=body, method="POST",
            headers={"Content-Type": ctype,
                     "User-Agent": "DiscordBot (sports-bot, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                try:
                    m = json.loads(resp.read().decode() or "{}")
                    _LAST_MSG.clear()
                    _LAST_MSG.update({"id": m.get("id"), "channel_id": m.get("channel_id")})
                except Exception:  # noqa: BLE001
                    pass
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
        if pts == 0:
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
                if not force and state == "in" and not td_ready(summary, p, first):
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
        trs.append(f"<div class='tr{' cut' if i == n_po else ''}'><span class='rk5'>{i}</span>"
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
.gl{height:auto!important;row-gap:8px!important;padding:8px 12px!important}
.gl .tp{grid-column:1/-1;display:flex;flex-direction:column;gap:4px;margin-top:2px}
.tpr{display:grid;grid-template-columns:24px 196px 56px 232px 1fr;align-items:center;gap:10px;font-size:14px;
 background:rgba(255,255,255,.045);border-radius:8px;padding:5px 10px}
.tpr.one{background:rgba(255,179,71,.12)}
.tpr .rk5{text-align:center;font-weight:900;color:#c3cad8}
.tpr .nm5{font-weight:900;color:#fff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tpr .nm5 small{font-size:11px;color:#a89a8a;font-weight:800;margin-left:5px}
.tpr .fp5{text-align:center;font-weight:900;color:#0d1220;background:#7ee2a8;border-radius:6px;padding:1px 0}
.tpr.one .fp5{background:linear-gradient(90deg,#ffd166,#ffb347)}
.tpr .ow5{font-weight:800;color:#e8e2da;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tpr .ow5 em{font-style:normal;font-size:10px;letter-spacing:1px;margin-left:6px;padding:1px 5px;border-radius:4px}
.tpr .ow5 em.s{background:rgba(31,170,89,.25);color:#7ee2a8}.tpr .ow5 em.b{background:rgba(255,255,255,.1);color:#a89a8a}
.tpr .ln5{font-size:12.5px;color:#c9bfb3;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
                   f"{pj_box(r['fp'], pregame_proj('nba', r, live, event), r['elite'], game_frac(event, 'nba'))}"
                   f"<div><div class='ow'>{H.escape(r['owner'])}</div>"
                   f"<span class='bd {'st' if r['starting'] else 'bn'}'>{'STARTING' if r['starting'] else 'BENCH'}</span></div>"
                   f"{mt}</div>")
    if fa and fa["fp"] >= 15:
        raw = fa["raw"]
        prs.append(f"<div class='pr2'><img src='{H.escape(fa['hs'])}'>"
                   f"<div><div class='nm2'>{H.escape(fa['name'])}<span>{fa['team']}</span></div>"
                   f"<div class='ln'>{raw['pts']} pts · {raw['reb']} reb · {raw['ast']} ast</div></div>"
                   f"{pj_box(fa['fp'], pregame_proj('nba', fa, live, event), False, game_frac(event, 'nba'))}"
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
    return _card(f"--a:{ca};--h:{ch}", body, 64 + 70 + n * 68 + (28 if more else 0) + 8)


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
        starters_fp = sum(r["fp"] for r in owned if r["starting"])
        f1 = (f"<span class='m'><b>{len(owned)}</b> league players · <b>{fmt_pts(starters_fp)}</b> fpts from starters</span>"
              if owned else "<span class='m'>No league players</span>")
        tops = []
        for i, r in enumerate(owned[:3]):
            raw = r.get("raw") or {}
            bits = [f"{raw.get('pts', 0)} pts", f"{raw.get('reb', 0)} reb", f"{raw.get('ast', 0)} ast"]
            for k, lab in (("stl", "stl"), ("blk", "blk")):
                if raw.get(k):
                    bits.append(f"{raw[k]} {lab}")
            if raw.get("tpm", 0) >= 3:
                bits.append(f"{raw['tpm']} 3pm")
            tops.append(f"<div class='tpr{' one' if i == 0 else ''}'><span class='rk5'>{'⭐' if i == 0 else i + 1}</span>"
                        f"<span class='nm5'>{H.escape(r['name'])}<small>{H.escape(r.get('team', ''))}</small></span>"
                        f"<span class='fp5'>{fmt_pts(r['fp'])}</span>{pj_small(r['fp'], pregame_proj('nba', r, live, e), game_frac(e, 'nba'))}"
                        f"<span class='ow5'>{H.escape(r['owner'])}<em class='{'s' if r['starting'] else 'b'}'>"
                        f"{'STARTING' if r['starting'] else 'BENCH'}</em></span>"
                        f"<span class='ln5'>{' · '.join(bits)}</span></div>")
        tp = f"<div class='tp'>{''.join(tops)}</div>" if tops else ""
        stt = "FINAL" if e["status"]["type"].get("completed") else game_status(e)
        lines.append(f"<div class='gl'><div class='sc2'><img src='{H.escape(away['team'].get('logo', ''))}'>"
                     f"<span>{away['team']['abbreviation']}</span><b class='r' style='{'' if a_s > h_s else 'color:#8b95a8'}'>{a_s}</b>"
                     f"<span>–</span><b style='{'' if h_s > a_s else 'color:#8b95a8'}'>{h_s}</b>"
                     f"<span class='r'>{home['team']['abbreviation']}</span><img src='{H.escape(home['team'].get('logo', ''))}'></div>"
                     f"<div class='st {'fin' if stt == 'FINAL' else 'live'}'>{H.escape(stt)}</div><div class='f1'>{f1}</div>{tp}</div>")
    lg = ""
    if live and live["pairs"] and CONFIG["nba"].get("race_panel", False):
        try:
            lg = nba_race_html(events, summaries, live)
        except Exception as ex:  # noqa: BLE001
            log(f"NBA race panel unavailable: {ex}")
    if not lg and live and live["pairs"] and CONFIG["nba"].get("matchup_panel", False):
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
    for ev_, s in zip(events, sums):
        for r in (nba_league_rows(s) if s else []):
            if r["owner"] and (best is None or r["fp"] > best["fp"]):
                best = dict(r, _event=ev_)
    embed = {"author": {"name": f"NBA · {CONFIG['nba'].get('league_name', 'League')} league", "icon_url": NBA_LOGO},
             "color": 0x17408B}
    if best:
        embed["title"] = f"Best {'so far' if mode == 'HALF-TIME' else 'of the bunch'}: {best['name']} " \
                         f"{fmt_pts(best['fp'])}" + \
                         (lambda pj: f" (proj {pj:.1f})" if pj is not None else "")(pregame_proj('nba', best, live, best.get('_event'))) + \
                         f" · {best['owner']}"
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


_NBA_LIVE = {"on": False}       # any NBA game live (or just finished and not posted): check every 20s, not every 5 min


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
    _NBA_LIVE["on"] = bool(live_games) or any(
        e["status"]["type"]["state"] == "post" and not G(e).get("posted") for e in events)

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
        if not force and now_s < G(e).setdefault("ft_seen", now_s) + spoiler_delay():
            continue
        finals.append(e)
    if finals:
        first = st.setdefault("nba_ft_wait", now_s)
        # only hold for a game that's really about to finish: 4th quarter / OT with <= 1:00 on the clock (max 10 minutes)
        cfg = CONFIG.get("nba") or {}
        closing = [e for e in live_games if e["status"].get("period", 0) >= 4
                   and clock_secs(e) <= float(cfg.get("ft_group_if_clock_under_secs", 60))]
        if force or not closing or now_s - first >= float(cfg.get("ft_group_max_wait_minutes", 10)) * 60:
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
            if now_s >= g.setdefault("ht_seen", now_s) + spoiler_delay():
                at_half.append(e)
            else:
                approaching.append(e)
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
    mu = {}
    try:                                       # projected finals (current scores mean little mid-week)
        proj = nfl_projections(live["week"])
        for ra, rb in live["pairs"]:
            pa, pb = _proj_total(ra, live, ol, event, proj), _proj_total(rb, live, ol, event, proj)
            mu[live["rosters"][ra]["manager"]] = (pa, live["rosters"][rb]["manager"], pb)
            mu[live["rosters"][rb]["manager"]] = (pb, live["rosters"][ra]["manager"], pa)
    except Exception as ex:  # noqa: BLE001
        log(f"projected matchups unavailable: {ex}")
        mu = nba_matchup_lookup(live)
    prs = []
    for i, r in enumerate(owned):
        m = mu.get(r["owner"])
        mt = (f"<div class='mt'>proj <b class='{'w' if m[0] >= m[2] else 'l'}'>{m[0]:.1f}</b> – {m[2]:.1f}<br>vs {H.escape(m[1])}</div>"
              if m else "")
        prs.append(f"<div class='pr2{' lead' if i == 0 else ''}'><img src='{H.escape(nfl_headshot(r.get('id')))}'>"
                   f"<div><div class='nm2'>{H.escape(r['name'])}<span>{r['team']} · {r.get('pos', '')}</span></div>"
                   f"<div class='ln'>{H.escape(r['line'])}</div></div>"
                   f"{pj_box(r['pts'], pregame_proj('nfl', r, live), r['elite'], game_frac(event, 'nfl'))}"
                   f"<div><div class='ow'>{H.escape(r['owner'])}</div>"
                   f"<span class='bd {'st' if r['starting'] else 'bn'}'>{'STARTING' if r['starting'] else 'BENCH'}</span></div>{mt}</div>")
    if fa and fa["pts"] >= 12:
        prs.append(f"<div class='pr2'><img src='{H.escape(nfl_headshot(fa.get('id')))}'>"
                   f"<div><div class='nm2'>{H.escape(fa['name'])}<span>{fa['team']} · {fa.get('pos', '')}</span></div>"
                   f"<div class='ln'>{H.escape(fa['line'])}</div></div>{pj_box(fa['pts'], pregame_proj('nfl', fa, live), False, game_frac(event, 'nfl'))}"
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
    if event["status"].get("period", 0) > 4 and event["status"]["type"].get("state") == "post":
        body = body.replace("<div class='pill'>FULL-TIME</div>", "<div class='pill'>FULL-TIME · OT</div>", 1)
    return _card(f"--a:{ca};--h:{ch}", body, 64 + 70 + len(prs) * 68 + (60 + ((len(sub) + 1) // 2) * 24 if panel else 0) + 14,
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


def espn_topup(live, events):
    """Sleeper's live points can trail ESPN by minutes: for games in progress, use the ESPN-based
    league score when it's higher, and lift each roster's total to match."""
    npid = nfl_name_pid()
    for e in events:
        if e["status"]["type"]["state"] != "in":
            continue
        try:
            s = fetch(f"football/nfl/summary?event={e['id']}")
            pts = {}
            for p in nfl_fantasy(s):
                pid = (p["id"][4:] if str(p.get("id", "")).startswith("def:")
                       else npid.get((name_key(p["name"]), sl_team(p["team"]))))
                if pid:
                    pts[pid] = p["pts"]
        except Exception as ex:  # noqa: BLE001
            log(f"ESPN top-up unavailable: {ex}")
            continue
        for x in live["rosters"].values():
            for pid, v in pts.items():
                if pid in x["players"]:
                    old = float(x["pp"].get(pid) or 0)
                    if v > old + 0.05:
                        x["pp"][pid] = v
                        if pid in x["starters"]:
                            x["points"] += v - old
    return live


def nfl_ctx(st, events):
    """Live league context: Sleeper week, matchups, live outlook and the week-start baseline."""
    live = league_live("nfl")
    if not live or not live.get("pairs"):
        return None
    live = espn_topup(live, events)
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
    if event["status"]["type"].get("state") == "post" and (play.get("period") or {}).get("number", 0) > 4:
        lead_txt, tag = f"{nick} win it in overtime!", "WALK-OFF"
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
             "img_title": _td_headline(td, nick)}
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
                if not td_ready(summary, p, first):
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
            if not force and not event_ready(g, "ft_seen", summary):
                active = True
                continue
            # another game in this slot in its last 2:00 of the 4th (or OT)? hold so they post together (max 10 min)
            closing = [o for o in slot if o is not e and o["status"]["type"]["state"] == "in"
                       and o["status"].get("period", 0) >= 4 and clock_secs(o) <= 120]
            if closing and not force and time.time() - g.setdefault("ft_hold", time.time()) < 600:
                active = True
                continue
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
    if now < mark + timedelta(seconds=spoiler_delay()):
        return True
    cut = now - timedelta(seconds=spoiler_delay())
    ctx = getctx()
    if not ctx:
        return True
    live, ol = ctx["live"], ctx["ol"]
    since = datetime.strptime(s.get("last") or first.strftime("%Y-%m-%dT%H:%M:%SZ"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    scoring = scoring_since(win, since, cut)
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
        s["last"] = cut.strftime("%Y-%m-%dT%H:%M:%SZ")
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


def _no_panel(fn, *a, **k):
    """Build a game card without the playoff-odds panel (half-time / overtime: the players matter more)."""
    global playoff_panel_html
    keep = playoff_panel_html
    playoff_panel_html = lambda *x, **y: ""  # noqa: E731
    try:
        return fn(*a, **k)
    finally:
        playoff_panel_html = keep


def nfl_half_html(event, summary, live, ol, base):
    """Half-time: every league player in the game with points so far (no playoff panel)."""
    h = _no_panel(nfl_game_fantasy_html, event, summary, live, ol, base)
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
        summary = fetch(f"football/nfl/summary?event={e['id']}")
        if not event_ready(g, "half_seen", summary, period=2):
            continue
        ctx = getctx()
        if ctx is None:
            g["half"] = True
            continue
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
        out = out + [d for d in nfl_defenses(summary, None, summary.get("_week")) if d["pts"]]
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


# ------------------------------------------------ spoiler delay (people watching on a delay)

def spoiler_delay():
    return float(CONFIG.get("spoiler_delay_seconds", 120))


def _wallclock_ts(wc):
    return datetime.strptime(wc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def td_ready(summary, play, first_seen):
    """True once the touchdown is at least spoiler_delay() old (ESPN's time of the play, else first seen)."""
    t0 = first_seen
    try:
        _, d = play_detail(summary, play.get("id"))
        if (d or {}).get("wallclock"):
            t0 = _wallclock_ts(d["wallclock"])
    except Exception:  # noqa: BLE001
        pass
    return time.time() >= t0 + spoiler_delay()


def event_ready(g, key, summary=None, period=None):
    """True once a half-time / full-time moment is at least spoiler_delay() old.
    Moment = time of the last play (of `period` if given) from ESPN, or when the bot first noticed it."""
    t0 = g.setdefault(key, time.time())
    try:
        drives = (summary or {}).get("drives") or {}
        pool = list(drives.get("previous") or []) + ([drives["current"]] if drives.get("current") else [])
        wcs = [_wallclock_ts(p["wallclock"]) for d in pool for p in d.get("plays") or []
               if p.get("wallclock") and (period is None or (p.get("period") or {}).get("number") == period)]
        if wcs:
            t0 = min(t0, max(wcs))
    except Exception:  # noqa: BLE001
        pass
    return time.time() >= t0 + spoiler_delay()


# ------------------------------------------------ v5 touchdown card: conversion strip, projections, win swing

TD5_CSS = """
.pair{gap:10px!important;margin-top:8px!important}
.pp5{display:grid;grid-template-columns:72px 1fr;gap:12px;align-items:center;background:rgba(255,255,255,.06);border-radius:12px;padding:9px 10px}
.pp5 img{width:72px;height:72px;border-radius:10px;object-fit:cover;object-position:top;background:rgba(255,255,255,.08)}
.pp5 .role{font-size:10px;letter-spacing:1.4px;color:#9aa3b5;font-weight:700}
.pp5 .nm{font-size:17px;font-weight:900;line-height:1.1;margin-top:1px}
.pp5 .gn{display:flex;align-items:baseline;gap:8px;margin-top:2px}
.pp5 .gn b{font-size:24px;font-weight:900;color:#7ee2a8;line-height:1}
.pp5 .gn span{font-size:12.5px;color:#cfd5e1;font-weight:700}.pp5 .gn span em{font-style:normal;color:#fff;font-size:16px;font-weight:900}
.pb5{display:grid;grid-template-columns:1fr auto;align-items:center;gap:8px;margin-top:5px}
.pb5 .bar{height:5px;border-radius:999px;background:rgba(255,255,255,.1);overflow:hidden}
.pb5 .bar div{height:5px;border-radius:999px;background:linear-gradient(90deg,#d97757,#f2a77f)}
.pb5 .bar div.over{background:linear-gradient(90deg,#1faa59,#7ee2a8)}
.pb5 span{font-size:11px;color:#9aa3b5;font-weight:700;white-space:nowrap}.pb5 span b{color:#e6e9ef}
.pp5 .ow3{font-size:12.5px;color:#cfd5e1;margin-top:3px}.pp5 .ow3 b{color:#fff}
.cv{display:flex;align-items:center;gap:9px;margin-top:8px;background:rgba(255,255,255,.045);border-radius:10px;padding:6px 12px;
 font-size:13px;color:#cfd5e1;flex-wrap:wrap}
.cv .tg{font-size:10px;letter-spacing:1.3px;font-weight:800;color:#9aa3b5}
.cv .ok{color:#7ee2a8;font-weight:900;font-size:10.5px;letter-spacing:1px}.cv .no{color:#ff9a9e;font-weight:900;font-size:10.5px;letter-spacing:1px}
.cv b{color:#fff}.cv .g{color:#7ee2a8;font-weight:900}.cv .sep{color:#5d6679}
.cv .cvw{white-space:nowrap;font-size:13px;font-weight:600;color:#cfd5e1}
.mu5{margin-top:8px;background:rgba(255,255,255,.055);border-radius:12px;padding:8px 14px}
.mu5 .row{display:grid;grid-template-columns:1fr 250px 1fr;align-items:center;gap:12px}
.mu5 .nm{font-size:16px;font-weight:900;text-transform:none;letter-spacing:0}.mu5 .r{text-align:right}
.mu5 .pj{font-size:11.5px;color:#9aa3b5;font-weight:700;margin-top:1px}.mu5 .pj b{color:#e6e9ef;font-size:12.5px}
.mu5 .po{font-size:11.5px;color:#9aa3b5;font-weight:700;margin-top:2px}.mu5 .po b{color:#fff}
.mu5 .po .g{color:#7ee2a8}.mu5 .po .b{color:#ff9a9e}.mu5 .po .u{color:#7ee2a8}.mu5 .po .dn{color:#ff9a9e}
.mu5 .sc{text-align:center;font-size:20px;font-weight:900;font-variant-numeric:tabular-nums;margin-bottom:3px}.mu5 .sc .lo{color:#8b95a8}
.mu5 .wcx{text-align:center;font-size:10px;letter-spacing:1px;color:#9aa3b5;font-weight:700;margin-top:3px}
.mu5 .wcx .u{color:#7ee2a8}.mu5 .wcx .dn{color:#ff9a9e}
.pp5.hot{box-shadow:0 0 0 2px #ffd166,0 0 26px rgba(255,190,60,.45)!important;background:linear-gradient(120deg,rgba(60,42,8,.85),rgba(9,12,22,.75))!important}
.pb5.win .bar{height:10px;background:rgba(255,209,102,.15);box-shadow:0 0 14px rgba(255,190,60,.75)}
.pb5.win .bar div{height:10px;background:linear-gradient(90deg,#ff9f1c,#ffd166 40%,#fff3b0 55%,#ffd166 70%,#ff9f1c)!important}
.pb5 .beat{background:linear-gradient(90deg,#ffd166,#ff9f1c);color:#1b1305!important;padding:3px 10px;border-radius:8px;
 font-weight:900;font-size:12.5px!important;letter-spacing:.6px;box-shadow:0 0 12px rgba(255,190,60,.6)}
.pjn{font-size:12px;color:#c3cad8;font-weight:700;margin-top:3px}.pjn b{font-size:13.5px}.pjn .up{color:#ffd166}.pjn .dn{color:#ff9a9e}
.pp5,.cv,.imp{background:rgba(9,12,22,.62)!important}
.pp5 .role,.cv .tg,.imp .ih{color:#c3cad8!important}
.pp5 .ow3,.pp5 .gn span,.cv,.pb5 span{color:#e3e7ef!important}
.sec{color:#e3e7ef!important}
.imp{margin-top:8px;border-radius:12px;padding:8px 14px}
.imp .ih{font-size:10.5px;letter-spacing:1.4px;font-weight:800;margin-bottom:2px}
.im{display:grid;grid-template-columns:18px 175px 1fr 110px 120px;align-items:center;gap:10px;font-size:14px;color:#e3e7ef;
 padding:5px 0;border-top:1px solid rgba(255,255,255,.07)}
.ih + .im{border-top:none}
.im b{color:#fff}.im .who5{font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.im .lo{color:#aab2c2}.im .ar{font-size:12px;text-align:center}.im .u{color:#7ee2a8}.im .dn{color:#ff9a9e}
.mu5 .tc{font-size:11.5px;color:#9aa3b5;margin-top:3px;white-space:nowrap}.mu5 .tc b{color:#e6e9ef;font-weight:700}.mu5 .tc span{color:#6f788b}
"""

_NFL_ST = {}


def vivid(h, min_l=0.36, min_s=0.6):
    """Team colour made bold enough to dominate a dark card (keeps the hue, lifts lightness/saturation)."""
    import colorsys
    r, g, b = (x / 255 for x in hex_rgb(h))
    hh, ll, ss = colorsys.rgb_to_hls(r, g, b)
    ll = min(0.62, max(ll, min_l))
    ss = max(ss, min_s) if ss > 0.08 else ss
    r, g, b = colorsys.hls_to_rgb(hh, ll, ss)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def _sl_pid(name, espn_team):
    return nfl_name_pid().get((name_key(name), sl_team(espn_team)))


def _proj_total(r, live, ol, event, proj):
    """Projected final: points so far + projection for what's left (in-game players: the remaining share)."""
    players = sleeper_players("nfl")
    here = {sl_team(t["team"]["abbreviation"]) for t in teams_of(event)}
    rem = max(0.0, 1 - game_elapsed(event))
    tot = ol[r]["cur"]
    for p in ol[r]["left"]:
        pj = proj.get(str(p), 0.0)
        tot += pj * rem if ((players.get(p) or {}).get("team") or p) in here else pj
    return tot


def _conversion_html(play, summary, team_abbr):
    import html as H
    m = re.search(r"\(([^()]*)\)\s*$", play.get("text") or "")
    if not m:
        return ""
    pat = m.group(1)
    sc = league_scoring("nfl")
    failed = bool(re.search(r"fail|miss|block|no good", pat, re.I))
    mark = "<span class='no'>NO GOOD</span>" if failed else "<span class='ok'>GOOD</span>"

    def who_html(name, gain):
        o = league_owner(boxscore_id(summary, name), name)
        now = player_now(summary, name)
        own = (f"{H.escape(o['manager'])}{'' if o['starting'] else ' (bench)'}" if o else "free agent")
        return (f"<span class='cvw'><b>{H.escape(name)}</b> <span class='g'>+{gain:g}</span>"
                + (f" · now {fmt_pts(now)}" if now is not None else "") + f" <span class='sep'>·</span> {own}</span>")

    if re.search(r"two-point|2pt|2-pt", pat, re.I):
        mm = re.match(r"^(.+?)\s+pass to\s+(.+?)\s+for two-point", pat, re.I)
        if mm:
            g1, g2 = (0, 0) if failed else (float(sc.get("pass_2pt") or 0), float(sc.get("rec_2pt") or 0))
            body = f"{who_html(mm.group(1).strip(), g1)} <span class='sep'>➜</span> {who_html(mm.group(2).strip(), g2)}"
        else:
            mm = re.match(r"^(.+?)\s+(?:run|rush)\s+for two-point", pat, re.I)
            body = who_html(mm.group(1).strip(), 0 if failed else float(sc.get("rush_2pt") or 0)) if mm else H.escape(pat)
        return f"<div class='cv'><span class='tg'>2-PT CONVERSION</span>{mark}{body}</div>"
    mm = re.match(r"^(.+?)\s+(?:kick|pat|extra point)\b", pat, re.I)
    name = mm.group(1).strip() if mm else ""
    if not name:
        return ""
    k = next((x for x in nfl_kickers(summary) if name_key(x["name"]) == name_key(name)), None)
    o = league_owner(k["id"] if k else boxscore_id(summary, name), name)
    gain = float(sc.get("xpmiss") or 0) if failed else float(sc.get("xpm") or 0)
    own = (f"{H.escape(o['manager'])}{'' if o['starting'] else ' (bench)'}" if o else "free agent")
    return (f"<div class='cv'><span class='tg'>EXTRA POINT</span>{mark}<b>{H.escape(name)}</b>"
            f"<span class='g'>{'+' if gain >= 0 else ''}{gain:g}</span>"
            + (f" · now {fmt_pts(k['pts'])} pts" if k else "") + f" <span class='sep'>·</span> {own}</div>")


_nfl_td_fantasy_html_v4 = nfl_td_fantasy_html


def nfl_td_fantasy_html(event, play, summary, live, ol, base, tag):
    """v5 touchdown card (switched on with nfl.td_card_v5 in config.json)."""
    if not CONFIG["nfl"].get("td_card_v5"):
        return _nfl_td_fantasy_html_v4(event, play, summary, live, ol, base, tag)
    import html as H
    away, home = teams_of(event)
    td = td_parse(play)
    abbr = play["team"]["abbreviation"]
    scorer_team = away if abbr == away["team"]["abbreviation"] else home
    proj = nfl_projections(live["week"])
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    q = play.get("period", {}).get("number", 0)
    st = _NFL_ST.get("st") or {}
    prev_win = st.get("nfl_td_win") or {}

    def panel(role, name, gain_):
        pid = boxscore_id(summary, name)
        o = league_owner(pid, name)
        hs = nfl_headshot(pid) or scorer_team["team"].get("logo", "")
        now = player_now(summary, name)
        pj = proj.get(str(_sl_pid(name, abbr) or ""), 0.0)
        bar = ""
        if pj and now is not None:
            bar = proj_bar(now, pj, 1 - game_elapsed(event))
        own = (f"<b>{H.escape(o['manager'])}</b> {'⭐ starting' if o['starting'] else '· bench'}" if o
               else "<b>Unrostered</b> — free agent")
        tot = f"<span>now <em>{fmt_pts(now)}</em> pts</span>" if now is not None else ""
        hot = " hot" if pj and now is not None and now >= pj else ""
        return o, (f"<div class='pp5{hot}'><img src='{H.escape(hs)}'><div><div class='role'>{role}</div>"
                   f"<div class='nm'>{H.escape(name)}</div><div class='gn'><b>+{gain_:.1f}</b>{tot}</div>{bar}"
                   f"<div class='ow3'>{own}</div></div></div>")

    pid0 = boxscore_id(summary, td["scorer"])
    o0 = league_owner(pid0, td["scorer"])
    gain, qb_gain = play_fpts(td, (o0 or {}).get("pos", ""))
    owners = []
    if td["kind"] == "pass":
        what = f"{td['yds']}-yard touchdown pass"
        o1, p1 = panel("RECEIVER", td["scorer"], gain)
        o2, p2 = panel("QUARTERBACK", td["qb"], qb_gain)
        panels, cls = p1 + p2, "pair"
        owners = [o1, o2]
    else:
        what = (f"{td['yds']}-yard touchdown run" if td["kind"] == "rush" else "defensive / return touchdown")
        o1, p1 = panel("SCORER", td["scorer"], gain)
        panels, cls = p1, "pair one"
        owners = [o1]

    def impact_row(o):
        rid = next((r for r, x in live["rosters"].items() if x["manager"] == o["manager"]), None)
        opp = next((b if a == rid else a for a, b in live["pairs"] if rid in (a, b)), None) if rid else None
        if not opp:
            return ""
        me, op = ol[rid], ol[opp]
        pw = prev_win.get(str(rid))
        sw = ""
        if pw is not None and abs(me["win"] - pw) >= 1:
            d = me["win"] - pw
            sw = f" <span class='{'u' if d > 0 else 'dn'}'>{'▲' if d > 0 else '▼'}{abs(d):.0f}</span>"
        on = H.escape(live["rosters"][opp]["manager"])
        pa, pb = _proj_total(rid, live, ol, event, proj), _proj_total(opp, live, ol, event, proj)
        cls = "u" if pa >= pb else "dn"
        return (f"<div class='im'><span class='ar u'>▲</span><b class='who5'>{H.escape(o['manager'])}</b>"
                f"<span>projected <b class='{cls}'>{pa:.1f}</b> – {pb:.1f} vs {on}</span>"
                f"<span>win <b>{me['win']:.0f}%</b>{sw}</span>"
                f"<span>playoffs <b>{me['po']:.0f}%</b></span></div>")

    def defence_row():
        """The defence that just conceded loses points-allowed points."""
        con = home if scorer_team is away else away
        pid = sl_team(con["team"]["abbreviation"])
        o = league_owner(f"def:{pid}", "")
        if not o:
            return ""
        sc = league_scoring("nfl")
        pat = re.search(r"\(([^()]*)\)\s*$", play.get("text") or "")
        extra = 0
        if pat and not re.search(r"fail|miss|block|no good", pat.group(1), re.I):
            extra = 2 if re.search(r"two-point|2pt|2-pt", pat.group(1), re.I) else 1
        after = a_s if scorer_team is away else h_s
        before = after - 6 - extra

        def tier(n):
            for lo, hi, k in ((0, 0, "pts_allow_0"), (1, 6, "pts_allow_1_6"), (7, 13, "pts_allow_7_13"),
                              (14, 20, "pts_allow_14_20"), (21, 27, "pts_allow_21_27"), (28, 34, "pts_allow_28_34"),
                              (35, 999, "pts_allow_35p")):
                if lo <= n <= hi:
                    return float(sc.get(k) or 0)
            return 0.0
        d = tier(after) - tier(before)
        if abs(d) < 0.05:
            return ""
        nick = con["team"].get("name") or con["team"]["abbreviation"]
        dn = next((x for x in nfl_defenses(summary, event) if x["team"] == con["team"]["abbreviation"]), None)
        now_txt = f" → now <b>{fmt_pts(dn['pts'])}</b> pts" if dn else ""
        return (f"<div class='im'><span class='ar dn'>▼</span><b class='who5'>{H.escape(o['manager'])}</b>"
                f"<span>{H.escape(nick)} D/ST loses <b class='dn'>{abs(d):g}</b>{now_txt} ({after} allowed)</span>"
                f"<span>{'starting' if o['starting'] else 'bench'}</span><span></span></div>")

    seen, rows = set(), []
    for o in owners:
        if o and o["manager"] not in seen and (o["starting"] or not rows):
            seen.add(o["manager"])
            rows.append(impact_row(o))
    rows.append(defence_row())
    if not owners or not owners[0]:
        rows.insert(0, f"<div class='im'><span class='ar'>🆓</span><b class='who5'>{H.escape(td['scorer'])}</b>"
                       f"<span>is unrostered — available on waivers</span><span></span><span></span></div>")
    rows = [r for r in rows if r]
    mus = [f"<div class='imp'><div class='ih'>FANTASY IMPACT</div>{''.join(rows)}</div>"] if rows else []
    st["nfl_td_win"] = {str(r): v["win"] for r, v in ol.items()}

    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>TOUCHDOWN · {'OT' if q > 4 else 'Q' + str(q)} "
            f"{play.get('clock', {}).get('displayValue', '')} · {away['team']['abbreviation']} {a_s}–{h_s} "
            f"{home['team']['abbreviation']}</div><div class='pill'>{H.escape(tag)}</div></div>"
            + score_hero(event, a_s, h_s, f"{'OT' if q > 4 else 'Q' + str(q)} · {play.get('clock', {}).get('displayValue', '')}",
                         tag, hot=abbr, plus=(a_s + h_s) - _pre_total(play, a_s, h_s, abbr, away), big=_td_yards_label(td)) +
            f"<div class='{cls}' style='margin-top:12px'>{panels}</div>{_conversion_html(play, summary, abbr)}{''.join(mus)}")
    t = scorer_team["team"]
    other = (home if scorer_team is away else away)["team"]
    ca = vivid(t.get("color") or "1faa59")
    ch = vivid(t.get("alternateColor") or t.get("color") or "5b7bd5", min_l=0.5)
    co = vivid(other.get("color") or "5b7bd5")
    bg = (f".bg{{opacity:1!important;background:"
          f"linear-gradient(120deg,{ca}d9 0%,{ca}73 38%,rgba(13,18,32,0) 78%),"
          f"linear-gradient(300deg,{co}38 0%,rgba(13,18,32,0) 22%)!important}}"
          f".hl{{border-left-color:{ch}!important}}")
    return _card(f"--a:{ca};--h:{ch}", body, extra_css=V4_CSS + TD5_CSS + SB_CSS + bg)


_nfl_prime_slot_v5 = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    _NFL_ST["st"] = st
    return _nfl_prime_slot_v5(slot, st, getctx, force)


# ------------------------------------------------ clutch field goals (one-score game, final minute / overtime)

FG_CSS = """
.sb5{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:18px;margin-top:10px;background:rgba(9,12,22,.62);
 border-radius:14px;padding:12px 18px}
.sb5 .tm{display:flex;align-items:center;gap:12px;font-size:20px;font-weight:900;color:#e3e7ef}
.sb5 .tm.r{justify-content:flex-end}
.sb5 .tm img{width:52px;height:52px;object-fit:contain}
.sb5 .sc{font-size:46px;font-weight:900;font-variant-numeric:tabular-nums;line-height:1}
.sb5 .sc.lo{color:#8b95a8}
.sb5 .mid{text-align:center}
.sb5 .clk{font-size:13px;letter-spacing:1.5px;font-weight:800;color:#ffd166}
.sb5 .wp{font-size:12px;color:#c3cad8;font-weight:700;margin-top:4px}
"""


def clutch_fg(play, event):
    """A field goal kicked when the game was within 3, with under a minute left in the 4th (or any in overtime)."""
    if (play.get("type") or {}).get("abbreviation") != "FG":
        return False
    q = (play.get("period") or {}).get("number", 0)
    try:
        m, s = (play.get("clock") or {}).get("displayValue", "15:00").split(":")
        left = int(m) * 60 + int(s)
    except ValueError:
        left = 900
    if q > 4:
        return bool(CONFIG["nfl"].get("overtime_posts"))
    if not CONFIG["nfl"].get("clutch_field_goals") or q < 4 or left > 60:
        return False
    away, _ = teams_of(event)
    a, h = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    before = (a - 3) - h if play["team"]["abbreviation"] == away["team"]["abbreviation"] else a - (h - 3)
    return abs(before) <= 3


def nfl_fg_html(event, play, summary, live, ol, base):
    import html as H
    away, home = teams_of(event)
    abbr = play["team"]["abbreviation"]
    kt = away if abbr == away["team"]["abbreviation"] else home
    other = home if kt is away else away
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    q = play.get("period", {}).get("number", 0)
    clock = play.get("clock", {}).get("displayValue", "")
    m = re.match(r"^(.+?) (\d+) Yd Field Goal", play.get("text") or "")
    kicker, yds = (m.group(1), int(m.group(2))) if m else ("Kicker", 0)
    proj = nfl_projections(live["week"])
    final = event["status"]["type"].get("state") == "post"
    mine, theirs = (a_s, h_s) if kt is away else (h_s, a_s)
    tag = ("WALK-OFF" if final and mine > theirs else "TIES IT" if mine == theirs else
           "GO-AHEAD" if mine > theirs and mine - 3 <= theirs else f"LEAD NOW {mine - theirs}" if mine > theirs
           else f"WITHIN {theirs - mine}")
    wp = ""
    for w in summary.get("winprobability") or []:
        if str(w.get("playId")) == str(play.get("id")):
            hp = float(w.get("homeWinPercentage") or 0) * 100
            fav, pct = (home, hp) if hp >= 50 else (away, 100 - hp)
            wp = f"<div class='wp'>ESPN win chance: {fav['team']['abbreviation']} {pct:.0f}%</div>"

    def side(t, sc, lo, right):
        img = f"<img src='{H.escape(t['team'].get('logo', ''))}'>"
        name = f"<span>{t['team']['abbreviation']}</span>"
        score = f"<span class='sc{' lo' if lo else ''}'>{sc}</span>"
        inner = (score + name + img) if right else (img + name + score)
        return f"<div class='tm{' r' if right else ''}'>{inner}</div>"
    sb = (f"<div class='sb5'>{side(away, a_s, a_s < h_s, False)}"
          f"<div class='mid'><div class='clk'>{'FINAL' if final else ('OT' if q > 4 else 'Q4') + ' · ' + clock + ' LEFT'}</div>{wp}</div>"
          f"{side(home, h_s, h_s < a_s, True)}</div>")

    # kicker panel (same look as the touchdown card)
    k = next((x for x in nfl_kickers(summary) if name_key(x["name"]) == name_key(kicker)), None)
    kid = k["id"] if k else boxscore_id(summary, kicker)
    o = league_owner(kid, kicker)
    gain = kicker_fpts(yds)
    pj = proj.get(str(_sl_pid(kicker, abbr) or ""), 0.0)
    now = k["pts"] if k else None
    bar = ""
    if pj and now is not None:
        bar = proj_bar(now, pj, 1 - game_elapsed(event))
    own = (f"<b>{H.escape(o['manager'])}</b> {'⭐ starting' if o['starting'] else '· bench'}" if o
           else "<b>Unrostered</b> — free agent")
    hot = " hot" if pj and now is not None and now >= pj else ""
    panel = (f"<div class='pair one'><div class='pp5{hot}'><img src='{H.escape(nfl_headshot(kid) or kt['team'].get('logo', ''))}'><div>"
             f"<div class='role'>KICKER · {abbr}</div><div class='nm'>{H.escape(kicker)}</div>"
             f"<div class='gn'><b>+{gain:g}</b>{f'<span>now <em>{fmt_pts(now)}</em> pts</span>' if now is not None else ''}</div>"
             f"{bar}<div class='ow3'>{own}</div></div></div></div>")

    # fantasy impact: kicker's owner + the defence that conceded
    rows = []
    if o:
        rid = next((r for r, x in live["rosters"].items() if x["manager"] == o["manager"]), None)
        opp = next((b if a == rid else a for a, b in live["pairs"] if rid in (a, b)), None) if rid else None
        if opp:
            pa, pb = _proj_total(rid, live, ol, event, proj), _proj_total(opp, live, ol, event, proj)
            rows.append(f"<div class='im'><span class='ar u'>▲</span><b class='who5'>{H.escape(o['manager'])}</b>"
                        f"<span>projected <b class='{'u' if pa >= pb else 'dn'}'>{pa:.1f}</b> – {pb:.1f} vs "
                        f"{H.escape(live['rosters'][opp]['manager'])}</span>"
                        f"<span>win <b>{ol[rid]['win']:.0f}%</b></span><span>playoffs <b>{ol[rid]['po']:.0f}%</b></span></div>")
    od = league_owner(f"def:{sl_team(other['team']['abbreviation'])}", "")
    if od:
        sc = league_scoring("nfl")

        def tier(n):
            for lo, hi, key in ((0, 0, "pts_allow_0"), (1, 6, "pts_allow_1_6"), (7, 13, "pts_allow_7_13"),
                                (14, 20, "pts_allow_14_20"), (21, 27, "pts_allow_21_27"), (28, 34, "pts_allow_28_34"),
                                (35, 999, "pts_allow_35p")):
                if lo <= n <= hi:
                    return float(sc.get(key) or 0)
            return 0.0
        d = tier(mine) - tier(mine - 3)
        if abs(d) >= 0.05:
            dn = next((x for x in nfl_defenses(summary, event) if x["team"] == other["team"]["abbreviation"]), None)
            now_d = f" → now <b>{fmt_pts(dn['pts'])}</b> pts" if dn else ""
            rows.append(f"<div class='im'><span class='ar dn'>▼</span><b class='who5'>{H.escape(od['manager'])}</b>"
                        f"<span>{H.escape(other['team'].get('name') or '')} D/ST loses <b class='dn'>{abs(d):g}</b>"
                        f"{now_d} ({mine} allowed)</span>"
                        f"<span>{'starting' if od['starting'] else 'bench'}</span><span></span></div>")
    if not o:
        rows.insert(0, f"<div class='im'><span class='ar'>🆓</span><b class='who5'>{H.escape(kicker)}</b>"
                       f"<span>is unrostered — {gain:g} pts going spare on waivers</span><span></span><span></span></div>")
    imp = f"<div class='imp'><div class='ih'>FANTASY IMPACT</div>{''.join(rows)}</div>" if rows else ""

    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>FIELD GOAL · {'OT' if q > 4 else 'Q' + str(q)} {clock} · "
            f"{away['team']['abbreviation']} {a_s}–{h_s} {home['team']['abbreviation']}</div>"
            f"<div class='pill'>{tag}</div></div>"
            + score_hero(event, a_s, h_s, ("FINAL" if final else ("OT" if q > 4 else "Q4") + " · " + clock + " LEFT"),
                         tag, hot=abbr, plus=3, big=f"{yds}-YARD FG") +
            f"{panel}{imp}")
    t = kt["team"]
    ca = vivid(t.get("color") or "1faa59")
    ch = vivid(t.get("alternateColor") or t.get("color") or "5b7bd5", min_l=0.5)
    co = vivid(other["team"].get("color") or "5b7bd5")
    bg = (f".bg{{opacity:1!important;background:"
          f"linear-gradient(120deg,{ca}d9 0%,{ca}73 38%,rgba(13,18,32,0) 78%),"
          f"linear-gradient(300deg,{co}38 0%,rgba(13,18,32,0) 22%)!important}}"
          f".hl{{border-left-color:{ch}!important}}")
    return _card(f"--a:{ca};--h:{ch}", body, extra_css=V4_CSS + TD5_CSS + FG_CSS + SB_CSS + bg), tag, kicker, yds, kt, mine, theirs


def post_fg_clutch(event, play, summary, ctx):
    html_text, tag, kicker, yds, kt, mine, theirs = nfl_fg_html(event, play, summary, ctx["live"], ctx["ol"], ctx["base"])
    nick = kt["team"].get("name") or kt["team"]["abbreviation"]
    left = play.get("clock", {}).get("displayValue", "")
    what = {"WALK-OFF": f"{nick} win it {mine}–{theirs}!",
            "TIES IT": f"all square at {mine} with {left} left!",
            "GO-AHEAD": f"{nick} lead {mine}–{theirs} with {left} left!"}.get(tag, f"{nick} {mine}–{theirs} with {left} left")
    embed = {"author": AU_NFL(), "color": color(kt["team"].get("color")),
             "title": f"{kicker} from {yds} — {what}"}
    return _post_card(NFL_HOOK, html_text, f"🏈 FIELD GOAL {nick.upper()}", embed, "fg.png")


_nfl_prime_slot_fg = nfl_prime_slot


def post_overtime(event, summary, ctx):
    """The game is heading to overtime: everyone's points after regulation + projected matchups."""
    away, home = teams_of(event)
    a, h = int(num(away.get("score"))), int(num(home.get("score")))
    html_text = _no_panel(nfl_game_fantasy_html, event, summary, ctx["live"], ctx["ol"], ctx["base"])
    html_text = (html_text.replace("<div class='pill'>FULL-TIME</div>", "<div class='pill'>OVERTIME</div>", 1)
                 .replace("YOUR LEAGUE'S PLAYERS IN THIS GAME", "YOUR LEAGUE'S PLAYERS · POINTS AFTER REGULATION", 1))
    owned = [(p, o) for p in nfl_fantasy(summary) for o in [league_owner(p.get("id"), p["name"])] if o]
    desc = (f"{owned[0][0]['name']} leads the way with {fmt_pts(owned[0][0]['pts'])} for {owned[0][1]['manager']}"
            " · every overtime score gets a post" if owned else "Every overtime score gets a post")
    embed = {"author": AU_NFL(), "color": 0xD97757,
             "title": f"We're going to overtime! {away['team'].get('name')} {a}–{h} {home['team'].get('name')}",
             "description": desc}
    return _post_card(NFL_HOOK, html_text, "⏱️ **OVERTIME**", embed, "ot.png")


def nfl_prime_slot(slot, st, getctx, force=False):
    """+ field goals in a one-score game's final minute, the overtime post, and every overtime score."""
    if CONFIG["nfl"].get("clutch_field_goals") or CONFIG["nfl"].get("overtime_posts"):
        for e in slot:
            if e["status"]["type"]["state"] not in ("in", "post"):
                continue
            g = st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})
            if g.get("final"):
                continue
            q = e["status"].get("period", 0)
            if e["status"]["type"]["state"] == "in" and (q < 4 or (q == 4 and clock_secs(e) > 90)):
                continue
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            if CONFIG["nfl"].get("overtime_posts") and not g.get("ot"):
                tied = int(num(teams_of(e)[0].get("score"))) == int(num(teams_of(e)[1].get("score")))
                end_reg = q == 4 and tied and e["status"]["type"].get("name") == "STATUS_END_PERIOD"
                if (q >= 5 or end_reg) and e["status"]["type"]["state"] == "in" and event_ready(g, "ot_seen", summary):
                    ctx = getctx()
                    if ctx and post_overtime(e, summary, ctx):
                        g["ot"] = True
            if e["status"]["type"]["state"] == "post":
                # game just ended (walk-off): post any touchdown from the last few minutes before full-time
                for p in summary.get("scoringPlays", []):
                    if p["id"] in g["tds"] or (p.get("type") or {}).get("abbreviation") != "TD":
                        continue
                    _, d = play_detail(summary, p.get("id"))
                    wc = (d or {}).get("wallclock")
                    recent = (time.time() - _wallclock_ts(wc) < 900) if wc else (p.get("period") or {}).get("number", 0) > 4
                    if recent and td_ready(summary, p, g.setdefault("seen", {}).setdefault(p["id"], time.time())):
                        ctx = getctx()
                        if ctx and post_td_fantasy(e, p, summary, ctx):
                            g["tds"].append(p["id"])
            for p in summary.get("scoringPlays", []):
                if p["id"] in g["tds"] or not clutch_fg(p, e):
                    continue
                if not td_ready(summary, p, g.setdefault("seen", {}).setdefault(p["id"], time.time())):
                    continue
                ctx = getctx()
                if ctx and post_fg_clutch(e, p, summary, ctx):
                    g["tds"].append(p["id"])
    return _nfl_prime_slot_fg(slot, st, getctx, force)


# ------------------------------------------------ game-changers: turnovers that swing the win probability

GC_CSS = """
.sw5{margin-top:8px;background:rgba(9,12,22,.62);border-radius:14px;padding:12px 18px}
.sw5 .ih{font-size:10.5px;letter-spacing:1.4px;font-weight:800;color:#c3cad8;margin-bottom:6px}
.sw5 .row{display:grid;grid-template-columns:90px 1fr 90px;align-items:center;gap:14px;margin-top:6px}
.sw5 .lb{font-size:12px;letter-spacing:1.2px;font-weight:800;color:#c3cad8}
.sw5 .lb.r{text-align:right}
.sw5 .bar{position:relative;height:14px;border-radius:999px;overflow:hidden;background:rgba(255,255,255,.08);display:flex}
.sw5 .bar div{height:14px}
.sw5 .big{display:flex;align-items:baseline;justify-content:center;gap:14px;font-size:30px;font-weight:900;margin-top:8px;
 font-variant-numeric:tabular-nums}
.sw5 .big .from{color:#8b95a8}.sw5 .big .arr{font-size:22px;color:#c3cad8}
.sw5 .big .d{font-size:18px;font-weight:900}.sw5 .big .d.u{color:#7ee2a8}.sw5 .big .d.dn{color:#ff9a9e}
"""

TURNOVER_TYPES = ("Interception", "Fumble Recovery (Opponent)", "Fumble Return", "Blocked Punt", "Blocked Field Goal",
                  "Blocked FG")


def _all_plays(summary):
    drives = summary.get("drives") or {}
    pool = list(drives.get("previous") or []) + ([drives["current"]] if drives.get("current") else [])
    return [p for d in pool for p in d.get("plays") or []]


def game_changers(summary, threshold):
    """[(play, home win% before, home win% after)] for turnovers that moved ESPN's win chance by `threshold`+ points."""
    wp = {str(w.get("playId")): float(w.get("homeWinPercentage") or 0) * 100 for w in summary.get("winprobability") or []}
    out, prev = [], None
    for p in _all_plays(summary):
        w = wp.get(str(p.get("id")))
        t = (p.get("type") or {}).get("text", "")
        if w is not None and prev is not None and any(k in t for k in TURNOVER_TYPES) and "Touchdown" not in t:
            if abs(w - prev) >= threshold:
                out.append((p, prev, w))
        if w is not None:
            prev = w
    return out


def nfl_gc_html(event, play, summary, live, ol, before, after):
    """before/after: home team's ESPN win chance (0-100) either side of the play."""
    import html as H
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    q = (play.get("period") or {}).get("number", 0)
    clock = (play.get("clock") or {}).get("displayValue", "")
    text = play.get("text") or ""
    t = (play.get("type") or {}).get("text", "")
    kind = ("INTERCEPTION" if "Interception" in t else "FUMBLE" if "Fumble" in t else "BLOCKED KICK")
    # the team that gained: the side whose win chance went up
    gain_home = after > before
    gt, lt = (home, away) if gain_home else (away, home)
    g_before, g_after = (before, after) if gain_home else (100 - before, 100 - after)
    swing = g_after - g_before
    m = re.search(r"INTERCEPTED by ([A-Z]\.\s?[A-Za-z'\-]+)", text) or re.search(r"RECOVERED by \w+-([A-Z]\.\s?[A-Za-z'\-]+)", text)
    hero = m.group(1).replace(". ", ".") if m else (gt["team"].get("name") or "")
    m2 = re.search(r"([A-Z]\.\s?[A-Za-z'\-]+) pass", text) or re.search(r"([A-Z]\.\s?[A-Za-z'\-]+) FUMBLES", text)
    victim = m2.group(1) if m2 else ""

    def side(t_, sc, lo, right):
        img = f"<img src='{H.escape(t_['team'].get('logo', ''))}'>"
        name = f"<span>{t_['team']['abbreviation']}</span>"
        score = f"<span class='sc{' lo' if lo else ''}'>{sc}</span>"
        return f"<div class='tm{' r' if right else ''}'>{(score + name + img) if right else (img + name + score)}</div>"
    sb = (f"<div class='sb5'>{side(away, a_s, a_s < h_s, False)}"
          f"<div class='mid'><div class='clk'>{'OT' if q > 4 else 'Q' + str(q)} · {clock} LEFT</div></div>"
          f"{side(home, h_s, h_s < a_s, True)}</div>")

    ca = vivid(gt["team"].get("color") or "1faa59")
    co = vivid(lt["team"].get("color") or "5b7bd5")

    def bar(p):
        return (f"<div class='bar'><div style='width:{p:.1f}%;background:{ca}'></div>"
                f"<div style='width:{100 - p:.1f}%;background:{co}88'></div></div>")
    sw = (f"<div class='sw5'><div class='ih'>WIN CHANCE · ESPN</div>"
          f"<div class='big'><span>{gt['team']['abbreviation']}</span><span class='from'>{g_before:.0f}%</span>"
          f"<span class='arr'>→</span><span>{g_after:.0f}%</span><span class='d u'>▲{abs(swing):.0f}</span></div>"
          f"<div class='row'><span class='lb'>BEFORE</span>{bar(g_before)}<span class='lb r'>{lt['team']['abbreviation']} {100 - g_before:.0f}%</span></div>"
          f"<div class='row'><span class='lb'>AFTER</span>{bar(g_after)}<span class='lb r'>{lt['team']['abbreviation']} {100 - g_after:.0f}%</span></div></div>")

    # fantasy: defence that forced it gains, the player who gave it up loses
    sc = league_scoring("nfl")
    rows = []
    pid = sl_team(gt["team"]["abbreviation"])
    od = league_owner(f"def:{pid}", "")
    dpts = float(sc.get("int") or 0) if kind == "INTERCEPTION" else float(sc.get("fum_rec") or 0) + float(sc.get("ff") or 0)
    if kind == "BLOCKED KICK":
        dpts = float(sc.get("blk_kick") or 0)
    nick = gt["team"].get("name") or gt["team"]["abbreviation"]
    if od:
        rows.append(f"<div class='im'><span class='ar u'>▲</span><b class='who5'>{H.escape(od['manager'])}</b>"
                    f"<span>{H.escape(nick)} D/ST <b class='u'>+{dpts:g}</b> for the {kind.lower()}</span>"
                    f"<span>{'starting' if od['starting'] else 'bench'}</span><span></span></div>")
    else:
        rows.append(f"<div class='im'><span class='ar'>🆓</span><b class='who5'>{H.escape(nick)} D/ST</b>"
                    f"<span><b class='u'>+{dpts:g}</b> for the {kind.lower()} — unrostered, on waivers</span>"
                    f"<span></span><span></span></div>")
    if victim:
        full = next((p["name"] for p in nfl_fantasy(summary)
                     if p["team"] == lt["team"]["abbreviation"] and name_key(p["name"]).endswith(name_key(victim.split(".")[-1])[2:])),
                    victim)
        vo = league_owner(boxscore_id(summary, full), full)
        loss = float(sc.get("pass_int") or 0) if kind == "INTERCEPTION" else float(sc.get("fum_lost") or 0)
        if vo:
            rows.append(f"<div class='im'><span class='ar dn'>▼</span><b class='who5'>{H.escape(vo['manager'])}</b>"
                        f"<span>{H.escape(full)} <b class='dn'>{loss:g}</b> for the {kind.lower()}</span>"
                        f"<span>{'starting' if vo['starting'] else 'bench'}</span><span></span></div>")
    imp = f"<div class='imp'><div class='ih'>FANTASY IMPACT</div>{''.join(rows)}</div>"

    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>GAME-CHANGER · {'OT' if q > 4 else 'Q' + str(q)} {clock} · "
            f"{away['team']['abbreviation']} {a_s}–{h_s} {home['team']['abbreviation']}</div>"
            f"<div class='pill'>{kind}</div></div>"
            + score_hero(event, a_s, h_s, f"{'OT' if q > 4 else 'Q' + str(q)} · {clock} LEFT", f"{kind} · {gt['team']['abbreviation']} BALL")
            + f"{sw}{imp}")
    ch = vivid(gt["team"].get("alternateColor") or gt["team"].get("color") or "5b7bd5", min_l=0.5)
    bg = (f".bg{{opacity:1!important;background:"
          f"linear-gradient(120deg,{ca}d9 0%,{ca}73 38%,rgba(13,18,32,0) 78%),"
          f"linear-gradient(300deg,{co}38 0%,rgba(13,18,32,0) 22%)!important}}"
          f".hl{{border-left-color:{ch}!important}}")
    return (_card(f"--a:{ca};--h:{ch}", body, extra_css=V4_CSS + TD5_CSS + FG_CSS + GC_CSS + SB_CSS + bg),
            kind, hero, victim, gt, lt, g_before, g_after)


def post_game_changer(event, play, summary, ctx, before, after):
    html_text, kind, hero, victim, gt, lt, gb, ga = nfl_gc_html(event, play, summary, ctx["live"], ctx["ol"], before, after)
    nick = gt["team"].get("name") or gt["team"]["abbreviation"]
    what = {"INTERCEPTION": f"{hero} picks off {victim}!" if victim else f"{hero} with the interception!",
            "FUMBLE": f"{hero} recovers — {victim} coughs it up!" if victim else f"{nick} take it away!",
            "BLOCKED KICK": f"Blocked! {nick} get the stop!"}[kind]
    m = re.search(r"(?:INTERCEPTED|RECOVERED) by .+? at ([A-Z]{2,3} \d+)", play.get("text") or "")
    where = ""
    if m:
        lnick = lt["team"].get("name") or lt["team"]["abbreviation"]
        own_half = m.group(1).startswith(gt["team"]["abbreviation"])
        where = f"{lnick} were at the {m.group(1)} · " if own_half else f"Takeaway at the {m.group(1)} · "
    embed = {"author": AU_NFL(), "color": color(gt["team"].get("color")), "title": f"Game-changer: {what}",
             "description": f"{where}{nick} win chance {gb:.0f}% → {ga:.0f}% (ESPN)"}
    return _post_card(NFL_HOOK, html_text, f"🔄 GAME-CHANGER · {nick.upper()}", embed, "gc.png")


_nfl_prime_slot_gc = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    """+ game-changing turnovers (ESPN win chance swings by nfl.game_changer_swing points or more)."""
    thr = CONFIG["nfl"].get("game_changer_swing")
    if thr:
        for e in slot:
            if e["status"]["type"]["state"] != "in":
                continue
            g = st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            for p, b, a in game_changers(summary, float(thr)):
                if p["id"] in g.setdefault("gc", []):
                    continue
                if p.get("wallclock") and time.time() - _wallclock_ts(p["wallclock"]) > 600:
                    g["gc"].append(p["id"])          # older than 10 minutes (bot restarted): don't post late
                    continue
                if not td_ready(summary, p, g.setdefault("seen", {}).setdefault(p["id"], time.time())):
                    continue
                ctx = getctx()
                if ctx and post_game_changer(e, p, summary, ctx, b, a):
                    g["gc"].append(p["id"])
    return _nfl_prime_slot_gc(slot, st, getctx, force)


# ------------------------------------------------ half-time / overtime / full-time: big scoreboard first

_nfl_game_fantasy_html_sb = nfl_game_fantasy_html


def nfl_game_fantasy_html(event, summary, live, ol, base, max_rows=30):
    import html as H
    html_text = _nfl_game_fantasy_html_sb(event, summary, live, ol, base, max_rows)
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    t = event["status"]["type"]
    q = event["status"].get("period", 0)
    if t.get("state") == "post":
        status = "FINAL · OT" if q > 4 else "FINAL"
    elif t.get("name") == "STATUS_HALFTIME":
        status = "HALF-TIME"
    elif q >= 5:
        status = "OVERTIME"
    elif t.get("name") == "STATUS_END_PERIOD" and q == 4:
        status = "END OF REGULATION"
    else:
        status = f"Q{q}"

    def side(tm, sc, lo, right):
        img = f"<img src='{H.escape(tm['team'].get('logo', ''))}'>"
        name = f"<span>{H.escape(tm['team'].get('name') or tm['team']['abbreviation'])}</span>"
        score = f"<span class='sc{' lo' if lo else ''}'>{sc}</span>"
        return f"<div class='tm{' r' if right else ''}'>{(score + name + img) if right else (img + name + score)}</div>"
    sb = (f"<div class='sb5 big'>{side(away, a_s, a_s < h_s, False)}<div class='mid'><div class='clk'>{status}</div></div>"
          f"{side(home, h_s, h_s < a_s, True)}</div>")
    i = html_text.find("<div class='top'>")
    j = html_text.find("</div></div>", i) + len("</div></div>")
    html_text = html_text[:j] + sb + html_text[j:]
    html_text = re.sub(r"<div class='sm'>.*?</div>(?=</div>)", "", html_text, count=1, flags=re.S)
    css = FG_CSS + ".sb5.big{margin-top:12px;padding:16px 22px}.sb5.big .sc{font-size:64px}.sb5.big .tm{font-size:22px}"
    css += ".sb5.big .tm img{width:64px;height:64px}.sb5.big .clk{font-size:15px}"
    if a_s != h_s:                                   # leading team's colours; neutral when level
        lead, trail = (away, home) if a_s > h_s else (home, away)
        ca, co = vivid(lead["team"].get("color") or "1faa59"), vivid(trail["team"].get("color") or "5b7bd5")
        css += (f".bg{{opacity:1!important;background:linear-gradient(120deg,{ca}c0 0%,{ca}5c 36%,rgba(13,18,32,0) 74%),"
                f"linear-gradient(300deg,{co}30 0%,rgba(13,18,32,0) 22%)!important}}")
    return html_text.replace("</style>", css + "</style>", 1)


# ------------------------------------------------ overtime: the urgent version

OT_CSS = """
.oth{position:relative;margin-top:12px;border-radius:16px;overflow:hidden;padding:22px 26px;
 background:linear-gradient(100deg,var(--ca) 0%,var(--ca) 47%,var(--cb) 53%,var(--cb) 100%)}
.oth:before{content:'';position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.25),rgba(0,0,0,.55))}
.oth .row{position:relative;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px}
.oth .tm{display:flex;align-items:center;gap:16px}.oth .tm.r{justify-content:flex-end}
.oth .tm img{width:92px;height:92px;object-fit:contain;background:rgba(255,255,255,.92);border-radius:50%;padding:10px;box-shadow:0 4px 16px rgba(0,0,0,.45)}
.oth .tm .nm{font-size:24px;font-weight:900;text-transform:uppercase;letter-spacing:1px}
.oth .sc{font-size:84px;font-weight:900;line-height:1;font-variant-numeric:tabular-nums;text-shadow:0 4px 18px rgba(0,0,0,.6)}
.oth .badge{width:118px;height:118px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;
 background:radial-gradient(circle,#ff5a4e 0%,#c81d25 62%,#7a0d12 100%);
 box-shadow:0 0 0 5px rgba(255,209,102,.9),0 0 38px 10px rgba(255,90,78,.75)}
.oth .badge b{font-size:44px;font-weight:900;line-height:1;letter-spacing:1px}
.oth .badge span{font-size:10.5px;font-weight:900;letter-spacing:1.6px;color:#ffe3a3;margin-top:3px}
.oth .sub{position:relative;text-align:center;margin-top:14px;font-size:14px;font-weight:900;letter-spacing:3px;color:#ffd166}
.otl{margin-top:10px;background:rgba(9,12,22,.7);border-radius:14px;padding:10px 16px}
.otl .ih{font-size:11px;letter-spacing:1.6px;font-weight:900;color:#ff8a80}
.otr{display:grid;grid-template-columns:150px 1fr 230px;align-items:center;gap:12px;padding:8px 0;
 border-top:1px solid rgba(255,255,255,.08)}
.otl .ih + .otr{border-top:none}
.otr .mg{font-size:16px;font-weight:900}
.otr .ch{display:flex;flex-wrap:wrap;gap:6px}
.otr .ch span{letter-spacing:0;background:rgba(255,255,255,.1);border-radius:7px;padding:3px 8px;font-size:13px;font-weight:700;color:#e3e7ef}
.otr .ch span b{color:#7ee2a8;margin-left:4px}
.otr .pj{text-align:right;font-size:13px;color:#c3cad8;font-weight:700}
.otr .pj b{color:#fff;font-size:15px}.otr .pj .u{color:#7ee2a8}.otr .pj .dn{color:#ff9a9e}
.otr .flip{display:inline-block;margin-left:6px;background:#ffd166;color:#1b1f2a;border-radius:6px;padding:1px 6px;
 font-size:10.5px;font-weight:900;letter-spacing:1px}
.otf{margin-top:10px;text-align:center;font-size:12.5px;font-weight:800;letter-spacing:2px;color:#ffd166}
"""


def nfl_ot_html(event, summary, live, ol):
    import html as H
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    ca = vivid(away["team"].get("color") or "1faa59")
    cb = vivid(home["team"].get("color") or "5b7bd5")
    hero = (f"<div class='oth' style='--ca:{ca};--cb:{cb}'><div class='row'>"
            f"<div class='tm'><img src='{H.escape(away['team'].get('logo', ''))}'><div><div class='nm'>"
            f"{H.escape(away['team'].get('name') or away['team']['abbreviation'])}</div><div class='sc'>{a_s}</div></div></div>"
            f"<div class='badge'><b>OT</b><span>OVERTIME</span></div>"
            f"<div class='tm r'><div style='text-align:right'><div class='nm'>"
            f"{H.escape(home['team'].get('name') or home['team']['abbreviation'])}</div><div class='sc'>{h_s}</div></div>"
            f"<img src='{H.escape(home['team'].get('logo', ''))}'></div></div>"
            f"<div class='sub'>TIED AFTER 60 MINUTES · 10:00 OF OVERTIME TO COME</div></div>")

    # what's on the line: every manager with a starter in this game
    proj = nfl_projections(live["week"])
    players = sleeper_players("nfl")
    here = {sl_team(t["team"]["abbreviation"]) for t in teams_of(event)}
    npid = nfl_name_pid()
    now = {}
    for p in nfl_fantasy(summary):
        pid = p["id"][4:] if str(p.get("id", "")).startswith("def:") else npid.get((name_key(p["name"]), sl_team(p["team"])))
        if pid:
            now[pid] = p["pts"]
    rows = []
    for a, b in live["pairs"]:
        for r, opp in ((a, b), (b, a)):
            x = live["rosters"][r]
            mine = [p for p in x["starters"] if ((players.get(p) or {}).get("team") or p) in here]
            if not mine:
                continue
            pa, pb = _proj_total(r, live, ol, event, proj), _proj_total(opp, live, ol, event, proj)
            win = ol[r]["win"]
            chips = "".join(
                f"<span>{H.escape(short_name(players[p]['full_name']) if (players.get(p) or {}).get('full_name') else p + ' D/ST')}"
                f"<b>{fmt_pts(now.get(p, 0))}</b></span>" for p in mine)
            flip = "<span class='flip'>COIN FLIP</span>" if 35 <= win <= 65 else ""
            rows.append((abs(win - 50), f"<div class='otr'><span class='mg'>{H.escape(x['manager'])}</span>"
                         f"<div class='ch'>{chips}</div>"
                         f"<div class='pj'>proj <b class='{'u' if pa >= pb else 'dn'}'>{pa:.1f}</b> – {pb:.1f}"
                         f" vs {H.escape(live['rosters'][opp]['manager'])}<br>win <b>{win:.0f}%</b>{flip}</div></div>"))
    rows.sort(key=lambda z: z[0])
    line = (f"<div class='otl'><div class='ih'>🚨 WHAT'S ON THE LINE · STARTERS STILL PLAYING</div>"
            f"{''.join(r for _, r in rows)}</div>") if rows else ""
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL · WEEK {live['week']} · "
            f"{H.escape(CONFIG['nfl'].get('league_name', 'League').upper())}</div><div class='pill' "
            f"style='border-color:#ff5a4e;color:#ffb4ad'>OVERTIME</div></div>{hero}{line}"
            f"<div class='otf'>EVERY OVERTIME SCORE GETS A POST ⚡</div>")
    bg = (f".bg{{opacity:1!important;background:linear-gradient(120deg,#c81d25b0 0%,#7a0d1240 40%,rgba(13,18,32,0) 70%),"
          f"linear-gradient(300deg,#ffd16640 0%,rgba(13,18,32,0) 30%)!important}}.hl{{border-left-color:#ff5a4e!important}}"
          f".hl .h1{{font-size:28px!important}}")
    return _card("--a:#c81d25;--h:#ffd166", body, extra_css=V4_CSS + TD5_CSS + OT_CSS + bg)


def post_overtime(event, summary, ctx):
    away, home = teams_of(event)
    a = int(num(away.get("score")))
    embed = {"author": AU_NFL(), "color": 0xC81D25,
             "title": f"🚨 WE'RE GOING TO OVERTIME! {away['team'].get('name')} {a}–{a} {home['team'].get('name')}",
             "description": "Sixty minutes couldn't split them — and your league's matchups are riding on it."}
    return _post_card(NFL_HOOK, nfl_ot_html(event, summary, ctx["live"], ctx["ol"]), "🚨 **OVERTIME**", embed, "ot.png")


_nfl_ot_html_v2 = nfl_ot_html


# ------------------------------------------------ overtime v3: full-screen alarm

OT3_CSS = """
html,body{background:#12040a!important}.card{background:#12040a!important}
.tape.bot{margin-bottom:6px!important}
.bg{opacity:1!important;background:
 repeating-conic-gradient(from 0deg at 50% 38%,rgba(255,80,60,.20) 0deg 6deg,rgba(255,80,60,0) 6deg 18deg),
 radial-gradient(ellipse at 50% 38%,#ff3b2f 0%,#b3121b 30%,#4a0710 62%,#12040a 100%)!important}
.tape{margin:0 -32px;height:34px;display:flex;align-items:center;overflow:hidden;white-space:nowrap;
 background:repeating-linear-gradient(-45deg,#ffd166 0 22px,#141414 22px 44px);box-shadow:0 0 22px rgba(255,209,102,.5)}
.tape span{background:#141414;color:#ffd166;font-weight:900;letter-spacing:5px;font-size:15px;padding:4px 14px;margin:0 18px}
.tape.top{margin-top:-6px}.tape.bot{margin-top:18px;margin-bottom:-6px}
.otw{text-align:center;margin-top:18px;font-size:132px;line-height:.95;font-weight:900;font-style:italic;letter-spacing:-2px;
 transform:skew(-8deg);background:linear-gradient(180deg,#fff7d6 0%,#ffd166 45%,#ff9f1c 100%);-webkit-background-clip:text;
 background-clip:text;color:transparent;filter:drop-shadow(0 6px 0 #6b0a10) drop-shadow(0 0 28px rgba(255,190,60,.75))}
.ots{text-align:center;margin-top:8px;font-size:17px;font-weight:900;letter-spacing:6px;color:#fff;text-shadow:0 2px 10px #000}
.otv{display:grid;grid-template-columns:1fr 120px 1fr;align-items:center;gap:10px;margin-top:20px}
.otv .tm{display:flex;flex-direction:column;align-items:center;gap:6px}
.otv .disc{width:136px;height:136px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center;
 box-shadow:0 0 0 6px var(--tc),0 0 36px 8px var(--tc)}
.otv .disc img{width:104px;height:104px;object-fit:contain}
.otv .nm{font-size:22px;font-weight:900;letter-spacing:2px;text-transform:uppercase;color:#fff;margin-top:6px}
.otv .sc{font-size:110px;font-weight:900;line-height:.9;color:#fff;font-variant-numeric:tabular-nums;
 text-shadow:0 0 30px rgba(255,255,255,.35),0 6px 0 rgba(0,0,0,.45)}
.otv .vs{display:flex;align-items:center;justify-content:center;width:110px;height:110px;margin:auto;border-radius:50%;
 background:radial-gradient(circle,#fff 0%,#ffd166 35%,#ff6b35 70%,rgba(255,60,40,0) 72%);font-size:40px;font-weight:900;
 font-style:italic;color:#4a0710;text-shadow:0 1px 0 rgba(255,255,255,.6)}
.otq{text-align:center;margin-top:16px;font-size:15px;font-weight:900;letter-spacing:4px;color:#ffd166}
.otl{margin-top:16px!important;background:rgba(10,4,8,.78)!important;border:2px solid rgba(255,90,78,.85);
 box-shadow:0 0 28px rgba(255,59,47,.45)}
.otl .ih{color:#ffd166!important;font-size:13px!important;letter-spacing:2.4px!important}
.otr .flip{animation:none;box-shadow:0 0 14px rgba(255,209,102,.8)}
.otf{display:none}
"""


def nfl_ot_html(event, summary, live, ol):
    import html as H
    away, home = teams_of(event)
    a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
    # reuse v2's "what's on the line" panel
    v2 = _nfl_ot_html_v2(event, summary, live, ol)
    i = v2.find("<div class='otl'>")
    line = v2[i:v2.find("<div class='otf'>", i)] if i >= 0 else ""
    tape = "".join("<span>OVERTIME</span>" for _ in range(8))

    def side(tm, sc):
        c = vivid(tm["team"].get("color") or "ffffff", min_l=0.45)
        return (f"<div class='tm' style='--tc:{c}'><div class='disc'><img src='{H.escape(tm['team'].get('logo', ''))}'></div>"
                f"<div class='nm'>{H.escape(tm['team'].get('name') or tm['team']['abbreviation'])}</div>"
                f"<div class='sc'>{sc}</div></div>")
    body = (f"<div class='tape top'>{tape}</div>"
            f"<div class='otw'>OVERTIME</div>"
            f"<div class='ots'>🚨 TIED AFTER 60 MINUTES 🚨</div>"
            f"<div class='otv'>{side(away, a_s)}<div class='vs'>VS</div>{side(home, h_s)}</div>"
            f"<div class='otq'>10:00 ON THE CLOCK · EVERY SNAP MATTERS</div>"
            f"{line}"
            f"<div class='tape bot'>{''.join('<span>EVERY OT SCORE GETS A POST</span>' for _ in range(4))}</div>")
    return _card("--a:#c81d25;--h:#ffd166", body, extra_css=V4_CSS + TD5_CSS + OT_CSS + OT3_CSS)


def post_overtime(event, summary, ctx):
    away, home = teams_of(event)
    a = int(num(away.get("score")))
    embed = {"author": AU_NFL(), "color": 0xC81D25, "img_title": "", "img_desc": "",
             "title": f"🚨 OVERTIME! {away['team'].get('name')} {a}–{a} {home['team'].get('name')}"}
    return _post_card(NFL_HOOK, nfl_ot_html(event, summary, ctx["live"], ctx["ol"]), "🚨 **OVERTIME**", embed, "ot.png")


# ------------------------------------------------ scoreline first: shared big scoreboard for every in-game post

SB_CSS = """
.sbx{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;margin-top:12px;
 background:rgba(9,12,22,.66);border-radius:16px;padding:16px 22px}
.sbx .tm{display:flex;align-items:center;gap:14px}.sbx .tm.r{justify-content:flex-end}
.sbx .disc{width:70px;height:70px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center;flex:none}
.sbx .disc img{width:54px;height:54px;object-fit:contain}
.sbx .nm{font-size:19px;font-weight:900;letter-spacing:1px;text-transform:uppercase;color:#e3e7ef}
.sbx .sc{font-size:76px;font-weight:900;line-height:.9;font-variant-numeric:tabular-nums;color:#8b95a8}
.sbx .sc.hot{color:#fff;text-shadow:0 0 26px var(--hc),0 4px 0 rgba(0,0,0,.4)}
.sbx .sc.lead{color:#fff}
.sbx .plus{display:inline-block;margin-top:6px;background:#7ee2a8;color:#0d1220;border-radius:8px;padding:2px 9px;
 font-size:17px;font-weight:900}
.sbx .mid{text-align:center}
.sbx .clk{font-size:14px;font-weight:900;letter-spacing:2px;color:#ffd166}
.sbx .st{font-size:12px;font-weight:800;letter-spacing:1.5px;color:#c3cad8;margin-top:4px}
.sbx .yd{font-size:30px;font-weight:900;line-height:1.05;color:#fff;margin-top:6px;letter-spacing:.5px;white-space:nowrap}
"""


def score_hero(event, a_s, h_s, clock_txt, sub="", hot=None, plus=None, big=None):
    """Big scoreline: logos, names, scores; the team that just scored glows with a +N chip."""
    import html as H
    away, home = teams_of(event)

    def side(tm, sc, other, right):
        ab = tm["team"]["abbreviation"]
        is_hot = hot == ab
        cls = "hot" if is_hot else ("lead" if sc > other else "")
        hc = vivid(tm["team"].get("color") or "ffffff", min_l=0.5)
        chip = f"<div class='plus'>+{plus}</div>" if is_hot and plus else ""
        disc = f"<div class='disc'><img src='{H.escape(tm['team'].get('logo', ''))}'></div>"
        txt = (f"<div style='text-align:{'right' if right else 'left'}'><div class='nm'>"
               f"{H.escape(tm['team'].get('name') or ab)}</div><div class='sc {cls}' style='--hc:{hc}'>{sc}</div>{chip}</div>")
        return f"<div class='tm{' r' if right else ''}'>{(txt + disc) if right else (disc + txt)}</div>"
    return (f"<div class='sbx'>{side(away, a_s, h_s, False)}<div class='mid'><div class='clk'>{H.escape(clock_txt)}</div>"
            f"{f'<div class=yd>{H.escape(big)}</div>' if big else ''}"
            f"{f'<div class=st>{H.escape(sub)}</div>' if sub else ''}</div>{side(home, h_s, a_s, True)}</div>")


def _pre_total(play, a_s, h_s, abbr, away):
    """Combined score before this touchdown."""
    m = re.search(r"\(([^()]*)\)\s*$", play.get("text") or "")
    pts = 6
    if m and not re.search(r"fail|miss|block|no good", m.group(1), re.I):
        pts += 2 if re.search(r"two-point|2pt|2-pt", m.group(1), re.I) else 1
    return a_s + h_s - pts


# ------------------------------------------------ NBA: its own look so it never blends in with the NFL posts

NBA_THEME_CSS = """
.bg{opacity:1!important;background:
 radial-gradient(circle at 50% -6%,transparent 0 210px,rgba(255,255,255,.22) 210px 214px,transparent 215px),
 linear-gradient(180deg,rgba(14,9,5,.62) 0%,rgba(14,9,5,.86) 55%,rgba(14,9,5,.92) 100%),
 repeating-linear-gradient(90deg,#c26a24 0 58px,#b45f1e 58px 60px,#cf7630 60px 118px,#b45f1e 118px 120px)!important}
.nbar{margin:-26px -32px 14px;padding:10px 32px;display:flex;align-items:center;gap:12px;
 background:linear-gradient(90deg,#ee6730,#f58426 55%,#ffb347);color:#1a0f06;font-weight:900;font-size:17px;letter-spacing:3px;
 box-shadow:0 4px 18px rgba(245,132,38,.45)}
.nbar img{width:30px;height:30px;background:#fff;border-radius:7px;padding:2px}
.nbar .b{font-size:22px}
.pr2,.lg2,.hl,.sbx{background:rgba(16,11,7,.78)!important}
.pr2.lead{background:rgba(52,30,14,.88)!important}
.hl{border-left-color:#f58426!important}
.sbx .clk{color:#ffb347!important}
"""


def _nba_theme(html_text, event=None, label="FULL-TIME"):
    ribbon = f"<div class='nbar'><span class='b'>🏀</span><img src='{NBA_LOGO}'>NBA · {label}</div>"
    i = html_text.find("<div class='in'>")
    if i >= 0:
        html_text = html_text[:i + 16] + ribbon + html_text[i + 16:]
    if event is not None:                                   # single game: big scoreline under the top bar
        away, home = teams_of(event)
        a_s, h_s = int(num(away.get("score"))), int(num(home.get("score")))
        st = event["status"]["type"]
        clock = "FINAL" + (" · OT" if event["status"].get("period", 0) > 4 else "") if st.get("state") == "post" else "HALF-TIME"
        j = html_text.find("<div class='top'>")
        k = html_text.find("</div></div>", j) + len("</div></div>")
        if j >= 0:
            html_text = html_text[:k] + score_hero(event, a_s, h_s, clock) + html_text[k:]
        html_text = re.sub(r"<div class='sm'>.*?</div>(?=</div>)", "", html_text, count=1, flags=re.S)
    return html_text.replace("</style>", SB_CSS + NBA_THEME_CSS + "</style>", 1)


_nba_game_html_themed = nba_game_fantasy_html


def nba_game_fantasy_html(event, summary, live, max_rows=7):
    return _nba_theme(_nba_game_html_themed(event, summary, live, max_rows), event)


_nba_batch_html_themed = nba_batch_html


def nba_batch_html(mode, events, summaries, live, title):
    return _nba_theme(_nba_batch_html_themed(mode, events, summaries, live, title), None,
                      "HALF-TIME" if "HALF" in str(mode).upper() else "FULL-TIME")


def _nba_post(html_text, content, embed, name):
    _POSTER["name"] = (CONFIG.get("nba") or {}).get("bot_name") or "Donald 🏀"
    try:
        return _post_card(NBA_HOOK, html_text, content, embed, name)
    finally:
        _POSTER.clear()


# ------------------------------------------------ poster names: Donald 🏈 for NFL, Donald 🏀 for NBA

_post_weekly_recap_named = post_weekly_recap


def post_weekly_recap(sport, week, live, settings):
    if sport == "nba":
        _POSTER["name"] = (CONFIG.get("nba") or {}).get("bot_name") or "Donald 🏀"
    try:
        return _post_weekly_recap_named(sport, week, live, settings)
    finally:
        _POSTER.clear()


def _td_yards_label(td):
    y = td.get("yds")
    kind = {"pass": "TD PASS", "rush": "TD RUN"}.get(td.get("kind"), "RETURN TD")
    return f"{y}-YARD {kind}" if y is not None else kind


def _td_headline(td, nick):
    y = td.get("yds")
    yd = f"{y}-yard " if y is not None else ""
    if td.get("kind") == "pass":
        return f"{td['qb']} finds {td['scorer']} for a {yd}TD — {nick}!"
    if td.get("kind") == "rush":
        return f"{td['scorer']} {yd}TD run — {nick}!"
    return f"{td['scorer']} {yd}return TD — {nick}!"


def proj_bar(now, pj, rem=0.0):
    """Points vs projection + the updated projection (points so far + projection for the rest of the game).
    Beating the original is celebrated: gold glowing bar + badge (💥 at 150%+)."""
    newp = now + pj * max(0.0, min(1.0, rem))
    upd = (f"<div class='pjn'>projected {pj:.1f} → <b class='{'up' if newp >= pj else 'dn'}'>now {newp:.1f}</b></div>"
           if rem > 0.005 else f"<div class='pjn'>projected {pj:.1f} · final {now:.1f}</div>")
    if now < pj:
        pct = max(now / pj * 100, 3)
        return (f"<div class='pb5'><div class='bar'><div style='width:{pct:.0f}%'></div></div>"
                f"<span>proj <b>{pj:.1f}</b></span></div>{upd}")
    over = now - pj
    badge = (f"💥 SMASHED PROJ · +{over:.1f}" if now >= 1.5 * pj else f"🔥 BEAT PROJ · +{over:.1f}")
    return (f"<div class='pb5 win'><div class='bar'><div style='width:100%'></div></div>"
            f"<span class='beat'>{badge}</span></div>{upd}")


# ------------------------------------------------ NBA race panel: tonight's swing, games left, projected finals

NBA_ESPN_TO_SLEEPER = {"GS": "GSW", "NY": "NYK", "NO": "NOP", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}
_NBA_PROJ = {}

RACE5_CSS = """
.rc{margin-top:10px;background:rgba(16,11,7,.82);border-radius:14px;padding:10px 16px}
.rc .ih{font-size:11px;letter-spacing:1.6px;font-weight:900;color:#ffb347;margin-bottom:4px}
.rc .hdr,.rc .rr{display:grid;grid-template-columns:1fr 128px 1fr 96px 150px 108px;align-items:center;gap:10px}
.rc .hdr{font-size:10px;letter-spacing:1.3px;font-weight:800;color:#a89a8a;padding:2px 0 6px}
.rc .rr{padding:7px 0;border-top:1px solid rgba(255,255,255,.07);font-size:14px;color:#e8e2da}
.rc .nm{font-weight:900;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rc .nm.r{text-align:right}.rc .nm small{display:block;font-size:11.5px;font-weight:800;color:#7ee2a8}
.rc .nm small.z{color:#8b7f72}
.rc .sc{text-align:center;font-weight:900;font-size:16px;font-variant-numeric:tabular-nums}.rc .sc .lo{color:#8b7f72}
.rc .gl{text-align:center;font-weight:900;font-variant-numeric:tabular-nums}.rc .gl .more{color:#7ee2a8}.rc .gl .less{color:#ff9a9e}
.rc .pj{text-align:center;font-variant-numeric:tabular-nums;font-weight:800}.rc .pj b{color:#fff}
.rc .wn{display:flex;align-items:center;gap:6px;justify-content:flex-end;font-weight:900}
.rc .wn .bar{width:54px;height:7px;border-radius:999px;background:rgba(255,255,255,.12);overflow:hidden}
.rc .wn .bar div{height:7px;background:linear-gradient(90deg,#ee6730,#ffb347)}
.rc .flip{background:#ffd166;color:#1b1305;border-radius:5px;padding:0 5px;font-size:10px;letter-spacing:.8px}
.rc .note{font-size:11px;color:#a89a8a;margin-top:6px}
"""


def _nba_projections_lastrow(week):
    """Old version (kept for reference): kept whichever game came last."""
    if week in _NBA_PROJ:
        return _NBA_PROJ[week]
    out = {}
    try:
        season = fetch_url(f"{SLEEPER}/state/nba").get("season")
        sc = league_scoring("nba")
        for r in fetch_url(f"https://api.sleeper.app/projections/nba/{season}/{week}?season_type=regular") or []:
            s = r.get("stats") or {}
            gp = float(s.get("gp") or 1) or 1
            out[str(r.get("player_id"))] = sum(float(v or 0) * float(sc[k]) for k, v in s.items()
                                               if k in sc and isinstance(v, (int, float))) / gp
    except Exception as e:  # noqa: BLE001
        log(f"NBA projections unavailable: {e}")
    _NBA_PROJ[week] = out
    return out


def nba_games_left():
    """Games still to play this Sleeper week (Mon-Sun, US Eastern) per Sleeper team code; live games count by time left."""
    today = datetime.now(ET).date()
    end = today + timedelta(days=(6 - today.weekday()))
    left = {}
    d = today
    while d <= end:
        for e in fetch(f"basketball/nba/scoreboard?dates={d:%Y%m%d}").get("events", []):
            st = e["status"]["type"]["state"]
            if st == "post":
                continue
            share = 1.0
            if st == "in":
                per = e["status"].get("period", 1)
                share = max(0.0, 1 - ((per - 1) * 720 + (720 - clock_secs(e))) / 2880)
            for tm in teams_of(e):
                ab = NBA_ESPN_TO_SLEEPER.get(tm["team"]["abbreviation"], tm["team"]["abbreviation"])
                left[ab] = left.get(ab, 0.0) + share
        d += timedelta(days=1)
    return left


def nba_race_html(events, summaries, live):
    import html as H
    from math import erf, sqrt
    players = sleeper_players("nba")
    proj = nba_projections(live["week"])
    left = nba_games_left()
    tonight = {}
    for s in summaries:
        for r in (nba_league_rows(s) if s else []):
            if r["owner"] and r["starting"]:
                tonight[r["owner"]] = tonight.get(r["owner"], 0.0) + r["fp"]

    def outlook(rid):
        x = live["rosters"][rid]
        games = pts = 0.0
        for p in x["starters"]:
            pp = players.get(p) or {}
            if pp.get("injury_status") in ("Out", "IR"):
                continue
            g = left.get(pp.get("team"), 0.0)
            games += g
            pts += g * proj.get(str(p), 0.0)
        return games, x["points"] + pts

    rows = []
    for ra, rb in live["pairs"]:
        A, B = live["rosters"][ra], live["rosters"][rb]
        ga, pa = outlook(ra)
        gb, pb = outlook(rb)
        sd = 10.0 * sqrt(max(ga + gb, 0.5))
        wa = 100 * 0.5 * (1 + erf((pa - pb) / (sd * sqrt(2))))
        rows.append((abs(wa - 50), A, B, ga, gb, pa, pb, wa))
    rows.sort(key=lambda z: z[0])
    out = []
    for _, A, B, ga, gb, pa, pb, wa in rows:
        fav_a = wa >= 50
        w = wa if fav_a else 100 - wa
        fav = A if fav_a else B
        ta, tb = tonight.get(A["manager"], 0.0), tonight.get(B["manager"], 0.0)
        lead_a = A["points"] >= B["points"]
        flip = " <span class='flip'>COIN FLIP</span>" if w < 60 else ""
        out.append(
            f"<div class='rr'><div class='nm'>{H.escape(A['manager'])}<small class='{'' if ta else 'z'}'>"
            f"{'+' + fmt_pts(ta) + ' tonight' if ta else 'nobody tonight'}</small></div>"
            f"<div class='sc'><span class='{'' if lead_a else 'lo'}'>{A['points']:.1f}</span> – "
            f"<span class='{'lo' if lead_a else ''}'>{B['points']:.1f}</span></div>"
            f"<div class='nm r'>{H.escape(B['manager'])}<small class='{'' if tb else 'z'}'>"
            f"{'+' + fmt_pts(tb) + ' tonight' if tb else 'nobody tonight'}</small></div>"
            f"<div class='gl'><span class='{'more' if ga > gb + 0.5 else 'less' if ga < gb - 0.5 else ''}'>{ga:.0f}</span>"
            f" · <span class='{'more' if gb > ga + 0.5 else 'less' if gb < ga - 0.5 else ''}'>{gb:.0f}</span></div>"
            f"<div class='pj'><b>{pa:.0f}</b> – <b>{pb:.0f}</b></div>"
            f"<div class='wn'>{H.escape(fav['manager'][:9])} {w:.0f}%{flip}</div></div>")
    hdr = ("<div class='hdr'><span>MANAGER · TONIGHT</span><span style='text-align:center'>SCORE</span>"
           "<span style='text-align:right'>MANAGER · TONIGHT</span><span style='text-align:center'>GAMES LEFT</span>"
           "<span style='text-align:center'>PROJECTED FINAL</span><span style='text-align:right'>FAVOURITE</span></div>")
    league = CONFIG["nba"].get("league_name", "League")
    return (f"<div class='rc'><div class='ih'>🏀 {H.escape(league.upper())} · WEEK {live['week']} RACE · CLOSEST FIRST</div>"
            f"{hdr}{''.join(out)}<div class='note'>Games left = starters' games still to play this week (injured players left out) · "
            f"projected final = points so far + Sleeper per-game projections</div></div><style>{RACE5_CSS}</style>")


# ------------------------------------------------ health alerts (errors and missed posts) to a private channel

ALERT_HOOK = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
_ALERTED = {}
_IN_ALERT = {"on": False}
_log_plain = log


def alert(msg, key=None, every_h=3.0):
    """Post a short plain-text warning to the alert channel (rate-limited per key)."""
    if not ALERT_HOOK or SILENT or DRY_RUN or CAPTURE is not None or _IN_ALERT["on"]:
        return
    k = key or msg[:60]
    if time.time() - _ALERTED.get(k, 0) < every_h * 3600:
        return
    _ALERTED[k] = time.time()
    _IN_ALERT["on"] = True
    _POSTER["name"] = "Donald 🩺"
    try:
        discord(ALERT_HOOK, [], "alert", content=f"⚠️ **sports-bot** · {msg}"[:1900])
    finally:
        _POSTER.clear()
        _IN_ALERT["on"] = False


def log(*a):
    """Normal log line; anything that looks like a failure also goes to the alert channel."""
    _log_plain(*a)
    text = " ".join(str(x) for x in a)
    low = text.lower()
    if " error" in low or "discord rejected" in low or "discord post failed" in low or "traceback" in low:
        alert(text, key=re.sub(r"[0-9]+", "#", text)[:70])


def watchdog(st):
    """Flag posts that should have gone out by now (prime-time TDs/full-times, NBA full-times)."""
    now = time.time()
    if ALERT_HOOK and not st.get("alert_hello"):
        st["alert_hello"] = True
        _ALERTED.pop("hello", None)
        alert("✅ Alerts connected. I'll only post here if something goes wrong: an error, a failed Discord post, "
              "a touchdown or full-time that didn't go out, or the Tuesday accuracy check.", key="hello", every_h=0)
    try:
        for e in fetch("football/nfl/scoreboard").get("events", []):
            g = st.get("nfl", {}).get(e["id"])
            if not g or not g.get("preview"):          # only games the prime-time posting handles
                continue
            name = e.get("shortName", e["id"])
            t = e["status"]["type"]
            if t.get("completed") and not g.get("final") and g.get("ft_seen") and now - g["ft_seen"] > 20 * 60:
                alert(f"{name} finished 20+ min ago but no full-time post yet", key=f"ft{e['id']}", every_h=24)
            if t["state"] == "in":
                s = fetch(f"football/nfl/summary?event={e['id']}")
                for p in s.get("scoringPlays", []):
                    if (p.get("type") or {}).get("abbreviation") != "TD" or p["id"] in g.get("tds", []):
                        continue
                    _, d = play_detail(s, p.get("id"))
                    wc = (d or {}).get("wallclock")
                    if wc and now - _wallclock_ts(wc) > 10 * 60:
                        alert(f"{name}: touchdown not posted after 10 min — {p.get('text', '')[:80]}",
                              key=f"td{p['id']}", every_h=24)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"watchdog (NFL) failed: {ex}")
    try:
        for gid, g in (st.get("nba") or {}).items():
            if isinstance(g, dict) and g.get("ft_seen") and not g.get("posted") and now - g["ft_seen"] > 45 * 60:
                alert(f"NBA game {gid} finished 45+ min ago but no full-time post yet", key=f"nba{gid}", every_h=24)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"watchdog (NBA) failed: {ex}")


_maybe_daily_race_wd = maybe_daily_race


def maybe_daily_race(st):
    """(runs every 10 min from the main loop) daily race + the missed-post watchdog."""
    try:
        return _maybe_daily_race_wd(st)
    finally:
        watchdog(st)


# ------------------------------------------------ Sunday window / previews: big score tiles instead of small chips

TILE_CSS = """
.gtiles{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:12px}
.gt{display:grid;grid-template-columns:48px 1fr auto 1fr 48px;align-items:center;gap:8px;background:rgba(9,12,22,.66);
 border-radius:14px;padding:10px 14px}
.gt .dz{width:48px;height:48px;border-radius:50%;background:#fff;display:flex;align-items:center;justify-content:center}
.gt .dz img{width:36px;height:36px;object-fit:contain}
.gt .s{font-size:40px;font-weight:900;line-height:1;font-variant-numeric:tabular-nums;color:#8b95a8}
.gt .s.l{color:#fff}.gt .s.r{text-align:right}
.gt .ab{font-size:12px;font-weight:900;letter-spacing:1px;color:#c3cad8}
.gt .gmid{text-align:center;min-width:70px}
.gt .gclk{font-size:12px;font-weight:900;letter-spacing:1.2px;color:#ffd166;white-space:nowrap}
.gt .gclk.f{color:#c3cad8}
.gt .gko{font-size:22px;font-weight:900;color:#fff}
.spl .w2 b.yd{color:#fff;font-weight:900;font-size:13.5px}
"""


def games_strip_html(events, state_at=None, pre=False):
    """Big score tiles for every game in a window (kick-off times before the start)."""
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

        def disc(t):
            return f"<div class='dz'><img src='{H.escape(t['team'].get('logo', ''))}'></div>"
        if a_s is None:
            mid = f"<div class='gmid'><div class='gko'>{H.escape(str(stt))}</div><div class='ab'>KICK-OFF</div></div>"
            left = f"<div class='ab'>{aw['team']['abbreviation']}</div>"
            right = f"<div class='ab' style='text-align:right'>{hm['team']['abbreviation']}</div>"
        else:
            fin = str(stt).upper().startswith("FINAL")
            mid = (f"<div class='gmid'><div class='gclk{' f' if fin else ''}'>{H.escape(str(stt))}</div>"
                   f"<div class='ab'>{aw['team']['abbreviation']} · {hm['team']['abbreviation']}</div></div>")
            left = f"<div class='s{' l' if a_s >= h_s else ''}'>{a_s}</div>"
            right = f"<div class='s r{' l' if h_s >= a_s else ''}'>{h_s}</div>"
        out.append(f"<div class='gt'>{disc(aw)}{left}{mid}{right}{disc(hm)}</div>")
    return f"<div class='gtiles'>{''.join(out)}</div><style>{TILE_CSS}</style>"


_scoring_line_html_tiles = scoring_line_html


def scoring_line_html(p):
    """Scoring line with the yardage in bold."""
    h = _scoring_line_html_tiles(p)
    return re.sub(r"(<div class='w2'>)(\d+-yd)", r"\1<b class='yd'>\2</b>", h, count=1)


# ------------------------------------------------ weekly accuracy check: bot's points vs Sleeper's finals (NFL)

def nfl_accuracy(week, tol=0.5):
    """(checked, [(player, manager, ours, sleeper)]) for every rostered player who played in `week`."""
    live = league_live("nfl", week)
    npid = nfl_name_pid()
    allp = sleeper_players("nfl")
    by_espn = {str(v.get("espn_id")): k for k, v in allp.items() if v.get("espn_id")}
    by_name, by_last = {}, {}
    for k, v in allp.items():
        if v.get("full_name") and v.get("team"):
            by_name.setdefault(name_key(v["full_name"]), []).append(k)
            by_last.setdefault((name_key(v.get("last_name", "")), v["team"]), []).append(k)

    def match(p):
        if str(p.get("id", "")).startswith("def:"):
            return p["id"][4:]
        hit = by_espn.get(str(p.get("id"))) or npid.get((name_key(p["name"]), sl_team(p["team"])))
        if hit:
            return hit
        one = by_name.get(name_key(p["name"]), [])
        if len(one) == 1:                                    # traded player: same name, new team
            return one[0]
        last = by_last.get((name_key(p["name"].split()[-1]), sl_team(p["team"])), [])
        return last[0] if len(last) == 1 else None           # nickname: same surname + team
    ours = {}
    for e in fetch(f"football/nfl/scoreboard?seasontype=2&week={week}").get("events", []):
        if not e["status"]["type"].get("completed"):
            continue
        s = fetch(f"football/nfl/summary?event={e['id']}")
        s["_week"] = week
        played = set()
        for p in nfl_fantasy(s):
            pid = match(p)
            if pid:
                ours[pid] = (p["name"], p["pts"])
        for t in teams_of(e):
            played.add(sl_team(t["team"]["abbreviation"]))
        ours.setdefault("_teams", set()).update(played)
    teams = ours.pop("_teams", set())
    players = sleeper_players("nfl")
    checked, bad = 0, []
    for x in live["rosters"].values():
        for pid in x["players"]:
            pp = players.get(pid) or {}
            team = pp.get("team") or pid
            if team not in teams or pid not in x["pp"]:
                continue
            slp = float(x["pp"].get(pid) or 0)
            name, mine = ours.get(pid, (pp.get("full_name") or f"{pid} D/ST", 0.0))
            checked += 1
            if abs(mine - slp) > tol:
                bad.append((name, x["manager"], mine, slp))
    bad.sort(key=lambda z: -abs(z[2] - z[3]))
    return checked, bad


def maybe_accuracy_check(st):
    """Tuesday 12:00 UK (before the 13:00 recap): last week's check goes to the alert channel."""
    now = datetime.now(UK)
    if WEEKDAYS[now.weekday()] != "Tuesday" or now.hour < 12:
        return
    try:
        week = int(fetch_url(f"{SLEEPER}/state/nfl").get("week") or 0) - 1
    except Exception:  # noqa: BLE001
        return
    if week < 1 or st.get("acc_week") == week:
        return
    checked, bad = nfl_accuracy(week)
    st["acc_week"] = week
    if not bad:
        msg = f"✅ Week {week} accuracy: all {checked} rostered players match Sleeper (within 0.5)."
    else:
        lines = "\n".join(f"• {n} ({m}): bot {a:.1f} vs Sleeper {b:.1f}" for n, m, a, b in bad[:12])
        msg = f"Week {week} accuracy: {len(bad)} of {checked} players differ from Sleeper by more than 0.5\n{lines}"
    alert(msg, key=f"acc{week}", every_h=0)


_maybe_daily_race_acc = maybe_daily_race


def maybe_daily_race(st):
    try:
        maybe_accuracy_check(st)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"accuracy check failed: {ex}")
    return _maybe_daily_race_acc(st)


# ------------------------------------------------ team defences scored from Sleeper's own stats (exact match)

_SLP_STATS = {}


def sleeper_week_stats(week=None):
    """Sleeper's NFL stats for the week (live during games), cached for a minute."""
    try:
        if week is None:
            week = int(fetch_url(f"{SLEEPER}/state/nfl").get("week") or 0)
        hit = _SLP_STATS.get(week)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        season = fetch_url(f"{SLEEPER}/state/nfl").get("season")
        data = fetch_url(f"{SLEEPER}/stats/nfl/regular/{season}/{week}") or {}
        _SLP_STATS[week] = (time.time(), data)
        return data
    except Exception as e:  # noqa: BLE001
        _log_plain(f"Sleeper stats unavailable: {e}")
        return {}


def sleeper_def_points(s):
    sc = league_scoring("nfl")
    v = lambda k: float(sc.get(k) or 0)  # noqa: E731
    pts = sum(float(s.get(k) or 0) * v(k) for k in ("sack", "int", "fum_rec", "ff", "safe", "def_td", "blk_kick",
                                                       "def_st_td", "def_st_ff", "def_st_fum_rec"))
    if "pts_allow" in s:
        a = float(s["pts_allow"])
        for lo, hi, k in ((0, 0, "pts_allow_0"), (1, 6, "pts_allow_1_6"), (7, 13, "pts_allow_7_13"),
                          (14, 20, "pts_allow_14_20"), (21, 27, "pts_allow_21_27"), (28, 34, "pts_allow_28_34"),
                          (35, 999, "pts_allow_35p")):
            if lo <= a <= hi:
                pts += v(k)
    return pts


_nfl_defenses_espn = nfl_defenses


def nfl_defenses(summary, event=None, week=None):
    """ESPN-based defences, corrected with Sleeper's own stats when it has them (they match Sleeper exactly)."""
    out = _nfl_defenses_espn(summary, event)
    stats = sleeper_week_stats(week)
    for d in out:
        s = stats.get(d["id"][4:]) if stats else None
        if not s or "pts_allow" not in s:
            continue
        d["pts"] = sleeper_def_points(s)
        bits = []
        if s.get("sack"):
            bits.append(f"{s['sack']:g} sack{'s' if s['sack'] != 1 else ''}")
        if s.get("int"):
            bits.append(f"{int(s['int'])} INT")
        if s.get("fum_rec") or s.get("def_st_fum_rec"):
            bits.append(f"{int((s.get('fum_rec') or 0) + (s.get('def_st_fum_rec') or 0))} fumble rec")
        if s.get("def_td") or s.get("def_st_td"):
            bits.append(f"{int((s.get('def_td') or 0) + (s.get('def_st_td') or 0))} TD")
        if s.get("safe"):
            bits.append(f"{int(s['safe'])} safety")
        bits.append(f"{int(s['pts_allow'])} pts allowed")
        d["line"] = " · ".join(bits)
    return out



# ------------------------------------------------ pre-game projections on every player row

_PJIDX = {}


def pregame_proj(sport, row, live=None, event=None):
    """The player's pre-game Sleeper projection (league scoring), or None if Sleeper has none.
    NBA: the projection for that exact game date (Sleeper projects every game separately)."""
    try:
        week = (live or {}).get("week") or fetch_url(f"{SLEEPER}/state/{sport}").get("week")
        if sport == "nba" and event is not None:
            gdate = parse_time(event["date"]).astimezone(ET).strftime("%Y-%m-%d")
            proj = {}
            for w in (week, int(week) - 1, int(week) + 1):
                proj = {pid: v for pid, d, v in nba_rows(w) if d == gdate}
                if proj:
                    break
        else:
            proj = nfl_projections(week) if sport == "nfl" else nba_projections(week)
        if not proj:
            return None
        rid = str(row.get("id") or "")
        if rid.startswith("def:"):
            v = proj.get(rid[4:])
            return round(v, 1) if v is not None else None
        if sport not in _PJIDX:
            ix = {}
            for pid, p in sleeper_players(sport).items():
                if p.get("full_name"):
                    if p.get("espn_id"):
                        ix[f"e:{p['espn_id']}"] = pid
                    ix[(name_key(p["full_name"]), p.get("team") or "")] = pid
                    ix.setdefault(name_key(p["full_name"]), pid)
            _PJIDX[sport] = ix
        ix = _PJIDX[sport]
        team = row.get("team") or ""
        if sport == "nba":
            team = NBA_ESPN_TO_SLEEPER.get(team, team)
        else:
            team = sl_team(team) if team else ""
        pid = (ix.get(f"e:{rid}") if rid else None) or ix.get((name_key(row.get("name", "")), team)) \
            or ix.get(name_key(row.get("name", "")))
        v = proj.get(str(pid)) if pid else None
        return round(v, 1) if v is not None else None
    except Exception as e:  # noqa: BLE001
        log(f"pre-game projection unavailable: {e}")
        return None


def game_frac(e, sport="nfl"):
    """Share of the game played, or None once it's over."""
    t = e["status"]["type"]
    if t.get("completed") or t.get("state") == "post":
        return None
    if t.get("state") == "pre":
        return 0.0
    if sport == "nfl":
        return game_elapsed(e)
    per = e["status"].get("period", 1)
    if per > 4:
        return 0.97
    try:
        clk = float(e["status"].get("clock", 0))
    except (TypeError, ValueError):
        clk = 0.0
    return min(0.99, ((per - 1) * 720 + (720 - clk)) / 2880)


def pj_box(pts, pj, elite=False, frac=None):
    """Points box with the pre-game projection underneath; gold when the player beat it.
    While the game is on (e.g. half-time) the bottom line shows where he's heading: pace = points + projection for the rest."""
    if pj is None:
        return f"<div class='fp{' el' if elite else ''}'>{fmt_pts(pts)}<small>FPTS</small></div>"
    beat = pj > 0 and pts >= pj
    if frac is not None and 0 < frac < 1:
        pace = pts + pj * (1 - frac)
        return (f"<div class='fp pjx{' beat' if beat else (' el' if elite else '')}'>{fmt_pts(pts)}"
                f"<small>proj {pj:.1f}</small><i class='{'up' if pace >= pj else 'dn'}'>pace {pace:.1f}</i></div>")
    d = pts - pj
    return (f"<div class='fp pjx{' beat' if beat else (' el' if elite else '')}'>{fmt_pts(pts)}"
            f"<small>proj {pj:.1f}</small><i class='{'up' if d >= 0 else 'dn'}'>{'+' if d >= 0 else '−'}{abs(d):.1f}</i></div>")


def pj_small(pts, pj, frac=None):
    if pj is None:
        return "<span class='pj5'></span>"
    if frac is not None and 0 < frac < 1:
        pace = pts + pj * (1 - frac)
        return (f"<span class='pj5{' beat' if pj > 0 and pts >= pj else ''}'>proj {pj:.1f}"
                f"<b class='{'up' if pace >= pj else 'dn'}'>pace {pace:.0f}</b></span>")
    d = pts - pj
    return (f"<span class='pj5{' beat' if pj > 0 and pts >= pj else ''}'>proj {pj:.1f}"
            f"<b class='{'up' if d >= 0 else 'dn'}'>{'+' if d >= 0 else '−'}{abs(d):.1f}</b></span>")


FAN_CSS += """
.pr2{grid-template-columns:44px 1fr 92px 168px 176px!important;height:62px!important}
.fp.pjx{font-size:19px;padding:3px 0 4px;line-height:1.05}
.fp.pjx small{font-size:10.5px;letter-spacing:.2px;font-weight:800;opacity:.95;margin-top:2px;text-transform:none}
.fp.pjx i{display:block;font-style:normal;font-size:10.5px;font-weight:900;margin-top:1px}
.fp.pjx i.up{color:#7ee2a8}.fp.pjx i.dn{color:#ff9a9e}
.fp.pjx.el i.up{color:#eafff2}
.fp.pjx.beat{background:linear-gradient(180deg,#ffd166,#ffb347);color:#1b1305;box-shadow:0 0 14px rgba(255,190,70,.55)}
.fp.pjx.beat i.up{color:#1b1305}
.tpr{grid-template-columns:24px 180px 56px 100px 196px 1fr!important}
.tpr .pj5{font-size:12px;font-weight:800;color:#c9bfb3;white-space:nowrap;font-variant-numeric:tabular-nums}
.tpr .pj5 b{margin-left:5px}.tpr .pj5 b.up{color:#7ee2a8}.tpr .pj5 b.dn{color:#ff9a9e}
.tpr .pj5.beat{color:#ffd166}.tpr .pj5.beat b.up{color:#ffd166}
"""



# ------------------------------------------------ pin NFL full-time posts for 16 hours (needs a Discord bot token)

BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
_PIN_QUEUE = []


def discord_api(method, path, body=None):
    """Discord REST call as the bot user. Returns (status, json-or-None)."""
    for _ in range(3):
        req = urllib.request.Request(
            "https://discord.com/api/v10" + path, method=method,
            data=json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None),
            headers={"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (sports-bot, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                t = r.read().decode()
                return r.status, (json.loads(t) if t else None)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    time.sleep(float(json.loads(e.read().decode()).get("retry_after", 2)) + 0.5)
                except Exception:  # noqa: BLE001
                    time.sleep(2)
                continue
            return e.code, None
        except Exception:  # noqa: BLE001
            return 0, None
    return 429, None


def pin_hours():
    return float((CONFIG.get("nfl") or {}).get("pin_final_hours", 16) or 0)


def _pin(ch, mid, on=True):
    meth = "PUT" if on else "DELETE"
    code, _ = discord_api(meth, f"/channels/{ch}/messages/pins/{mid}")
    if code in (404, 405):                                   # older endpoint
        code, _ = discord_api(meth, f"/channels/{ch}/pins/{mid}")
    return code


def _hide_pin_notice(ch, mid):
    """Remove Discord's "Donald pinned a message" line so the channel stays clean (needs Manage Messages)."""
    code, msgs = discord_api("GET", f"/channels/{ch}/messages?limit=5")
    for m in msgs or []:
        if m.get("type") == 6 and str((m.get("message_reference") or {}).get("message_id")) == str(mid):
            discord_api("DELETE", f"/channels/{ch}/messages/{m['id']}")


def pin_last_post(label):
    """Pin the message just posted; remember to unpin it after pin_hours()."""
    if not BOT_TOKEN or pin_hours() <= 0 or SILENT or DRY_RUN or CAPTURE is not None:
        return
    ch, mid = _LAST_MSG.get("channel_id"), _LAST_MSG.get("id")
    if not (ch and mid):
        return
    code = _pin(ch, mid, True)
    if code in (200, 204):
        _log_plain(f"pinned {label} for {pin_hours():g}h")
        _PIN_QUEUE.append({"ch": ch, "id": mid, "until": time.time() + pin_hours() * 3600, "label": label})
        try:
            if (CONFIG.get("nfl") or {}).get("hide_pin_notice", True):
                _hide_pin_notice(ch, mid)
        except Exception:  # noqa: BLE001
            pass
    else:
        log(f"Discord pin error ({code}) for {label}: check the bot's Pin Messages permission in the channel")


def pins_tick(st):
    """Store new pins in state and unpin the ones whose 16 hours are up."""
    pins = st.setdefault("pins", [])
    while _PIN_QUEUE:
        pins.append(_PIN_QUEUE.pop(0))
    if not pins or not BOT_TOKEN:
        return
    now, keep = time.time(), []
    for p in pins:
        if p["until"] > now:
            keep.append(p)
            continue
        code = _pin(p["ch"], p["id"], False)
        if code in (200, 204, 404):
            _log_plain(f"unpinned {p.get('label', p['id'])}")
        elif now - p["until"] < 6 * 3600:
            keep.append(p)                                     # try again on the next loop
        else:
            log(f"Discord unpin error ({code}) for {p.get('label', p['id'])}: giving up")
    st["pins"] = keep


_save_state_nopins = save_state


def save_state(st):
    try:
        pins_tick(st)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"pins: {ex}")
    return _save_state_nopins(st)


def _pinning(fn, label_of):
    def wrapped(*a, **k):
        _LAST_MSG.clear()
        ok = fn(*a, **k)
        lab = label_of(*a, **k)
        if ok and lab:
            try:
                pin_last_post(lab)
            except Exception as ex:  # noqa: BLE001
                _log_plain(f"pin skipped: {ex}")
        return ok
    return wrapped


def _final_label(event, *a, **k):
    away, home = teams_of(event)
    return f"NFL full-time {away['team']['abbreviation']} @ {home['team']['abbreviation']}"


post_prime_final = _pinning(post_prime_final, _final_label)
post_nfl_final = _pinning(post_nfl_final, _final_label)
post_slot = _pinning(post_slot, lambda mode, slot, live: f"NFL full-time slate ({len(slot)} games)"
                     if mode == "FULL-TIME" else None)



# ------------------------------------------------ trades: every Sleeper trade posted with a verdict

_FC = {}
_NBA_ROWS = {}
_TR_CTX = {}

SLOT_ELIG = {
    "QB": {"QB"}, "RB": {"RB"}, "WR": {"WR"}, "TE": {"TE"}, "K": {"K"}, "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"},
    "WRRB_FLEX": {"WR", "RB"},
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"}, "UTIL": {"PG", "SG", "SF", "PF", "C"},
}


def _ord(n):
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(int(n), f"{n}th")


def fantasycalc_values():
    """Dynasty trade values (players by Sleeper id, picks by name) from FantasyCalc, refreshed every 6 hours."""
    hit = _FC.get("nfl")
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    out = {"players": {}, "picks": {}}
    try:
        lg = league_settings("nfl") or {}
        sf = 2 if "SUPER_FLEX" in (lg.get("roster_positions") or []) else 1
        ppr = float((lg.get("scoring_settings") or {}).get("rec", 1) or 0)
        rows = fetch_url(f"https://api.fantasycalc.com/values/current?isDynasty=true&numQbs={sf}"
                         f"&numTeams={lg.get('total_rosters', 12)}&ppr={ppr:g}", tries=2)
        for r in rows or []:
            p = r.get("player") or {}
            if p.get("position") == "PICK":
                out["picks"][p.get("name", "")] = float(r.get("value") or 0)
            elif p.get("sleeperId"):
                out["players"][str(p["sleeperId"])] = {"value": float(r.get("value") or 0),
                                                       "redraft": float(r.get("redraftValue") or 0),
                                                       "rank": r.get("positionRank")}
    except Exception as e:  # noqa: BLE001
        _log_plain(f"FantasyCalc values unavailable: {e}")
    _FC["nfl"] = (time.time(), out)
    return out


def pick_value(season, rnd, tier=None):
    picks = fantasycalc_values()["picks"]
    for k in ([f"{season} {_ord(rnd)} ({tier})"] if tier else []) + [f"{season} {_ord(rnd)}"]:
        if k in picks:
            return picks[k]
    same = [v for k, v in picks.items() if k.split(" ")[1:2] == [_ord(rnd)] and "(" not in k]
    if same:
        return min(same)
    if rnd >= 3:
        two = pick_value(season, 2)
        return two * 0.4 if two else 300.0
    return {1: 2500.0, 2: 1000.0}.get(int(rnd), 300.0)


def nba_rows(week):
    """Sleeper's NBA projections for one week: one row per game -> [(pid, US date, league fpts)]."""
    if week in _NBA_ROWS:
        return _NBA_ROWS[week]
    out = []
    try:
        season = fetch_url(f"{SLEEPER}/state/nba").get("season")
        sc = league_scoring("nba")
        for r in fetch_url(f"https://api.sleeper.app/projections/nba/{season}/{week}?season_type=regular") or []:
            s = r.get("stats") or {}
            if not s:
                continue
            pts = sum(float(v or 0) * float(sc[k]) for k, v in s.items() if k in sc and isinstance(v, (int, float)))
            out.append((str(r.get("player_id")), r.get("date") or "", pts / (float(s.get("gp") or 1) or 1)))
    except Exception as e:  # noqa: BLE001
        _log_plain(f"NBA projections unavailable: {e}")
    _NBA_ROWS[week] = out
    return out


def best_lineup(slots, pids, pts, sport):
    """Best possible starting lineup points: fill the strictest slots first with the best eligible player."""
    allp = sleeper_players(sport)
    pos = {p: set((allp.get(p) or {}).get("fantasy_positions") or [(allp.get(p) or {}).get("position") or p]) for p in pids}
    for p in pids:
        if p.isalpha() and p.isupper() and sport == "nfl":
            pos[p] = {"DEF"}
    free = sorted((p for p in pids if pts.get(p, 0) > 0), key=lambda p: -pts.get(p, 0))
    total = 0.0
    for s in sorted(slots, key=lambda s: len(SLOT_ELIG.get(s, {s}))):
        el = SLOT_ELIG.get(s, {s})
        for p in free:
            if pos[p] & el:
                total += pts[p]
                free.remove(p)
                break
    return total


def _league_people(sport):
    lid = str(CONFIG[sport].get("sleeper_league_id")).strip()
    users = {u["user_id"]: u for u in fetch_url(f"{SLEEPER}/league/{lid}/users")}
    rosters = fetch_url(f"{SLEEPER}/league/{lid}/rosters")
    out = {}
    for r in rosters:
        u = users.get(r.get("owner_id")) or {}
        s = r.get("settings") or {}
        out[r["roster_id"]] = {
            "manager": u.get("display_name", "?"), "avatar": u.get("avatar"),
            "team_name": (u.get("metadata") or {}).get("team_name") or "",
            "w": int(s.get("wins") or 0), "l": int(s.get("losses") or 0), "t": int(s.get("ties") or 0),
            "pf": float(s.get("fpts") or 0) + float(s.get("fpts_decimal") or 0) / 100,
            "players": list(r.get("players") or []),
            "inactive": set(r.get("taxi") or []) | set(r.get("reserve") or []),
        }
    order = sorted(out, key=lambda k: (-(out[k]["w"] + 0.5 * out[k]["t"]), -out[k]["pf"]))
    for i, k in enumerate(order, 1):
        out[k]["rank"] = i
    return out


def trade_sides(t):
    """{roster_id: {"players": [pid], "picks": [pick], "faab": n, "gives_players": [pid], ...}}"""
    sides = {rid: {"players": [], "picks": [], "faab": 0, "gives": [], "gives_picks": [], "gives_faab": 0}
             for rid in t.get("roster_ids") or []}
    for pid, rid in (t.get("adds") or {}).items():
        sides.setdefault(rid, {"players": [], "picks": [], "faab": 0, "gives": [], "gives_picks": [], "gives_faab": 0})
        sides[rid]["players"].append(str(pid))
    for pid, rid in (t.get("drops") or {}).items():
        if rid in sides and str(pid) in (t.get("adds") or {}):
            sides[rid]["gives"].append(str(pid))
    for pk in t.get("draft_picks") or []:
        if pk.get("owner_id") in sides:
            sides[pk["owner_id"]]["picks"].append(pk)
        if pk.get("previous_owner_id") in sides:
            sides[pk["previous_owner_id"]]["gives_picks"].append(pk)
    for wb in t.get("waiver_budget") or []:
        if wb.get("receiver") in sides:
            sides[wb["receiver"]]["faab"] += int(wb.get("amount") or 0)
        if wb.get("sender") in sides:
            sides[wb["sender"]]["gives_faab"] += int(wb.get("amount") or 0)
    return sides


def trade_eval(sport, t):
    """Everything the trade card needs: assets, values, lineup impact, grades and Donald's take."""
    lg = league_settings(sport) or {}
    st_week = int(fetch_url(f"{SLEEPER}/state/{sport}").get("week") or 1)
    week = max(int(t.get("leg") or st_week or 1), 1)
    people = _league_people(sport)
    allp = sleeper_players(sport)
    slots = [s for s in (lg.get("roster_positions") or []) if s not in ("BN", "IR", "TAXI")]
    po = int((lg.get("settings") or {}).get("playoff_week_start") or (15 if sport == "nfl" else 22))
    last = po + 2
    sides = trade_sides(t)
    n_teams = len(people)

    # ---- projections, week by week (NFL) or game by game (NBA), from the trade week to the end of the fantasy season
    if sport == "nfl":
        weeks = list(range(week, last + 1))
        wk_pts = {w: nfl_projections(w) for w in weeks}

        def ros(p):
            return sum(wk_pts[w].get(p, 0) for w in weeks)
        n_units = max(len(weeks), 1)
    else:
        weeks = list(range(week, last + 1))
        days = {}
        for w in weeks:
            for pid, d, v in nba_rows(w):
                days.setdefault(d, {})[pid] = v
        games = {}
        for d in days.values():
            for pid in d:
                games[pid] = games.get(pid, 0) + 1

        def ros(p):
            return sum(d.get(p, 0) for d in days.values())
        n_units = max(len(weeks), 1)

    def lineup_total(roster):
        if sport == "nfl":
            return sum(best_lineup(slots, roster, wk_pts[w], "nfl") for w in weeks) / n_units
        return sum(best_lineup(slots, [p for p in roster if p in d], d, "nba") for d in days.values()) / n_units

    fc = fantasycalc_values() if sport == "nfl" else {"players": {}}
    standings = sorted(people, key=lambda k: people[k]["rank"])

    def tier_of(orig_rid):
        r = people.get(orig_rid, {}).get("rank", n_teams // 2)
        third = max(n_teams // 3, 1)
        return "Late" if r <= third else ("Early" if r > n_teams - third else "Mid")

    next_draft = str(int(lg.get("season") or datetime.now().year) + 1)
    out = []
    for rid, s in sides.items():
        pe = people.get(rid, {"manager": "?", "rank": 0, "w": 0, "l": 0, "t": 0, "players": [], "inactive": set()})
        assets = []
        for pid in s["players"]:
            p = allp.get(pid) or {}
            is_def = sport == "nfl" and p.get("position") == "DEF"
            r_ = ros(pid)
            per = r_ / (n_units if sport == "nfl" else max(games.get(pid, 0), 1))
            val = (fc["players"].get(pid) or {}).get("value", 0.0) if sport == "nfl" else r_
            assets.append({
                "kind": "player", "pid": pid,
                "name": (f"{p.get('last_name', pid)} D/ST" if is_def else p.get("full_name") or pid),
                "pos": p.get("position") or "", "team": p.get("team") or "FA",
                "age": p.get("age"), "injury": p.get("injury_status"),
                "ros": r_, "per": per, "value": float(val or 0),
                "img": (f"https://sleepercdn.com/images/team_logos/nfl/{pid.lower()}.png" if is_def else
                        f"https://sleepercdn.com/content/{sport}/players/thumb/{pid}.jpg"),
            })
        for pk in s["picks"]:
            season, rnd, orig = str(pk.get("season")), int(pk.get("round") or 1), pk.get("roster_id")
            tier = tier_of(orig) if season == next_draft else None
            v = pick_value(season, rnd, tier) if sport == "nfl" else 0.0
            assets.append({"kind": "pick", "name": f"{season} {_ord(rnd)}", "tier": tier,
                           "orig_name": people.get(orig, {}).get("manager"),
                           "via": people.get(orig, {}).get("manager") if orig not in (rid, pk.get("previous_owner_id")) or True else None,
                           "orig": orig, "value": v, "ros": 0.0, "per": 0.0})
        if s["faab"]:
            assets.append({"kind": "faab", "name": f"${s['faab']} FAAB", "value": s["faab"] * 8.0 if sport == "nfl" else 0.0,
                           "ros": 0.0, "per": 0.0})
        assets.sort(key=lambda a: -(a["value"] if sport == "nfl" else a["ros"]))
        # roster after the trade = today's roster with the trade applied (works for live and past trades)
        cur = [p for p in pe["players"] if p not in pe["inactive"]]
        after = list(dict.fromkeys([p for p in cur if p not in s["gives"]] + s["players"]))
        before = list(dict.fromkeys([p for p in cur if p not in s["players"]] + s["gives"]))
        out.append({"rid": rid, "pe": pe, "assets": assets, "gives": s["gives"],
                    "got_value": sum(a["value"] for a in assets),
                    "got_ros": sum(a["ros"] for a in assets),
                    "now": lineup_total(after) - lineup_total(before)})

    tot_val = sum(x["got_value"] for x in out) or 1.0
    po_spots = int((lg.get("settings") or {}).get("playoff_teams") or 6)
    weeks_left = max(po - st_week, 0)
    for x in out:
        x["share"] = x["got_value"] / tot_val
        fair = 1 / max(len(out), 1)
        contender = x["pe"]["rank"] <= po_spots + 1 and weeks_left >= 0
        rebuilding = x["pe"]["rank"] > n_teams - 3 and weeks_left <= 6
        if sport == "nba":
            w_now = 1.0
        else:
            w_now = 0.5 if contender else (0.15 if rebuilding else 0.35)
        long_score = max(-1, min(1, (x["share"] - fair) / 0.25))
        now_score = max(-1, min(1, x["now"] / (8.0 if sport == "nfl" else 30.0)))
        if sport == "nfl":
            x["score"] = (1 - w_now) * long_score + w_now * now_score
        else:
            x["score"] = 0.5 * max(-1, min(1, (x["share"] - fair) / 0.15)) + 0.5 * now_score
        x["contender"], x["rebuilding"] = contender, rebuilding
        g = 84 + 16 * x["score"]
        x["grade"] = next(l for cut, l in ((97, "A+"), (93, "A"), (90, "A-"), (87, "B+"), (83, "B"), (80, "B-"),
                                          (77, "C+"), (73, "C"), (70, "C-"), (64, "D"), (-999, "F")) if g >= cut)
    best = max(out, key=lambda x: x["score"])
    worst = min(out, key=lambda x: x["score"])
    gap = best["score"] - worst["score"]
    vw = max(out, key=lambda x: x["share"])
    nw_ = max(out, key=lambda x: x["now"])
    ev_split = (len(out) == 2 and vw is not nw_ and vw["share"] - min(x["share"] for x in out) >= 0.08
                and nw_["now"] - min(x["now"] for x in out) >= (3 if sport == "nfl" else 12))
    if gap < 0.25 and not ev_split:
        verdict, vclass = "FAIR DEAL", "fair"
    elif ev_split and gap < 0.6:
        verdict, vclass = "NOW vs LATER", "split"
    elif gap > 1.3 or max(x["share"] for x in out) > 0.7:
        verdict, vclass = "FLEECE ALERT", "fleece"
    else:
        verdict, vclass = f"{best['pe']['manager'].upper()} WINS IT", "win"
    ev = {"sport": sport, "week": week, "sides": out, "verdict": verdict, "vclass": vclass, "best": best,
          "worst": worst, "gap": gap, "league": lg.get("name") or CONFIG[sport].get("league_name", "League"),
          "created": t.get("created"), "dynasty": int((lg.get("settings") or {}).get("type") or 0) == 2,
          "po_spots": po_spots, "n_teams": n_teams, "vw": vw, "nw": nw_}
    ev["take"] = trade_take(ev)
    return ev


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def trade_take(ev):
    """Donald's opinion, in plain words, built from the numbers."""
    sport, sides = ev["sport"], ev["sides"]
    out = []
    unit = "a week" if sport == "nfl" else "a week"
    for x in sides:
        m, pe = x["pe"]["manager"], x["pe"]
        star = next((a for a in x["assets"] if a["kind"] == "player"), None)
        rec = f"{pe['w']}-{pe['l']}, {_ordinal(pe['rank'])}"
        if x["now"] >= 1.5:
            how = f"adds {x['now']:.1f} pts {unit} to their best lineup"
        elif x["now"] <= -1.5:
            how = f"loses {abs(x['now']):.1f} pts {unit} from their best lineup"
        else:
            how = "barely changes their lineup this season"
        if star:
            age = f", {star['age']}" if star.get("age") and sport == "nfl" else ""
            got = f"lands {star['name']} ({star['pos']}{age})"
        else:
            np_ = len([a for a in x["assets"] if a["kind"] == "pick"])
            got = "takes the pick" if np_ == 1 else (f"takes {np_} picks" if np_ else "takes the FAAB")
        picks = [a for a in x["assets"] if a["kind"] == "pick"]
        if star and picks:
            got += f" plus {'a ' + picks[0]['name'] if len(picks) == 1 else str(len(picks)) + ' picks'}"
        if sport == "nfl" and x["rebuilding"] and x["share"] > 0.5:
            mood = "playing the long game"
        elif sport == "nfl" and x["contender"] and x["now"] > 1.5:
            mood = "going for it"
        else:
            mood = ""
        out.append(f"{m} ({rec}) {got}" + (f", {mood}," if mood else "") + f" and {how}.")
    best, worst, vw, nw = ev["best"], ev["worst"], ev["vw"], ev["nw"]
    lo = min(sides, key=lambda x: x["share"])
    pct = round(vw["share"] * 100)
    what = "long-term value" if sport == "nfl" else "rest-of-season points"
    if ev["vclass"] == "fair":
        verb = "are" if sport == "nba" else "is"
        if all(x["now"] >= 1.5 for x in sides):
            out.append(f"{what.capitalize()} {verb} close ({pct}–{100 - pct}) and both lineups get better: a genuine win-win.")
        else:
            out.append(f"{what.capitalize()} {verb} close ({pct}–{100 - pct}) and neither side loses out, so this one works for both.")
    elif ev["vclass"] == "split":
        ctx = ""
        if nw["pe"]["rank"] <= ev["po_spots"] + 1:
            ctx = f", which is the right move for a {_ordinal(nw['pe']['rank'])}-placed team chasing the playoffs"
        elif nw["pe"]["rank"] > ev["po_spots"] + 2:
            ctx = f", though at {nw['pe']['w']}-{nw['pe']['l']} it may be too late to matter this year"
        out.append(f"{vw['pe']['manager']} wins on {what} ({pct}–{100 - pct}), but {nw['pe']['manager']} gets the "
                   f"instant boost (+{nw['now']:.1f} a week){ctx}.")
    elif vw is best and best["now"] >= 1.5:
        edge = f"a slight edge on {what} ({pct}–{100 - pct})" if pct < 56 else f"{pct}% of the {what}"
        out.append(f"{best['pe']['manager']} wins on both counts: {edge} and a better lineup straight away.")
    elif vw is best:
        out.append(f"On {what} it's {pct}–{100 - pct} to {best['pe']['manager']}.")
    else:
        out.append(f"{best['pe']['manager']} gives up a little {what} ({round(best['share'] * 100)}–"
                   f"{100 - round(best['share'] * 100)}) but the lineup boost (+{best['now']:.1f} a week) is worth more.")
    if sport == "nba":
        fits = max(x["now"] for x in sides) - min(x["now"] for x in sides) >= 12
        if fits and ev["vclass"] != "fair" and abs(vw["share"] - lo["share"]) < 0.08:
            f = max(sides, key=lambda x: x["now"])
            out.append(f"The points are close, but the deal fits {f['pe']['manager']}'s lineup far better.")
    flags = []
    for x in sides:
        for a in x["assets"]:
            if a["kind"] != "player":
                continue
            word = {"Out": "is out", "IR": "is on IR", "Doubtful": "is doubtful", "PUP": "is on the PUP list",
                    "Sus": "is suspended"}.get(a.get("injury") or "")
            if word:
                flags.append(f"{a['name']} {word}")
            elif sport == "nfl" and a["pos"] == "RB" and (a.get("age") or 0) >= 28:
                flags.append(f"{a['name']} is a {a['age']}-year-old RB, so the clock is ticking")
    if flags:
        out.append("Watch out: " + " and ".join(flags[:2]) + ".")
    return " ".join(out)


TRADE_CSS = """
body,.card{width:1240px!important}
.tw{display:grid;grid-template-columns:1fr 150px 1fr;gap:14px;margin-top:14px;align-items:stretch}
.tw.n3{grid-template-columns:1fr 1fr 1fr}
.ts{background:rgba(255,255,255,.06);border-radius:16px;padding:14px 16px;display:flex;flex-direction:column;gap:8px;
 border:2px solid transparent}
.ts.win{border-color:rgba(255,209,102,.75);box-shadow:0 0 24px rgba(255,190,70,.25)}
.th{display:flex;align-items:center;gap:12px}
.av{width:54px;height:54px;border-radius:50%;object-fit:cover;background:#2a3350;flex:none;display:flex;align-items:center;
 justify-content:center;font-weight:900;font-size:22px}
.th .mg{flex:1;min-width:0}.th .mg b{display:block;font-size:26px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.th .mg small{font-size:14px;color:#aab3c5;font-weight:700}
.gr{width:72px;height:72px;border-radius:14px;display:flex;align-items:center;justify-content:center;font-size:36px;font-weight:900;
 color:#10141f;flex:none}
.gr.a{background:linear-gradient(160deg,#ffe08a,#f5b942)}.gr.b{background:linear-gradient(160deg,#a7f3c8,#4fd18b)}
.gr.c{background:linear-gradient(160deg,#e3e7ef,#aab3c5)}.gr.d{background:linear-gradient(160deg,#ffb4a8,#f07a6a)}
.rcv{font-size:12px;letter-spacing:2px;color:#9aa3b5;font-weight:800;margin-top:2px}
.as{display:grid;grid-template-columns:52px 1fr auto;gap:12px;align-items:center;background:rgba(0,0,0,.22);border-radius:12px;padding:7px 10px}
.as img{width:52px;height:52px;border-radius:50%;object-fit:cover;object-position:top;background:#1d2436}
.as .an b{display:block;font-size:20px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.as .an small{font-size:13.5px;color:#aab3c5;font-weight:700}
.as .an .inj{color:#ff9a9e}
.as .av2{display:flex;flex-direction:column;align-items:flex-end;gap:3px}
.as .pv{font-size:17px;font-weight:900;font-variant-numeric:tabular-nums}
.as .pv small{font-size:11px;color:#aab3c5;font-weight:800;margin-left:3px}
.as .vv{font-size:11.5px;font-weight:900;letter-spacing:.8px;background:rgba(255,255,255,.1);border-radius:5px;padding:1px 6px;color:#dfe5f1}
.tk{width:52px;height:52px;border-radius:10px;background:repeating-linear-gradient(135deg,#f5b942 0 8px,#e8a52e 8px 16px);
 display:flex;align-items:center;justify-content:center;font-size:26px}
.imp{margin-top:auto;display:flex;justify-content:space-between;align-items:baseline;padding-top:4px}
.imp span{font-size:12px;letter-spacing:1.6px;color:#9aa3b5;font-weight:800}
.imp b{font-size:24px;font-weight:900;font-variant-numeric:tabular-nums}.imp b.up{color:#7ee2a8}.imp b.dn{color:#ff9a9e}.imp b.z{color:#cfd5e1}
.mid{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px}
.swap{width:96px;height:96px;border-radius:50%;background:rgba(255,255,255,.1);display:flex;align-items:center;justify-content:center;
 font-size:46px;border:2px solid rgba(255,255,255,.18)}
.vd{font-size:15px;font-weight:900;letter-spacing:1.6px;text-align:center;border-radius:8px;padding:6px 10px;line-height:1.2}
.vd.fair{background:rgba(126,226,168,.16);color:#7ee2a8}.vd.win{background:rgba(255,209,102,.18);color:#ffd166}
.vd.fleece{background:#ff5a5f;color:#fff;box-shadow:0 0 22px rgba(255,90,95,.55)}
.vd.split{background:rgba(183,148,244,.2);color:#d6bcfa}
.tbt{display:grid;grid-template-columns:1fr 420px;gap:14px;margin-top:14px}
.take{background:rgba(255,255,255,.06);border-radius:16px;padding:14px 18px;border-left:5px solid #ffd166}
.take .k{font-size:12px;letter-spacing:2px;color:#ffd166;font-weight:900;margin-bottom:6px}
.take p{font-size:19px;line-height:1.45;color:#eef1f7;font-weight:600}
.mtr{background:rgba(255,255,255,.06);border-radius:16px;padding:14px 18px;display:flex;flex-direction:column;gap:12px;justify-content:center}
.mtr .k{font-size:12px;letter-spacing:1.8px;color:#9aa3b5;font-weight:900;display:flex;justify-content:space-between}
.mtr .k b{color:#eef1f7}
.sb{height:16px;border-radius:999px;overflow:hidden;display:flex;background:rgba(255,255,255,.08);margin-top:5px}
.sb div{height:16px}
.c0{background:linear-gradient(90deg,#f5b942,#ffd166)}.c1{background:linear-gradient(90deg,#4fd1c5,#7ee8dd)}.c2{background:linear-gradient(90deg,#b794f4,#d6bcfa)}
.dot0{color:#ffd166}.dot1{color:#7ee8dd}.dot2{color:#d6bcfa}
"""


def _asset_short(a):
    if a["kind"] == "pick":
        return f"a {a['name']}"
    return a["name"] if a["kind"] == "faab" else short_name(a["name"]) if len(a["name"]) > 16 else a["name"]


def trade_headline(ev):
    sides = ev["sides"]
    if len(sides) != 2:
        return " ⇄ ".join(x["pe"]["manager"] for x in sides) + f" · {len(sides)}-team trade"
    top = max(sides, key=lambda x: max([a["value"] if ev["sport"] == "nfl" else a["ros"] for a in x["assets"]] or [0]))
    oth = sides[1] if top is sides[0] else sides[0]
    if not top["assets"]:
        return " ⇄ ".join(x["pe"]["manager"] for x in sides)
    lead = top["assets"][0]["name"] if top["assets"][0]["kind"] == "player" else _asset_short(top["assets"][0])
    more = f" + {len(top['assets']) - 1} more" if len(top["assets"]) > 1 else ""
    gets = [_asset_short(a) for a in oth["assets"]]
    pay = ", ".join(gets[:-1]) + " and " + gets[-1] if len(gets) > 1 else (gets[0] if gets else "nothing")
    return f"{top['pe']['manager']} lands {lead}{more} from {oth['pe']['manager']} for {pay}"


def trade_card_html(ev):
    import html as H
    sport = ev["sport"]
    sides = ev["sides"]
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    unit = "/wk" if sport == "nfl" else "/g"

    def grade_cls(g):
        return {"A": "a", "B": "b", "C": "c"}.get(g[0], "d")

    cols = []
    for i, x in enumerate(sides):
        pe = x["pe"]
        av = (f"<img class='av' src='https://sleepercdn.com/avatars/thumbs/{H.escape(pe['avatar'])}'>" if pe.get("avatar")
              else f"<div class='av'>{H.escape(pe['manager'][:1].upper())}</div>")
        rows = []
        for a in x["assets"][:6]:
            if a["kind"] == "player":
                meta = f"{a['pos']} · {a['team']}" + (f" · {a['age']}" if a.get("age") and sport == "nfl" else "")
                inj = f" · <span class='inj'>{H.escape(a['injury'])}</span>" if a.get("injury") else ""
                vv = (f"<span class='vv'>VALUE {a['value']:,.0f}</span>" if sport == "nfl" else
                      f"<span class='vv'>ROS {a['ros']:,.0f}</span>")
                rows.append(f"<div class='as'><img src='{H.escape(a['img'])}'><div class='an'><b>{H.escape(a['name'])}</b>"
                            f"<small>{H.escape(meta)}{inj}</small></div><div class='av2'><span class='pv'>{a['per']:.1f}"
                            f"<small>PROJ{unit}</small></span>{vv}</div></div>")
            elif a["kind"] == "pick":
                orig = a.get("orig_name") or "?"
                sub = f"{H.escape(orig)}'s pick" + (f" · projected {a['tier'].lower()} {a['name'].split(' ')[1]}"
                                                    if a.get("tier") else "")
                rows.append(f"<div class='as'><div class='tk'>🎟️</div><div class='an'><b>{H.escape(a['name'])}-round pick</b>"
                            f"<small>{sub}</small></div><div class='av2'><span class='vv'>VALUE {a['value']:,.0f}</span></div></div>")
            else:
                rows.append(f"<div class='as'><div class='tk'>💰</div><div class='an'><b>{H.escape(a['name'])}</b>"
                            f"<small>waiver budget</small></div><div class='av2'></div></div>")
        if len(x["assets"]) > 6:
            rows.append(f"<div class='rcv'>+ {len(x['assets']) - 6} more</div>")
        nw = x["now"]
        cls = "up" if nw >= 0.5 else ("dn" if nw <= -0.5 else "z")
        imp_k = "BEST LINEUP, REST OF SEASON" if sport == "nfl" else "BEST LINEUP PER WEEK, REST OF SEASON"
        cols.append(
            f"<div class='ts{' win' if x is ev['best'] and ev['vclass'] != 'fair' else ''}'>"
            f"<div class='th'>{av}<div class='mg'><b>{H.escape(pe['manager'])}</b>"
            f"<small>{pe['w']}-{pe['l']}{'-' + str(pe['t']) if pe['t'] else ''} · {_ordinal(pe['rank'])} in the league</small></div>"
            f"<div class='gr {grade_cls(x['grade'])}'>{x['grade']}</div></div>"
            f"<div class='rcv'>RECEIVES</div>{''.join(rows)}"
            f"<div class='imp'><span>{imp_k}</span><b class='{cls}'>{'+' if nw >= 0 else '−'}{abs(nw):.1f}"
            f"<small style='font-size:13px;color:#aab3c5'> pts/wk</small></b></div></div>")

    mid = (f"<div class='mid'><div class='swap'>⇄</div><div class='vd {ev['vclass']}'>"
           f"{ {'fleece': '🚨 ', 'win': '🏆 ', 'split': '⏳ '}.get(ev['vclass'], '⚖️ ')}{H.escape(ev['verdict'])}</div></div>")
    if len(sides) == 2:
        grid = f"<div class='tw'>{cols[0]}{mid}{cols[1]}</div>"
    else:
        grid = f"<div class='tw n3'>{''.join(cols)}</div>"

    def split(key, label, fmt):
        tot = sum(max(x[key], 0) for x in sides) or 1
        segs = "".join(f"<div class='c{i}' style='width:{max(x[key], 0) / tot * 100:.1f}%'></div>" for i, x in enumerate(sides))
        lab = " · ".join(f"<b class='dot{i}'>{H.escape(x['pe']['manager'])} {fmt(x)}</b>" for i, x in enumerate(sides))
        return f"<div><div class='k'><span>{label}</span></div><div class='sb'>{segs}</div><div class='k' style='margin-top:6px'>{lab}</div></div>"

    if sport == "nfl":
        meters = split("got_value", "LONG-TERM VALUE (DYNASTY)", lambda x: f"{x['share'] * 100:.0f}%")
        meters += split("got_ros", "PROJECTED POINTS, REST OF SEASON", lambda x: f"{x['got_ros']:.0f}")
    else:
        for x in sides:
            x["pos_now"] = max(x["now"], 0) + 0.001
        meters = split("got_ros", "PROJECTED POINTS, REST OF SEASON", lambda x: f"{x['got_ros']:.0f}")
        meters += split("pos_now", "LINEUP BOOST PER WEEK", lambda x: f"{x['now']:+.1f}")
    bottom = (f"<div class='tbt'><div class='take'><div class='k'>🗣️ DONALD'S TAKE</div><p>{H.escape(ev['take'])}</p></div>"
              f"<div class='mtr'>{meters}</div></div>")
    names = trade_headline(ev)
    when = ""
    if ev.get("created"):
        when = " · " + datetime.fromtimestamp(ev["created"] / 1000, UK).strftime("%a %-d %b %H:%M")
    body = (f"<div class='top'><div class='lg'><img src='{logo}'>🤝 TRADE · {H.escape(ev['league'].upper())} · WEEK {ev['week']}"
            f"{H.escape(when.upper())}</div><div class='pill'>{'DYNASTY' if ev['dynasty'] else 'REDRAFT'}</div></div>"
            f"<div class='hl'><div class='h1'>{H.escape(names)}</div></div>{grid}{bottom}")
    w = 1100 if max(len(x["assets"]) for x in sides) <= 2 and len(sides) == 2 else 1240
    html_text = _card("--a:#d97757;--h:#5b7bd5" if sport == "nfl" else "--a:#ee6730;--h:#ffb347", body, None,
                      HL_CSS + TRADE_CSS.replace("width:1240px", f"width:{w}px"), width=w)
    if sport == "nba":
        html_text = _nba_theme(html_text, None, "TRADE")
    return html_text


def post_trade(sport, t):
    ev = trade_eval(sport, t)
    html_text = trade_card_html(ev)
    hook = NFL_HOOK if sport == "nfl" else NBA_HOOK
    role = str(((CONFIG.get(sport) or {}).get("trades") or {}).get("mention_role_id") or "").strip()
    content = f"<@&{role}>" if role.isdigit() else None       # e.g. @NFLfantasy: pinged on every trade
    png = render_card(html_text)
    if sport == "nba":
        _POSTER["name"] = (CONFIG.get("nba") or {}).get("bot_name") or "Donald 🏀"
    try:
        if png:
            return discord(hook, [], "trade", content=content, files={"trade.png": png})
        names = " ⇄ ".join(x["pe"]["manager"] for x in ev["sides"])
        return discord(hook, {"title": f"🤝 Trade: {names}", "description": ev["take"]}, "trade", content=content)
    finally:
        _POSTER.clear()


def maybe_trades(st):
    """Post every completed trade in both leagues exactly once (checked every few minutes)."""
    for sport in ("nfl", "nba"):
        cfg = (CONFIG.get(sport) or {}).get("trades") or {}
        lid = str(CONFIG[sport].get("sleeper_league_id") or "").strip()
        if not lid or not cfg.get("enabled", True):
            continue
        try:
            week = int(fetch_url(f"{SLEEPER}/state/{sport}").get("week") or 0)
            found = []
            for w in sorted({max(week - 1, 0), week, week + 1}):
                found += [t for t in (fetch_url(f"{SLEEPER}/league/{lid}/transactions/{w}") or [])
                          if t.get("type") == "trade" and t.get("status") == "complete"]
        except Exception as e:  # noqa: BLE001
            _log_plain(f"{sport} trades unavailable: {e}")
            continue
        seen = st.setdefault("trades_seen", {})
        first = sport not in seen
        done = seen.setdefault(sport, [])
        tries = st.setdefault("trade_tries", {})
        for t in sorted(found, key=lambda t: t.get("created") or 0):
            tid = str(t.get("transaction_id"))
            if tid in done:
                continue
            if first:                                   # first run: remember existing trades, don't re-post them
                done.append(tid)
                continue
            ok = False
            try:
                ok = post_trade(sport, t)
            except Exception as e:  # noqa: BLE001
                log(f"trade post error ({sport} {tid}): {e}")
            if ok:
                done.append(tid)
                tries.pop(tid, None)
            else:
                tries[tid] = tries.get(tid, 0) + 1
                if tries[tid] == 3:
                    alert(f"⚠️ A {sport.upper()} trade ({tid}) has failed to post 3 times; I'll keep retrying.",
                          f"trade-{tid}", 24)
        seen[sport] = done[-400:]


_maybe_daily_race_trades = maybe_daily_race


def maybe_daily_race(st):
    try:
        maybe_trades(st)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"trades check failed: {ex}")
    return _maybe_daily_race_trades(st)



# ------------------------------------------------ owner lookup: fresh rosters + careful ESPN -> Sleeper matching

_LEAGUE_T = {}
_RPID = {}
_PIDX = {}
_SEEN_TEAM = {}
_league_index_once = league_index
_league_owner_simple = league_owner


def league_index(sport="nfl"):
    """Same as before, but re-read from Sleeper every 10 minutes so trades and waiver moves show up mid-run."""
    if time.time() - _LEAGUE_T.get(sport, 0) > 600:
        old = _LEAGUE.pop(sport, None)
        _RPID.pop(sport, None)
        _LEAGUE_T[sport] = time.time()
        new = _league_index_once(sport)
        if not new and old:                         # Sleeper hiccup: keep the last good copy
            _LEAGUE[sport] = old
            return old
        return new
    return _league_index_once(sport)


def roster_pid_index(sport="nfl"):
    """Sleeper player id -> who rosters him (same entries as league_index)."""
    league_index(sport)
    if sport in _RPID:
        return _RPID[sport]
    out = {}
    lid = str(CONFIG[sport].get("sleeper_league_id") or "").strip()
    if lid:
        try:
            users = {u["user_id"]: u for u in fetch_url(f"{SLEEPER}/league/{lid}/users")}
            players = sleeper_players(sport)
            for r in fetch_url(f"{SLEEPER}/league/{lid}/rosters"):
                u = users.get(r.get("owner_id")) or {}
                starters = set(r.get("starters") or [])
                for pid in r.get("players") or []:
                    p = players.get(pid) or {}
                    out[pid] = {"manager": u.get("display_name", "?"),
                                "team_name": (u.get("metadata") or {}).get("team_name") or "",
                                "starting": pid in starters,
                                "name": p.get("full_name") or f"{p.get('last_name', pid)} D/ST",
                                "pos": p.get("position") or "", "team": p.get("team") or pid}
        except Exception as e:  # noqa: BLE001
            _log_plain(f"Sleeper rosters unavailable: {e}")
    _RPID[sport] = out
    return out


def _player_index(sport):
    if sport in _PIDX:
        return _PIDX[sport]
    ix = {"espn": {}, "name_team": {}, "name": {}, "last_team": {}}
    for pid, p in sleeper_players(sport).items():
        if p.get("espn_id"):
            ix["espn"][str(p["espn_id"])] = pid
        fn = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
        if not fn or p.get("position") == "DEF":
            continue
        nk, tm = name_key(fn), p.get("team") or ""
        ix["name_team"][(nk, tm)] = pid
        ix["name"].setdefault(nk, []).append(pid)
        ix["last_team"].setdefault((name_key(p.get("last_name", "")), tm), []).append(pid)
    _PIDX[sport] = ix
    return ix


def league_owner(espn_id, name="", sport="nfl"):
    """Who rosters this ESPN player: ESPN id, then name + team, then a unique name (traded players),
    then surname + team (nicknames like Bam/Zonovan Knight)."""
    eid = str(espn_id or "")
    if eid.startswith("def:"):
        return league_index(sport).get(eid)
    rp = roster_pid_index(sport)
    if not rp:
        return _league_owner_simple(espn_id, name, sport)
    ix = _player_index(sport)
    pid = ix["espn"].get(eid) if eid else None
    if pid:
        return rp.get(pid)
    team = _SEEN_TEAM.get((sport, eid)) or _SEEN_TEAM.get((sport, name_key(name)))
    tm = (sl_team(team) if sport == "nfl" else NBA_ESPN_TO_SLEEPER.get(team, team)) if team else ""
    nk = name_key(name)
    if tm and (nk, tm) in ix["name_team"]:
        return rp.get(ix["name_team"][(nk, tm)])
    cands = ix["name"].get(nk, [])
    if len(cands) == 1:
        return rp.get(cands[0])
    owned = [c for c in cands if c in rp]
    if len(owned) == 1:
        ptm = (sleeper_players(sport).get(owned[0]) or {}).get("team") or ""
        if not tm or not ptm or ptm == tm:
            return rp.get(owned[0])
    if tm and name.split():
        last = ix["last_team"].get((name_key(name.split()[-1]), tm), [])
        if len(last) == 1:
            return rp.get(last[0])
    return None


_nfl_fantasy_seen = nfl_fantasy


def nfl_fantasy(summary, *a, **k):
    rows = _nfl_fantasy_seen(summary, *a, **k)
    for r in rows or []:
        if r.get("team") and not str(r.get("id", "")).startswith("def:"):
            _SEEN_TEAM[("nfl", str(r.get("id")))] = r["team"]
            _SEEN_TEAM[("nfl", name_key(r.get("name", "")))] = r["team"]
    return rows


_nba_players_seen = nba_players


def nba_players(summary, *a, **k):
    rows = _nba_players_seen(summary, *a, **k)
    for r in rows or []:
        if r.get("team"):
            _SEEN_TEAM[("nba", name_key(r.get("name", "")))] = r["team"]
    return rows




def nba_projections(week):
    """Sleeper projections scored with the league's settings: {pid: average fpts per game this week}."""
    if week in _NBA_PROJ:
        return _NBA_PROJ[week]
    tot, n = {}, {}
    for pid, d, v in nba_rows(week):
        tot[pid] = tot.get(pid, 0.0) + v
        n[pid] = n.get(pid, 0) + 1
    _NBA_PROJ[week] = {pid: tot[pid] / n[pid] for pid in tot}
    return _NBA_PROJ[week]


# ------------------------------------------------ weekly recap, landscape: (1) results + awards + lineup report, (2) title race

def lineup_report(sport, week, live, settings):
    """NFL: each manager's score vs their best possible lineup (hindsight), and whether it cost them the win."""
    if sport != "nfl":
        return []
    slots = [s for s in (settings.get("roster_positions") or []) if s not in ("BN", "IR", "TAXI")]
    lid = str(CONFIG[sport].get("sleeper_league_id")).strip()
    inactive = {}
    try:
        for r in fetch_url(f"{SLEEPER}/league/{lid}/rosters"):
            inactive[r["roster_id"]] = set(r.get("taxi") or []) | set(r.get("reserve") or [])
    except Exception:  # noqa: BLE001
        pass
    opp = {}
    for a, b in live["pairs"]:
        opp[a], opp[b] = b, a
    rows = []
    for rid, x in live["rosters"].items():
        if not x.get("matchup_id"):
            continue
        elig = list(dict.fromkeys(x["starters"] + [p for p in x["players"] if p not in inactive.get(rid, set())]))
        pts = {p: float(x["pp"].get(p) or 0) for p in elig}
        best = max(best_lineup(slots, elig, pts, "nfl"), x["points"])
        o = live["rosters"].get(opp.get(rid), {})
        lost = o and x["points"] < o.get("points", 0)
        bench = [p for p in elig if p not in x["starters"]]
        top_b = max(bench, key=lambda p: pts.get(p, 0), default=None)
        rows.append({"rid": rid, "manager": x["manager"], "pts": x["points"], "best": best,
                     "eff": 100 * x["points"] / best if best else 100.0, "left": best - x["points"],
                     "cost": bool(lost and best > o.get("points", 0)), "opp": o.get("manager", ""),
                     "top_bench": top_b, "top_bench_pts": pts.get(top_b, 0) if top_b else 0})
    rows.sort(key=lambda r: (-r["eff"], -r["pts"]))
    return rows


RECAP2_CSS = """
.rw{display:grid;grid-template-columns:520px 1fr;gap:16px;margin-top:12px}
.sec2{font-size:13px;letter-spacing:2px;color:#9aa3b5;font-weight:800;margin-bottom:8px}
.rs{display:flex;flex-direction:column;gap:7px}
.r2{display:grid;grid-template-columns:1fr 74px 16px 74px 1fr;align-items:center;gap:8px;background:rgba(255,255,255,.06);
 border-radius:12px;padding:9px 14px;font-variant-numeric:tabular-nums}
.r2 .n{font-size:19px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.r2 .n.r{text-align:right}
.r2 .lo{color:#8b95a8!important;font-weight:700}.r2 .p{font-size:24px;font-weight:900;text-align:center}
.r2 i{color:#5d6679;font-style:normal;text-align:center}
.r2 .n small{display:block;font-size:12px;color:#7ee2a8;font-weight:800;letter-spacing:.5px}
.aw2{display:grid;grid-template-columns:repeat(4,1fr);gap:9px}
.t2{background:rgba(255,255,255,.06);border-radius:12px;padding:11px 13px;min-height:104px}
.t2 .k{font-size:12px;letter-spacing:1.4px;color:#9aa3b5;font-weight:800}
.t2 .v{font-size:20px;font-weight:900;margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t2 .x{font-size:14px;color:#cfd5e1;margin-top:3px;line-height:1.3}
.lr{margin-top:14px}
.lrg{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px}
.l2{display:grid;grid-template-columns:28px 1fr 64px 64px 150px 64px;align-items:center;gap:10px;background:rgba(255,255,255,.05);
 border-radius:10px;padding:6px 12px;font-size:16px;font-variant-numeric:tabular-nums}
.l2.h{background:none;font-size:11px;letter-spacing:1.3px;color:#9aa3b5;font-weight:800;padding:0 12px}
.l2 .m{font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.l2 .m em{font-style:normal;margin-left:6px}
.l2 .e{display:grid;grid-template-columns:46px 1fr;align-items:center;gap:6px;font-weight:900}
.l2 .bar{height:8px;border-radius:4px;background:rgba(255,255,255,.08);overflow:hidden}.l2 .bar div{height:8px;border-radius:4px}
.l2 .lf{color:#ff9a9e;font-weight:800;text-align:right}.l2 .lf.z{color:#7ee2a8}
.tr2{display:grid;grid-template-columns:34px 1fr 70px 70px 200px 200px;align-items:center;gap:12px;height:44px;
 font-size:18px;font-variant-numeric:tabular-nums;border-radius:9px;padding:0 12px}
.tr2:nth-child(odd){background:rgba(255,255,255,.04)}
.tr2.h{height:28px;font-size:12px;letter-spacing:1.4px;color:#9aa3b5;font-weight:800;background:none}
.tr2.cut{box-shadow:inset 0 -2px 0 rgba(255,209,102,.6)}
.tr2 .rk{color:#9aa3b5;font-weight:900}.tr2 .m{font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tr2 .o{display:grid;grid-template-columns:52px 1fr;align-items:center;gap:8px;font-weight:900}
.tr2 .bar{height:9px;border-radius:5px;background:rgba(255,255,255,.08);overflow:hidden}.tr2 .bar div{height:9px;border-radius:5px}
.tw2{display:grid;grid-template-columns:1fr 330px;gap:16px;margin-top:12px}
.sd{display:flex;flex-direction:column;gap:10px}
.sd .col{background:rgba(255,255,255,.06);border-radius:12px;padding:12px 14px;font-size:16px;line-height:1.45;color:#dfe5f1}
.sd .col b{color:#fff}.sd .ch{font-size:12px;letter-spacing:1.6px;color:#9aa3b5;font-weight:800;margin-bottom:6px}
.fav{font-size:30px;font-weight:900;color:#ffd166;line-height:1.1}
"""


def recap_wide_html(sport, week, live, settings):
    """Two landscape recap cards (results + awards, then the title race) and the headline lines."""
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
    allr = [x for x in live["rosters"].values() if x.get("matchup_id")]
    allpts = sorted((x["points"] for x in allr), reverse=True)

    def allplay(p):                                      # how many of the other teams this score would have beaten
        return sum(1 for q in allpts if q < p), len(allpts) - 1

    top = max(allr, key=lambda x: x["points"])
    blow = max(games, key=lambda g: g[0]["points"] - g[1]["points"])
    close = min(games, key=lambda g: g[0]["points"] - g[1]["points"])
    lucky = min((w for w, _ in games), key=lambda x: x["points"])
    unlucky = max((l for _, l in games), key=lambda x: x["points"])
    pow_ = max(starters, key=lambda p: p["pts"], default=None)
    bb = max(bench, key=lambda p: p["pts"], default=None)
    lw, ln_ = allplay(lucky["points"])
    uw, un = allplay(unlucky["points"])
    tiles = [
        ("🏆 TOP SCORE", top["manager"], f"{top['points']:.1f} pts"),
        ("⭐ PLAYER OF THE WEEK", pow_["name"] if pow_ else "—", f"{pow_['pts']:.1f} pts for {pow_['manager']}" if pow_ else ""),
        ("💥 BIGGEST WIN", blow[0]["manager"], f"by {blow[0]['points'] - blow[1]['points']:.1f} over {blow[1]['manager']}"),
        ("😬 CLOSEST FINISH", close[0]["manager"], f"by {close[0]['points'] - close[1]['points']:.1f} over {close[1]['manager']}"),
        ("🍀 LUCKIEST WIN", lucky["manager"], f"won with {lucky['points']:.1f}, which beats only {lw} of {ln_} teams"),
        ("🥶 UNLUCKIEST LOSS", unlucky["manager"], f"lost with {unlucky['points']:.1f}, which beats {uw} of {un} teams"),
    ]
    lr = lineup_report(sport, week, live, settings)
    if lr:
        mgr = lr[0]
        tiles.append(("🧠 MANAGER OF THE WEEK", mgr["manager"], f"{mgr['eff']:.0f}% of their best possible lineup"))
        costly = [r for r in lr if r["cost"]]
        worst = max(costly or lr, key=lambda r: r["left"])
        bp = players.get(worst["top_bench"]) or {}
        bname = bp.get("full_name") or (f"{bp.get('last_name', '')} D/ST" if bp else "")
        tiles.append(("💔 COSTLY CALL" if worst["cost"] else "🪑 BENCH BLUNDER", worst["manager"],
                      f"left {worst['left']:.1f} on the bench" + (f" ({H.escape(bname)} {worst['top_bench_pts']:.1f})" if bname else "")
                      + (f" and lost to {worst['opp']}" if worst["cost"] else "")))
    else:
        low = min(allr, key=lambda x: x["points"])
        tiles.append(("🪑 BENCH BLUNDER", bb["name"] if bb else "—", f"{bb['pts']:.1f} pts on {bb['manager']}'s bench" if bb else ""))
        tiles.append(("📉 LOW SCORE", low["manager"], f"{low['points']:.1f} pts"))

    res = "".join(
        f"<div class='r2'><span class='n'>{H.escape(w['manager'])}</span><span class='p'>{w['points']:.1f}</span><i>·</i>"
        f"<span class='p lo'>{l['points']:.1f}</span><span class='n r lo'>{H.escape(l['manager'])}</span></div>"
        for w, l in games)
    aw = "".join(f"<div class='t2'><div class='k'>{k}</div><div class='v'>{H.escape(v)}</div><div class='x'>{x}</div></div>"
                 for k, v, x in tiles)
    lr_html = ""
    if lr:
        hdr = ("<div class='l2 h'><span>#</span><span>MANAGER</span><span>SCORED</span><span>BEST</span>"
               "<span>LINEUP IQ</span><span style='text-align:right'>LEFT</span></div>")
        cells = []
        for i, r in enumerate(lr, 1):
            col = "#7ee2a8" if r["eff"] >= 95 else ("#ffd166" if r["eff"] >= 88 else "#ff9a9e")
            cells.append(f"<div class='l2'><span class='rk5'>{i}</span><span class='m'>{H.escape(r['manager'])}"
                         f"{'<em>💔</em>' if r['cost'] else ''}</span><span>{r['pts']:.1f}</span><span>{r['best']:.1f}</span>"
                         f"<span class='e'><span>{r['eff']:.0f}%</span><div class='bar'><div style='width:{r['eff']:.0f}%;"
                         f"background:{col}'></div></div></span><span class='lf{' z' if r['left'] < 0.05 else ''}'>"
                         f"{'—' if r['left'] < 0.05 else '−' + format(r['left'], '.1f')}</span></div>")
        half = (len(cells) + 1) // 2
        lr_html = (f"<div class='lr'><div class='sec2'>LINEUP REPORT · SCORE VS BEST POSSIBLE LINEUP · 💔 = THE BENCH COST THEM THE WIN</div>"
                   f"<div class='lrg'><div class='rs' style='gap:5px'>{hdr}{''.join(cells[:half])}</div>"
                   f"<div class='rs' style='gap:5px'>{hdr}{''.join(cells[half:])}</div></div></div>")
    head = (f"<div class='top'><div class='lg'><img src='{logo}'>{H.escape(league.upper())} · WEEK {week}</div>"
            f"<div class='pill'>WEEKLY RECAP · 1 OF 2</div></div>"
            f"<div class='hl'><div class='h1'>Week {week}: {H.escape(top['manager'])} top-scores with {top['points']:.1f}</div>"
            f"<div class='h2'>😬 Closest: <b>{H.escape(close[0]['manager'])}</b> beat {H.escape(close[1]['manager'])} by "
            f"{close[0]['points'] - close[1]['points']:.1f}</div></div>")
    body_a = (f"{head}<div class='rw'><div><div class='sec2'>RESULTS</div><div class='rs'>{res}</div></div>"
              f"<div><div class='sec2'>AWARDS</div><div class='aw2'>{aw}</div></div></div>{lr_html}")

    # ---- card 2: title race
    def bar(pct, col):
        return f"<div class='bar'><div style='width:{max(pct, 1.5):.1f}%;background:{col}'></div></div>"
    trs = ["<div class='tr2 h'><span>#</span><span>MANAGER</span><span>W-L</span><span>PF</span>"
           "<span>PLAYOFFS</span><span>TITLE</span></div>"]
    for i, r in enumerate(table, 1):
        x = rec[r]
        wl = f"{x['w']}-{x['l']}" + (f"-{x['t']}" if x["t"] else "")
        po, ti = odds[r]
        trs.append(f"<div class='tr2{' cut' if i == n_po else ''}'><span class='rk'>{i}</span>"
                   f"<span class='m'>{H.escape(names.get(r, '?'))}</span><span>{wl}</span><span>{x['pf']:.0f}</span>"
                   f"<span class='o'><span>{po:.0f}%</span>{bar(po, 'linear-gradient(90deg,#d97757,#f2a77f)')}</span>"
                   f"<span class='o'><span>{ti:.0f}%</span>{bar(ti * 2.5, 'linear-gradient(90deg,#e8b13c,#ffe08a)')}</span></div>")
    fav = max(odds, key=lambda r: odds[r][1])
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
            nxt = (f"<b>{H.escape(names[a])}</b> ({wl(a)}) vs <b>{H.escape(names[b])}</b> ({wl(b)})<br>"
                   f"playoff odds {odds[a][0]:.0f}% and {odds[b][0]:.0f}%")
    except Exception as e:  # noqa: BLE001
        _log_plain(f"recap next week unavailable: {e}")
    left = max(0, po_start - week - 1)
    ctx = (f"Playoffs start week <b>{po_start}</b>. Top <b>{n_po}</b> of {len(table)} qualify, "
           f"<b>{left}</b> regular-season week{'s' if left != 1 else ''} left.") if left else \
          f"Playoffs start week <b>{po_start}</b>. Top <b>{n_po}</b> qualify."
    side = (f"<div class='sd'><div class='col'><div class='ch'>TITLE FAVOURITE</div><div class='fav'>{H.escape(names[fav])}</div>"
            f"{odds[fav][1]:.0f}% to win it all</div>"
            + (f"<div class='col'><div class='ch'>GAME TO WATCH NEXT WEEK</div>{nxt}</div>" if nxt else "")
            + f"<div class='col'><div class='ch'>THE RACE</div>{ctx} The gold line is the playoff cut.</div></div>")
    body_b = (f"<div class='top'><div class='lg'><img src='{logo}'>{H.escape(league.upper())} · WEEK {week}</div>"
              f"<div class='pill'>TITLE RACE · 2 OF 2</div></div>"
              f"<div class='tw2'><div><div class='sec2'>STANDINGS · SIMULATED 4,000 TIMES</div>{''.join(trs)}</div>{side}</div>")
    css_vars = "--a:#1faa59;--h:#5b7bd5"
    a = _card(css_vars, body_a, None, HL_CSS + RECAP2_CSS + "body,.card{width:1400px!important}", width=1400)
    b = _card(css_vars, body_b, None, RECAP2_CSS + "body,.card{width:1300px!important}", width=1300)
    if sport == "nba":
        a, b = _nba_theme(a, None, "WEEKLY RECAP"), _nba_theme(b, None, "TITLE RACE")
    return a, b


def post_weekly_recap_wide(sport, week, live, settings):
    a, b = recap_wide_html(sport, week, live, settings)
    league = CONFIG[sport].get("league_name", "League")
    hook = NFL_HOOK if sport == "nfl" else NBA_HOOK
    emoji = "🏈" if sport == "nfl" else "🏀"
    ok = _post_card(hook, a, f"📊 **WEEK {week} RECAP** · {emoji} {league}", {}, "recap.png")
    if ok:
        if not _post_card(hook, b, f"🏁 **TITLE RACE** · {emoji} {league}", {}, "race.png"):
            log(f"{sport} recap error: the title-race card did not post")
    return ok


_post_weekly_recap_named = post_weekly_recap_wide


# ------------------------------------------------ landscape game cards (readable in Discord without opening)

WIDE_GAME_CSS = """
body,.card{width:1640px!important}
.in{display:grid;grid-template-columns:1fr 1fr;column-gap:16px;align-items:stretch}
.in>.top,.in>.nbar,.in>.hd,.in>.pl2,.in>.lg2,.in>.more{grid-column:1/-1}
.in>.hl{grid-column:1;margin-top:12px!important;margin-bottom:0!important}
.in>.sb5,.in>.sbx{grid-column:2;margin-top:12px!important}
.in>.top+.sb5,.in>.top+.sbx{grid-column:1/-1}
.pl2{display:grid!important;grid-auto-flow:column;column-gap:12px}
.pr2{grid-template-columns:44px 1fr 90px 135px 138px!important}
.pr2 .mt{font-size:12px!important}.pr2 .mt b{font-size:14px!important}
.pr2>div{min-width:0}
.pr2 .nm2,.pr2 .ln{white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important}
.pr2 .ln{font-size:14px!important}.pr2 .nm2{font-size:18px!important}
"""


def _widen(html_text):
    n = html_text.count("class='pr2")
    rows = (n + 1) // 2
    extra = WIDE_GAME_CSS + (f".pl2{{grid-template-rows:repeat({rows},auto)}}" if n > 3 else
                             ".pl2{display:flex!important}")
    return html_text.replace("</style>", extra + "</style>", 1)


_nfl_game_fantasy_html_tall = nfl_game_fantasy_html


def nfl_game_fantasy_html(event, summary, live, ol, base, max_rows=30):
    return _widen(_nfl_game_fantasy_html_tall(event, summary, live, ol, base, max_rows))


_nba_game_fantasy_html_tall = nba_game_fantasy_html


def nba_game_fantasy_html(event, summary, live, max_rows=7):
    return _widen(_nba_game_fantasy_html_tall(event, summary, live, max_rows))



# ------------------------------------------------ images: download them ourselves and embed them (no broken pictures)

_IMG = {}
_IMG_EXT = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "svg": "image/svg+xml", "webp": "image/webp",
            "gif": "image/gif"}
SILHOUETTE = ("data:image/svg+xml;base64," + __import__("base64").b64encode(
    b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><rect width='100' height='100' fill='#c4ccd6'/>"
    b"<circle cx='50' cy='38' r='19' fill='#8e9aab'/><path d='M14 100c2-22 17-34 36-34s34 12 36 34z' fill='#8e9aab'/></svg>"
).decode())


def _logo_fallback(url):
    """Team initials in a circle, e.g. .../nba/500/gs.png -> GS."""
    m = re.search(r"/([a-z0-9]{2,5})\.(?:png|jpg|svg)", url.lower())
    code = (m.group(1) if m else "?").upper()[:4]
    svg = (f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='48' fill='#2a3350'/>"
           f"<text x='50' y='50' dy='.35em' text-anchor='middle' font-family='Arial,Helvetica,sans-serif' font-weight='900' "
           f"font-size='{34 if len(code) <= 3 else 26}' fill='#ffffff'>{code}</text></svg>")
    return "data:image/svg+xml;base64," + __import__("base64").b64encode(svg.encode()).decode()


def _fetch_img(url):
    if url in _IMG:
        return _IMG[url]
    import base64
    data, ctype = None, None
    for i in range(3):
        try:
            import html as _h
            req = urllib.request.Request(_h.unescape(url), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                body = r.read()
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if body and len(body) > 100 and (ctype.startswith("image/") or url.lower().endswith(tuple(_IMG_EXT))):
                data = body
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.6 * (i + 1))
    if data:
        if not ctype.startswith("image/"):
            ctype = _IMG_EXT.get(url.lower().rsplit(".", 1)[-1].split("?")[0], "image/png")
        uri = f"data:{ctype};base64," + base64.b64encode(data).decode()
    else:
        headshot = any(k in url for k in ("headshot", "/players/", "thumb", "avatars"))
        uri = SILHOUETTE if headshot else _logo_fallback(url)
        _log_plain(f"image unavailable, using a placeholder: {url[:90]}")
    _IMG[url] = uri
    return uri


def inline_images(html_text):
    """Swap every web image in a card for an embedded copy (or a placeholder), so the screenshot never shows a broken one."""
    urls = sorted(set(re.findall(r"""src=['"](https?://[^'"]+)['"]""", html_text)))
    todo = [u for u in urls if u not in _IMG]
    if todo:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_fetch_img, todo))
    for u in urls:
        html_text = html_text.replace(f"src='{u}'", f"src='{_IMG[u]}'").replace(f'src="{u}"', f'src="{_IMG[u]}"')
    # an <img> with no picture at all (empty src) gets the silhouette too
    return html_text.replace("src=''", f"src='{SILHOUETTE}'")


_render_card_web = render_card
_render_png_web = render_png


def render_card(html_text, width=None):
    try:
        html_text = inline_images(html_text)
    except Exception as e:  # noqa: BLE001
        _log_plain(f"image embedding skipped: {e}")
    return _render_card_web(html_text, width)


def render_png(html_text, width=880, height=530):
    try:
        html_text = inline_images(html_text)
    except Exception as e:  # noqa: BLE001
        _log_plain(f"image embedding skipped: {e}")
    return _render_png_web(html_text, width, height)



# ------------------------------------------------ starting / bench / free agent: loud, solid tags + a coloured row edge

FAN_CSS += """
.bd{font-size:12.5px!important;font-weight:900!important;letter-spacing:1.3px!important;padding:3px 9px!important;
 border-radius:6px!important;margin-top:4px!important}
.bd.st{background:#1faa59!important;color:#fff!important;box-shadow:0 0 12px rgba(31,170,89,.5)}
.bd.bn{background:#64748b!important;color:#fff!important}
.bd.fa{background:#f5c542!important;color:#1b1305!important;box-shadow:0 0 12px rgba(245,197,66,.5)}
.pr2:has(.bd.st){box-shadow:inset 5px 0 0 #1faa59}
.pr2:has(.bd.bn){box-shadow:inset 5px 0 0 #64748b}
.pr2:has(.bd.fa){box-shadow:inset 5px 0 0 #f5c542}
.tpr .ow5 em{font-size:11.5px!important;font-weight:900!important;letter-spacing:1.2px!important;padding:2px 8px!important;border-radius:5px!important}
.tpr .ow5 em.s{background:#1faa59!important;color:#fff!important}
.tpr .ow5 em.b{background:#64748b!important;color:#fff!important}
.tpr:has(em.s){box-shadow:inset 4px 0 0 #1faa59}
.tpr:has(em.b){box-shadow:inset 4px 0 0 #64748b}
"""



_nba_batch_html_narrow = nba_batch_html


def nba_batch_html(mode, events, summaries, live, title):
    """Grouped NBA card, landscape: wider so owner tags and stat lines never get cut off."""
    return _nba_batch_html_narrow(mode, events, summaries, live, title).replace(
        "</style>", "body,.card{width:1300px!important}"
        ".tpr{grid-template-columns:28px 250px 64px 128px 300px 1fr!important}"
        ".tpr .ow5{display:flex;align-items:center;gap:8px;min-width:0}.tpr .ow5 em{flex:none}"
        ".tpr .ln5{font-size:14px!important}</style>", 1)


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
        if time.time() - last_nba >= (20 if _NBA_LIVE["on"] else NBA_EVERY):
            try:
                run_nba(st)
            except Exception as ex:  # noqa: BLE001
                log(f"NBA error: {ex}")
            last_nba = time.time()
        active = active or _NBA_LIVE["on"]
        save_state(st)
        now_saved = json.dumps(st, sort_keys=True)
        if keepalive and now_saved != saved and time.time() - last_push >= 120:
            push_state()                            # so a crash mid-run can't cause repeat posts
            saved, last_push = now_saved, time.time()
        elapsed = time.time() - start
        if elapsed > MAX_RUNTIME or (not active and not keepalive):
            break
        wait = min(POLL_SECONDS, int(CONFIG.get("live_poll_seconds", 15))) if active else idle
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
