#!/usr/bin/env python3
"""
NFL + NBA fantasy Discord bot ("Donald 🏈" / "Donald 🏀") for the Rickth21 Sleeper leagues.

Data:    ESPN's free public feeds (scores, box scores, win chance) + the Sleeper API (rosters, matchups,
         projections, live stats, trades) + FantasyCalc (dynasty trade values).
Posting: Discord webhooks (NFL_WEBHOOK_URL, NBA_WEBHOOK_URL; health alerts to ALERT_WEBHOOK_URL).
Cards:   HTML rendered to PNG with headless Chrome; landscape so they read without opening.

What it posts
  NFL  prime-time games: touchdowns, late field goals, game-changing turnovers, overtime, half-time, full-time.
       Sunday 18:00-24:00 UK: one preview, then an update every 30 minutes.
       Daily playoff race (14:00 UK), weekly recap (results + awards + lineup report, then the title race).
  NBA  full-time per game (grouped only when another game is in its final minute), half-time for grouped
       tip-offs, weekly recap.
  Both leagues: every trade, with grades and a verdict (NFL trades ping @NFLfantasy).
  Weekly matchups board (one per league per week, pinned, edited every 5 min while games are live):
       NFL 00:00 UK on the day of the first game (normally Friday) -> Tuesday 12:00; NBA Monday 12:00 -> Monday 12:00.

How this file is organised
  It grew in layers. A later section may wrap an earlier function: it saves the old one
  (`_old = f`) and defines `f` again, calling `_old(...)` and adding to it. The LAST definition of a
  name is the live one. Sections, top to bottom:
    HTTP, state, helpers ........................ fetching, state.json, small utilities
    NFL / Sleeper league rosters / touchdowns .... base NFL data, owners, touchdown parsing
    NBA ........................................... NBA box scores and full-time card
    NFL image cards / slates / weekly recap ....... card CSS, Sunday slate cards, league history + odds
    NBA fantasy-first posts ....................... NBA single-game and grouped cards, NBA dispatcher
    NFL fantasy-first posts + live outlook ........ win/playoff odds simulation, live league context
    v2 / live wiring / prime-time / Sunday ........ self-measuring cards, the NFL dispatcher (run_nfl)
    v4 / daily playoff race ....................... win meters, 14:00 race card
    kickers, DEF, spoiler delay ................... extra player types, 1-minute delay
    v5 touchdown card ... scoreline first ......... current in-game cards (TD, FG, game-changer, OT)
    poster names, NBA race panel, alerts .......... "Donald 🏀" name, error alerts, watchdog
    accuracy check, Sleeper DEF stats ............. weekly check against Sleeper
    projections, pins, trades, owner lookup ....... pre-game projections, trade posts, roster matching
    weekly recap (landscape), landscape cards ..... two-part recap, wide game cards
    images, starting/bench tags, main ............. embedded images, tag styles, main loop

Usage:
  python3 bot.py                          normal run (loops while games are live)
  python3 bot.py --test                   send a test message to each webhook
  python3 bot.py --preview-nfl 20261123   print what it would post for that date (never posts)
  python3 bot.py --preview-nba 20261124
  python3 tools/check.py                  run before every upload
  python3 tools/golden.py ...             before/after check for clean-ups (see the file)
  python3 tools/sim_board.py cards DIR    weekly matchups board mock-ups (sim_board.py lifecycle: pin/unpin dry run)
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
    kind = (next(iter(files)) if files else (label or "post")).rsplit(".", 1)[0]
    main_chan = hook in (NFL_HOOK, NBA_HOOK) and hook != globals().get("ALERT_HOOK")
    if main_chan and CONFIG.get("paused"):
        _HELD[kind] = _HELD.get(kind, 0) + 1                    # held: the caller simply tries again later
        return False
    tm = CONFIG.get("test_mode") or {}
    if main_chan and tm.get("enabled") and kind in (tm.get("kinds") or []) and globals().get("ALERT_HOOK"):
        hook = ALERT_HOOK                                        # try a feature in #rick-only before everyone sees it
        content = "🧪 **TEST MODE** · this would have gone to the main channel" + (f"\n{content}" if content else "")
    who = CONFIG.get("bot") or {}
    payload = {"username": _POSTER.get("name") or who.get("name", "Donald Claude"), "embeds": embeds[:10]}
    sh = CONFIG.get("silent_hours_uk") or [0, 8]
    if main_chan and hook != globals().get("ALERT_HOOK") and sh and sh[0] <= datetime.now(UK).hour < sh[1]:
        payload["flags"] = 4096                                  # overnight: in the channel, but no phone notification
    if who.get("avatar_url"):
        payload["avatar_url"] = who["avatar_url"]
    if content:
        payload["content"] = content
        roles = re.findall(r"<@&(\d+)>", content)
        payload["allowed_mentions"] = {"parse": [], "roles": roles}
    variants = []
    if files and not embeds and _v2_on():
        variants.append((_v2_payload(payload, files), True))          # newer layout: shown bigger in the channel
    variants.append((payload, False))
    timed = hook != globals().get("ALERT_HOOK")          # health alerts never wait for a game's send slot
    if timed:
        _wait_for_send_slot()
    for pl, v2 in variants:
        body, ctype = _post_body(pl, files)
        url = hook + ("&" if "?" in hook else "?") + "wait=true" + ("&with_components=true" if v2 else "")
        for _ in range(5):
            _t_up = time.time()
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": ctype, "User-Agent": "DiscordBot (sports-bot, 1.0)"})
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    try:
                        m = json.loads(resp.read().decode() or "{}")
                        _LAST_MSG.clear()
                        _LAST_MSG.update({"id": m.get("id"), "channel_id": m.get("channel_id"), "v2": v2,
                                          "att": [(a.get("id"), a.get("filename")) for a in m.get("attachments") or []]})
                    except Exception:  # noqa: BLE001
                        pass
                    _POSTLOG.append({"t": round(time.time()), "kind": kind,
                                     "where": "nfl" if hook == NFL_HOOK else ("nba" if hook == NBA_HOOK else "alert")})
                    del _POSTLOG[:-600]
                    if timed:
                        _UPLOAD_SECS.append(time.time() - _t_up)
                        del _UPLOAD_SECS[:-10]
                        _record_send(True)
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
                msg = e.read().decode()[:300]
                if v2 and e.code == 400:                  # newer layout refused: send it the classic way
                    _log_plain(f"Discord refused the new layout ({msg[:120]}): posting the classic way")
                    break
                log(f"Discord rejected post ({e.code}): {msg}")
                return 400 <= e.code < 500   # bad payload: don't retry forever
            except Exception as e:  # noqa: BLE001
                log(f"Discord post failed: {e}")
                return False
        else:
            return False
    return False


V2_FLAG = 1 << 15                     # IS_COMPONENTS_V2: Discord's newer message layout (bigger images)


def _v2_on():
    return CONFIG.get("discord_layout", "v2") == "v2"


def _v2_components(name, text=None):
    comps = [{"type": 10, "content": str(text)[:3900]}] if text else []
    return comps + [{"type": 12, "items": [{"media": {"url": f"attachment://{name}"}}]}]


def _v2_payload(payload, files):
    """The same post in the components-v2 layout: optional text, then the image in a media gallery."""
    out = {k: v for k, v in payload.items() if k not in ("content", "embeds")}
    out["flags"] = (payload.get("flags") or 0) | V2_FLAG
    out["components"] = _v2_components(next(iter(files)), payload.get("content"))
    out["attachments"] = [{"id": i, "filename": n} for i, n in enumerate(files)]
    out.setdefault("allowed_mentions", {"parse": []})
    return out


def _post_body(payload, files):
    if not files:
        return json.dumps(payload).encode(), "application/json"
    boundary = "----sportsbot" + str(int(time.time() * 1000))
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
             f"Content-Type: application/json\r\n\r\n".encode() + json.dumps(payload).encode() + b"\r\n"]
    for i, (name, data) in enumerate(files.items()):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[{i}]\"; filename=\"{name}\"\r\n"
                     f"Content-Type: {'image/gif' if name.endswith('.gif') else 'image/png'}\r\n\r\n".encode() + data + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


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


def ot_tag(event):
    detail = event["status"]["type"].get("shortDetail", "") or ""
    m = re.search(r"(\d*OT)", detail)
    return f" ({m.group(1)})" if m else ""


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
                out.append({"name": a["athlete"]["displayName"], "team": abbr, "fp": fp, "eid": str(a["athlete"].get("id") or ""),
                            "raw": {"pts": int(pts), "reb": int(reb), "ast": int(ast), "stl": int(stl),
                                    "blk": int(blk), "tpm": int(tpm)},
                            "line": " · ".join(parts) + tag + f" · **{fmt_pts(fp)} fpts**",
                            "elite": elite})
    out.sort(key=lambda x: -x["fp"])
    return out


NBA_LOGO = "https://a.espncdn.com/i/teamlogos/leagues/500/nba.png"


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


def headshot(summary, name):
    for team in summary.get("boxscore", {}).get("players", []):
        for block in team.get("statistics", []):
            for a in block.get("athletes", []):
                if a["athlete"]["displayName"] == name:
                    return (a["athlete"].get("headshot") or {}).get("href", "")
    return ""


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


def pid_attr(pid):
    """data-pid='<sleeper id>' for a player photo, so the rank badge never has to guess who it is."""
    try:
        return f" data-pid='{pid}'" if pid and re.fullmatch(r"\w+", str(pid)) else ""
    except Exception:  # noqa: BLE001
        return ""


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
            rec[a]["pa"] = rec[a].get("pa", 0.0) + pb                       # points against
            rec[b]["pa"] = rec[b].get("pa", 0.0) + pa
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


# ------------------------------------------------ NBA fantasy-first posts

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


def nba_moment(g, e, kind):
    """When the game (kind 'ft') or the first half ('ht') really ended: ESPN's play-by-play time, else first seen."""
    key = f"{kind}_at"
    if not g.get(key):
        seen = g.setdefault(f"{kind}_seen", time.time())
        t0 = seen
        try:
            plays = fetch(f"basketball/nba/summary?event={e['id']}").get("plays") or []
            if kind == "ht":
                plays = [p for p in plays if (p.get("period") or {}).get("number") == 2]
            wcs = [_wallclock_ts(p["wallclock"]) for p in plays if p.get("wallclock")]
            if wcs:
                t0 = min(seen, max(wcs))
        except Exception:  # noqa: BLE001
            pass
        g[key] = t0
    return g[key]


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
        if not force and not due(nba_moment(G(e), e, "ft"), "NBA full-time"):
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
            if due(nba_moment(g, e, "ht"), "NBA half-time"):
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
        prs.append(f"<div class='pr2{' lead' if i == 0 else ''}'><img src='{H.escape(nfl_headshot(r.get('id')))}'{pid_attr(_sl_pid(r['name'], r.get('team', '')))}>"
                   f"<div><div class='nm2'>{H.escape(r['name'])}<span>{r['team']} · {r.get('pos', '')}</span></div>"
                   f"<div class='ln'>{H.escape(r['line'])}</div></div>"
                   f"{pj_box(r['pts'], pregame_proj('nfl', r, live), r['elite'], game_frac(event, 'nfl'))}"
                   f"<div><div class='ow'>{H.escape(r['owner'])}</div>"
                   f"<span class='bd {'st' if r['starting'] else 'bn'}'>{'STARTING' if r['starting'] else 'BENCH'}</span></div>{mt}</div>")
    if fa and fa["pts"] >= 12:
        prs.append(f"<div class='pr2'><img src='{H.escape(nfl_headshot(fa.get('id')))}'{pid_attr(_sl_pid(fa['name'], fa.get('team', '')))}>"
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
            f"<script>addEventListener('load',()=>(document.fonts&&document.fonts.ready?document.fonts.ready:Promise.resolve())"
            f".then(()=>setTimeout(()=>document.body.setAttribute('data-h',"
            f"Math.ceil(document.querySelector('.card').getBoundingClientRect().height)),60)))</script></body></html>")


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
.rr2{display:grid;grid-template-columns:30px minmax(0,1fr) 172px 56px 60px 60px 236px 172px;align-items:center;gap:12px;height:40px;
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
.rr2 .pa{color:#aab3c5}
.rr2 .chip{font-size:11.5px;font-weight:900;letter-spacing:1px;border-radius:6px;padding:4px 8px;text-align:center;white-space:nowrap;justify-self:start}
.rr2 .chip.bye{background:linear-gradient(90deg,#ffd166,#ffb347);color:#1b1305;box-shadow:0 0 12px rgba(255,190,70,.5)}
.rr2 .chip.in{background:#1faa59;color:#fff;box-shadow:0 0 12px rgba(31,170,89,.5)}
.rr2 .chip.out{background:#3a1218;color:#ff9a9e;border:1px solid rgba(255,154,158,.6)}
.rr2 .chip.alive{background:rgba(255,255,255,.08);color:#cfd5e1}
.rr2 .chip.mir{background:linear-gradient(90deg,#6d28d9,#a855f7);color:#fff}
.rr2.clin{background:linear-gradient(90deg,rgba(31,170,89,.14),rgba(31,170,89,0) 60%)}
.rr2.elim{opacity:.55}
.rr2{border-radius:8px;padding:0 6px}
"""


def playoff_race_html(ctx, prev, title_date):
    import html as H
    live, ol, base, week = ctx["live"], ctx["ol"], ctx["base"], ctx["week"]
    league = CONFIG["nfl"].get("league_name", "League")
    settings = league_settings("nfl").get("settings", {})
    po_start, n_po = int(settings.get("playoff_week_start") or 15), int(settings.get("playoff_teams") or 6)
    _, rec = _history("nfl", week - 1)
    try:                                   # mathematically clinched / eliminated, from the finished weeks only
        status = playoff_status(league_settings("nfl"), rec, week - 1) if rec else {}
    except Exception as e:  # noqa: BLE001
        _log_plain(f"race status skipped: {e}")
        status = {}
    def _standing(r):                     # real league standings: record, then points for (Sleeper's tiebreak)
        x = rec.get(r) or {"w": 0, "l": 0, "t": 0, "pf": 0}
        return (-(x["w"] + 0.5 * x["t"]), -x["pf"])
    order = sorted(ol, key=_standing)
    rows = ["<div class='rr2 h'><span>#</span><span>MANAGER</span><span>STATUS</span><span>W-L</span><span>PF</span><span>PA</span>"
            "<span>PLAYOFFS · SINCE YESTERDAY</span><span>TITLE CHANCE</span></div>"]

    def chip(r, po):
        s_ = (status.get(r) or {})
        k = s_.get("status")
        if k == "bye":
            return "<span class='chip bye'>🔒 BYE CLINCHED</span>"
        if k == "in":
            return "<span class='chip in'>🔒 CLINCHED</span>"
        if k == "out":
            return "<span class='chip out'>❌ ELIMINATED</span>"
        if k == "alive" and po < 0.5:
            return "<span class='chip mir'>NEEDS A MIRACLE</span>"
        if k == "alive" and s_.get("magic"):
            m = s_["magic"]
            return f"<span class='chip alive'>{m} WIN{'S' if m != 1 else ''} TO CLINCH</span>"
        return "<span class='chip alive'>IN THE HUNT</span>" if k else "<span></span>"
    for i, r in enumerate(order, 1):
        x = rec.get(r, {"w": 0, "l": 0, "t": 0, "pf": 0})
        po, ti = ol[r]["po"], ol[r]["title"]
        d = po - prev.get(str(r), po)
        mv = (f"<span class='mv u'>▲{d:.0f}</span>" if d >= 1 else f"<span class='mv dn'>▼{abs(d):.0f}</span>" if d <= -1
              else "<span class='mv z'>–</span>")
        kk = (status.get(r) or {}).get("status")
        rc = " clin" if kk in ("in", "bye") else (" elim" if kk == "out" else "")
        rows.append(f"<div class='rr2{' cut' if i == n_po else ''}{rc}'><span class='k'>{i}</span>"
                    f"<span class='m'>{H.escape(live['rosters'][r]['manager'])}</span>{chip(r, po)}"
                    f"<span>{x['w']}-{x['l']}{'-' + str(x['t']) if x['t'] else ''}</span><span>{x['pf']:.0f}</span><span class='pa'>{x.get('pa', 0):.0f}</span>"
                    f"<span class='o'><span>{po:.0f}%</span><div class='rb'><div style='width:{max(po, 1.5):.1f}%;"
                    f"background:linear-gradient(90deg,#d97757,#f2a77f)'></div></div>{mv}</span>"
                    f"<span class='o2'><span>{ti:.0f}%</span><div class='rb'><div style='width:{min(100, max(ti * 2.5, 1.5)):.1f}%;"
                    f"background:linear-gradient(90deg,#e8b13c,#ffe08a)'></div></div></span></div>")
    left = max(0, po_start - week)
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>{H.escape(league.upper())} · PLAYOFF RACE · "
            f"{H.escape(title_date.upper())}</div><div class='pill'>WEEK {week} IN PROGRESS</div></div>"
            f"<div class='rt'>{''.join(rows)}</div>"
            f"<div class='race-note'>Current standings (record, then points for) · top {n_po} make the playoffs (dashed line) · playoffs start week {po_start} · "
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
    _PROJ[week] = _nfl_proj_fetch(week)
    return _PROJ[week]


def _nfl_proj_fetch(week):
    """One fresh download of the week's Sleeper projections (no cache)."""
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


# Exact timing: every in-game post should land spoiler_delay() seconds after the real moment (ESPN's
# timestamp of the play). The card is built PREP seconds early, then discord() waits for the exact second.
PREP_SECONDS = 30                    # animated cards take ~15-25s to build
_SEND_AT = {"t": None, "t0": None, "label": ""}      # target send time for the post being built right now
_NEXT_WAKE = {"t": None}                              # earliest upcoming "start building" time (main loop wakes for it)
_TIMING = []                                          # (label, moment, sent) for the last posts, saved to state
_POSTLOG = []                                         # every successful post (time, kind, channel) for the daily report
_HELD = {}                                            # posts held back while paused
_ANIM_CHECK = {}


def due(t0, label=""):
    """True when it's time to start building the post for a moment at t0; remembers when it must be sent."""
    target = t0 + spoiler_delay()
    if time.time() >= target - PREP_SECONDS:
        _SEND_AT.update(t=target, t0=t0, label=label)
        return True
    w = target - PREP_SECONDS
    if _NEXT_WAKE["t"] is None or w < _NEXT_WAKE["t"]:
        _NEXT_WAKE["t"] = w
    return False


def _wait_for_send_slot():
    """Called just before a webhook post: sleep until the exact target second (minus typical upload time)."""
    t = _SEND_AT.get("t")
    if not t:
        return
    lead = sum(_UPLOAD_SECS[-5:]) / len(_UPLOAD_SECS[-5:]) if _UPLOAD_SECS else 0.8
    wait = t - lead - time.time()
    if 0 < wait < PREP_SECONDS + 30:
        time.sleep(wait)


def _record_send(ok):
    if _SEND_AT.get("t0") and ok:
        _TIMING.append({"what": _SEND_AT.get("label") or "post", "moment": round(_SEND_AT["t0"], 1),
                        "sent": round(time.time(), 1), "after": round(time.time() - _SEND_AT["t0"], 1)})
        del _TIMING[:-40]
        _log_plain(f"posted {_SEND_AT.get('label') or 'post'} {time.time() - _SEND_AT['t0']:.1f}s after the moment "
                   f"(target {spoiler_delay():.0f}s)")
    _SEND_AT.update(t=None, t0=None, label="")


_UPLOAD_SECS = []


def _wallclock_ts(wc):
    return datetime.strptime(wc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def td_ready(summary, play, first_seen):
    """True when it's time to build the post for this play (it then lands spoiler_delay() after ESPN's time of the play)."""
    t0 = first_seen
    try:
        _, d = play_detail(summary, play.get("id"))
        if (d or {}).get("wallclock"):
            t0 = _wallclock_ts(d["wallclock"])
    except Exception:  # noqa: BLE001
        pass
    return due(t0, (play.get("type") or {}).get("text") or "play")


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
    return due(t0, key.replace("_seen", ""))


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
        return o, (f"<div class='pp5{hot}'><img src='{H.escape(hs)}'{pid_attr(_sl_pid(name, abbr))}><div><div class='role'>{role}</div>"
                   f"<div class='nm'>{H.escape(name)}</div><div class='gn'><b>+{gain_:.1f}</b>{tot}</div>{bar}"
                   f"<div class='ow3'>{own}</div></div></div>")

    pid0 = boxscore_id(summary, td["scorer"])
    o0 = league_owner(pid0, td["scorer"])
    gain, qb_gain = play_fpts(td, (o0 or {}).get("pos", ""))
    owners = []
    if td["kind"] == "pass":
        o1, p1 = panel("RECEIVER", td["scorer"], gain)
        o2, p2 = panel("QUARTERBACK", td["qb"], qb_gain)
        panels, cls = p1 + p2, "pair"
        owners = [o1, o2]
    else:
        o1, p1 = panel("SCORER", td["scorer"], gain)
        panels, cls = p1, "pair one"
        owners = [o1]

    def impact_row(o):
        rid = next((r for r, x in live["rosters"].items() if x["manager"] == o["manager"]), None)
        opp = next((b if a == rid else a for a, b in live["pairs"] if rid in (a, b)), None) if rid else None
        if not opp:
            return ""
        me = ol[rid]
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
    panel = (f"<div class='pair one'><div class='pp5{hot}'><img src='{H.escape(nfl_headshot(kid) or kt['team'].get('logo', ''))}'{pid_attr(_sl_pid(kicker, abbr))}><div>"
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
.sw5 .bar{position:relative;height:30px;border-radius:9px;overflow:hidden;background:rgba(255,255,255,.08);display:flex}
.sw5 .bar div{height:30px}
.sw5 .row{grid-template-columns:90px 1fr!important}
.sw5 .sg{display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:900;color:#fff;white-space:nowrap;
 overflow:hidden;text-shadow:0 1px 2px rgba(0,0,0,.55)}
.sw5 .sg.o{background:#c9ced8;color:#101522;text-shadow:none}
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


    ca = vivid(gt["team"].get("color") or "1faa59")
    co = vivid(lt["team"].get("color") or "5b7bd5")

    ga_, la_ = gt["team"]["abbreviation"], lt["team"]["abbreviation"]
    import colorsys as _cs

    def _pop(h):                                         # how much a colour stands out on the dark card
        r, g_, b_ = (x / 255 for x in hex_rgb(h))
        hh, ll, ss = _cs.rgb_to_hls(r, g_, b_)
        return ss * min(ll, 0.6) + (0.25 if not (0.5 < hh < 0.75) else 0)      # blues stand out least on navy
    cands = [vivid(c) for c in (gt["team"].get("color"), gt["team"].get("alternateColor")) if c]
    cbar = max(cands, key=_pop) if cands else ca

    def lab(ab, p):
        return f"{ab} {p:.0f}%" if p >= 30 else (f"{p:.0f}%" if p >= 20 else "")

    def bar(p):                                         # gaining team in its boldest colour, the other side light grey
        return (f"<div class='bar'><div class='sg' style='width:{p:.1f}%;background:{cbar}'>{lab(ga_, p)}</div>"
                f"<div class='sg o' style='width:{100 - p:.1f}%'>{lab(la_, 100 - p)}</div></div>")
    sw = (f"<div class='sw5'><div class='ih'>WIN CHANCE · ESPN</div>"
          f"<div class='big'><span>{gt['team']['abbreviation']}</span><span class='from'>{g_before:.0f}%</span>"
          f"<span class='arr'>→</span><span>{g_after:.0f}%</span><span class='d u'>▲{abs(swing):.0f}</span></div>"
          f"<div class='row'><span class='lb'>BEFORE</span>{bar(g_before)}</div>"
          f"<div class='row'><span class='lb'>AFTER</span>{bar(g_after)}</div></div>")

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


def _animations_ready():
    try:
        import PIL  # noqa: F401
        return bool(find_chrome())
    except Exception:  # noqa: BLE001
        return False


def save_state(st):
    if "animations_ready" not in _ANIM_CHECK:
        _ANIM_CHECK["animations_ready"] = _animations_ready()
    st["animations_ready"] = _ANIM_CHECK["animations_ready"]      # visible in state.json: are animated cards possible?
    if _TIMING:
        st["post_timing"] = list(_TIMING)            # how many seconds after the real moment each post landed
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


# ------------------------------------------------ trades: every Sleeper trade posted with a verdict

_FC = {}
_NBA_ROWS = {}

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
    _NBA_ROWS[week] = _nba_rows_fetch(week)
    return _NBA_ROWS[week]


def _nba_rows_fetch(week):
    """One fresh download of Sleeper's NBA projections for the week (no cache)."""
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
    return out


def best_lineup(slots, pids, pts, sport, picks=None):
    """Best possible starting lineup points: fill the strictest slots first with the best eligible player.
    picks: optional list that receives the chosen players."""
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
                if picks is not None:
                    picks.append(p)
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
        # lock-in league: a starter's week counts ONE game, so a player's week is worth his lock-in value
        # (optimal keep-or-play-on over that week's games), not the sum of his games
        weeks = list(range(week, last + 1))
        wk_val = {}
        games = {}
        for w in weeks:
            per = {}
            for pid, d, v in sorted(nba_rows(w), key=lambda r: r[1]):
                if v > 0:
                    per.setdefault(pid, []).append((v, None))
                    games[pid] = games.get(pid, 0) + 1
            wk_val[w] = {pid: nba_lockin_value(None, g)[0] for pid, g in per.items()}

        def ros(p):
            return sum(wk_val[w].get(p, 0) for w in weeks)
        n_units = max(len(weeks), 1)

    def lineup_total(roster):
        if sport == "nfl":
            return sum(best_lineup(slots, roster, wk_pts[w], "nfl") for w in weeks) / n_units
        return sum(best_lineup(slots, [p for p in roster if p in wk_val[w]], wk_val[w], "nba") for w in weeks) / n_units

    fc = fantasycalc_values() if sport == "nfl" else {"players": {}}

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
            per = r_ / n_units                             # per week (NBA: lock-in value of his week)
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
    best, vw, nw = ev["best"], ev["vw"], ev["nw"]
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
    unit = "/wk"

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
    gif = render_anim(html_text) if wants_animation(html_text) else None
    png = None if gif else render_card(html_text)
    if sport == "nba":
        _POSTER["name"] = (CONFIG.get("nba") or {}).get("bot_name") or "Donald 🏀"
    try:
        if gif:
            return discord(hook, [], "trade", content=content, files={"trade.gif": gif})
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



# ------------------------------------------------ season highs: 40+ fantasy points AND the player's best game of the season

SEASON_HIGH_MIN = None          # set a number here only to override both sports (e.g. for a mock-up)


def season_high_min(sport):
    """Minimum fantasy points for the season-high effect: NFL 20+, NBA 40+ (config <sport>.season_high_min)."""
    if SEASON_HIGH_MIN is not None:
        return float(SEASON_HIGH_MIN)
    return float((CONFIG.get(sport) or {}).get("season_high_min", 20 if sport == "nfl" else 40))
_SH_WEEKS = {}
_SH_NBA = {}


def nfl_week_points(week):
    """Every NFL player's fantasy points for one week, in the league's scoring (from Sleeper's stats)."""
    if week in _SH_WEEKS:
        return _SH_WEEKS[week]
    out = {}
    try:
        season = fetch_url(f"{SLEEPER}/state/nfl").get("season")
        sc = dict(league_scoring("nfl"))
        for pid, st in (fetch_url(f"{SLEEPER}/stats/nfl/regular/{season}/{week}") or {}).items():
            if isinstance(st, dict) and not pid.isalpha():
                out[pid] = sum(float(v or 0) * float(sc[k]) for k, v in st.items()
                               if k in sc and isinstance(v, (int, float)))
    except Exception as e:  # noqa: BLE001
        _log_plain(f"season-high data unavailable for week {week}: {e}")
    _SH_WEEKS[week] = out
    return out


def _nfl_sleeper_pid(row):
    ix = _player_index("nfl")
    rid = str(row.get("id") or "")
    pid = ix["espn"].get(rid) if rid else None
    if not pid:
        team = sl_team(row.get("team") or "")
        pid = ix["name_team"].get((name_key(row.get("name", "")), team))
    if not pid:
        c = ix["name"].get(name_key(row.get("name", "")), [])
        pid = c[0] if len(c) == 1 else None
    return pid


def prev_season_best(sport, row, event=None):
    """The player's best fantasy game this season BEFORE this one, or None if unknown."""
    try:
        if sport == "nfl":
            pid = _nfl_sleeper_pid(row)
            if not pid:
                return None
            week = int(fetch_url(f"{SLEEPER}/state/nfl").get("week") or 1)
            vals = [nfl_week_points(w).get(pid) for w in range(1, week)]
            vals = [v for v in vals if v is not None]
            return max(vals) if vals else 0.0
        eid = str(row.get("eid") or "")
        if not eid:
            return None
        if eid not in _SH_NBA:
            g = fetch_url(f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{eid}/gamelog")
            names = g.get("names") or g.get("labels") or []
            games = {}
            st = next((x for x in g.get("seasonTypes") or [] if "Regular" in (x.get("displayName") or "")), None)
            for cat in (st or {}).get("categories") or []:
                for ev in cat.get("events") or []:
                    games[ev["eventId"]] = dict(zip(names, ev.get("stats") or []))
            _SH_NBA[eid] = games
        sc = league_scoring("nba")
        v = lambda k: float(sc.get(k) or 0)  # noqa: E731
        best = 0.0
        for gid, s_ in _SH_NBA[eid].items():
            if event is not None and str(gid) == str(event.get("id")):
                continue
            n = lambda k: num(str(s_.get(k, "0")).split("-")[0])  # noqa: E731
            pts, reb, ast, stl, blk, tov, tpm = (n("points") or n("PTS"), n("rebounds") or n("REB"), n("assists") or n("AST"),
                                                 n("steals") or n("STL"), n("blocks") or n("BLK"),
                                                 n("turnovers") or n("TO"), n("threePointFieldGoalsMade-threePointFieldGoalsAttempted") or n("3PT"))
            doubles = sum(1 for x in (pts, reb, ast, stl, blk) if x >= 10)
            fp = pts * v("pts") + reb * v("reb") + ast * v("ast") + stl * v("stl") + blk * v("blk") + tov * v("to") + tpm * v("tpm")
            fp += v("dd") if doubles >= 2 else 0
            fp += v("td") if doubles >= 3 else 0
            fp += v("qd") if doubles >= 4 else 0
            fp += (v("bonus_pt_40p") if pts >= 40 else 0) + (v("bonus_pt_50p") if pts >= 50 else 0)
            fp += (v("bonus_reb_20p") if reb >= 20 else 0) + (v("bonus_ast_15p") if ast >= 15 else 0)
            best = max(best, fp)
        return best
    except Exception as e:  # noqa: BLE001
        _log_plain(f"season best unavailable for {row.get('name')}: {e}")
        return None


def season_highs(sport, rows, event=None, key="fp"):
    """{player name: previous best} for every player in `rows` who just set a 40+ season high."""
    out = {}
    for r in rows or []:
        pts = float(r.get(key) or 0)
        if pts < season_high_min(sport) or str(r.get("id", "")).startswith("def:"):
            continue
        prev = prev_season_best(sport, r, event)
        if prev is not None and pts > prev:
            out[r["name"]] = prev
    return out


SH_CSS = """
.pp5.sh{position:relative;overflow:visible!important;isolation:isolate;margin-top:10px;padding-top:20px!important;
 background:linear-gradient(100deg,rgba(255,94,0,.42),rgba(255,184,40,.26) 42%,rgba(255,61,0,.36))!important;
 box-shadow:0 0 0 2px #ffb000,0 0 26px rgba(255,140,0,.75)!important}
.pp5 .shr{left:96px}
.pr2.sh,.tpr.sh{position:relative;overflow:visible!important;
 background:linear-gradient(100deg,rgba(255,94,0,.42),rgba(255,184,40,.26) 42%,rgba(255,61,0,.36))!important;
 box-shadow:0 0 0 2px #ffb000,0 0 26px rgba(255,140,0,.75),inset 6px 0 0 #ff5e00!important}
.pr2.sh,.tpr.sh{isolation:isolate}
.pr2.sh::before,.tpr.sh::before{content:'';position:absolute;inset:0;border-radius:inherit;pointer-events:none;opacity:.9;z-index:-1;
 background-image:radial-gradient(circle,#ffe066 1.6px,transparent 2.2px),radial-gradient(circle,#ff6b6b 1.6px,transparent 2.2px),
 radial-gradient(circle,#7ee2a8 1.4px,transparent 2px),radial-gradient(circle,#8ab4ff 1.4px,transparent 2px),
 radial-gradient(circle,#ffffff 1.2px,transparent 1.8px);
 background-size:37px 29px,53px 41px,61px 23px,43px 47px,29px 31px;background-position:0 0,11px 7px,23px 13px,5px 19px,17px 3px;
 -webkit-mask-image:linear-gradient(180deg,#000 0,#000 16%,transparent 30%,transparent 70%,#000 84%,#000 100%)}
.pr2.sh .fp{background:linear-gradient(160deg,#fff1a8,#ffb000 45%,#ff5e00)!important;color:#1b0a00!important;
 box-shadow:0 0 22px rgba(255,140,0,.95)!important}
.pr2.sh .fp i{color:#1b0a00!important}
.tpr.sh .fp5{background:linear-gradient(90deg,#fff1a8,#ffb000,#ff5e00)!important;box-shadow:0 0 16px rgba(255,140,0,.9)}
.shr{position:absolute;top:-13px;left:64px;z-index:5;white-space:nowrap;font-size:13px;font-weight:900;letter-spacing:1.3px;
 color:#1b0a00;padding:3px 12px;border-radius:999px;background:linear-gradient(90deg,#ff3d00,#ff9100,#ffd600);
 box-shadow:0 0 16px rgba(255,110,0,.9),0 0 0 2px rgba(255,255,255,.35)}
.shr em{font-style:normal;font-weight:800;opacity:.85;margin-left:6px}
.tpr .shr{left:44px;top:-12px;font-size:11.5px}
.pr2.sh,.tpr.sh{margin-top:8px}
.card.shc .in{z-index:2}
.card.shc::after{content:'';position:absolute;left:0;right:0;top:0;height:260px;pointer-events:none;opacity:.45;z-index:1;
 background-image:radial-gradient(circle,#ffd166 2px,transparent 2.6px),radial-gradient(circle,#ff6b6b 2px,transparent 2.6px),
 radial-gradient(circle,#7ee2a8 1.8px,transparent 2.4px),radial-gradient(circle,#8ab4ff 1.8px,transparent 2.4px);
 background-size:71px 53px,97px 61px,83px 47px,59px 67px;background-position:0 0,31px 17px,13px 29px,47px 7px;
 -webkit-mask-image:linear-gradient(180deg,#000,transparent)}
"""


def _mark_season_highs(html_text, highs, name_tag, row_cls):
    """Light up each season-high row: fire/confetti styling + a SEASON HIGH ribbon with the old best."""
    import html as H
    if not highs:
        return html_text
    for name, prev in highs.items():
        i = html_text.find(f"{name_tag}{H.escape(name)}")
        if i < 0:
            continue
        j = html_text.rfind(f"<div class='{row_cls}", 0, i)
        if j < 0:
            continue
        k = html_text.find(">", j)
        opening = html_text[j:k]
        new_open = opening.replace(f"class='{row_cls}", f"class='{row_cls} sh", 1)
        ribbon = f"<span class='shr'>🔥 SEASON HIGH<em>old best {prev:.1f}</em></span>"
        html_text = html_text[:j] + new_open + ">" + ribbon + html_text[k + 1:]
    html_text = html_text.replace("<div class='card'", "<div class='card shc'", 1)
    _SH_HEAD.update(highs)
    return html_text.replace("</style>", SH_CSS + "</style>", 1)


_SH_HEAD = {}


_nfl_game_html_nosh = nfl_game_fantasy_html


def nfl_game_fantasy_html(event, summary, live, ol, base, max_rows=30):
    h = _nfl_game_html_nosh(event, summary, live, ol, base, max_rows)
    try:
        rows = [r for r in nfl_fantasy(summary)]
        h = _mark_season_highs(h, season_highs("nfl", rows, event, key="pts"), "<div class='nm2'>", "pr2")
    except Exception as e:  # noqa: BLE001
        _log_plain(f"season highs skipped: {e}")
    return h


_nba_game_html_nosh = nba_game_fantasy_html


def nba_game_fantasy_html(event, summary, live, max_rows=7):
    h = _nba_game_html_nosh(event, summary, live, max_rows)
    try:
        h = _mark_season_highs(h, season_highs("nba", nba_league_rows(summary), event), "<div class='nm2'>", "pr2")
    except Exception as e:  # noqa: BLE001
        _log_plain(f"season highs skipped: {e}")
    return h


_nba_batch_html_nosh = nba_batch_html


def nba_batch_html(mode, events, summaries, live, title):
    h = _nba_batch_html_nosh(mode, events, summaries, live, title)
    try:
        highs = {}
        for e, summ in zip(events, summaries):
            if summ:
                top3 = [r for r in nba_league_rows(summ) if r["owner"]][:3]
                highs.update(season_highs("nba", top3, e))
        h = _mark_season_highs(h, highs, "<span class='nm5'>", "tpr")
    except Exception as e:  # noqa: BLE001
        _log_plain(f"season highs skipped: {e}")
    return h


_post_card_nosh = _post_card


def _post_card(hook, html_text, content, embed, name):
    """Headline gets a 🔥 SEASON HIGH call-out when it names a player who just set one."""
    try:
        for nm in list(_SH_HEAD):
            for k in ("img_title", "title"):
                t = embed.get(k)
                if t and nm in t and "SEASON HIGH" not in t:
                    embed[k] = f"🔥 SEASON HIGH! {t}"
    finally:
        _SH_HEAD.clear()
    return _post_card_nosh(hook, html_text, content, embed, name)



_nfl_td_html_nosh = nfl_td_fantasy_html


def nfl_td_fantasy_html(event, play, summary, live, ol, base, *a, **k):
    """Touchdown card: the scorer (or passer) lights up when this score takes him to a 40+ season high."""
    h = _nfl_td_html_nosh(event, play, summary, live, ol, base, *a, **k)
    try:
        td = td_parse(play)
        rows = []
        for nm in (td.get("scorer"), td.get("qb")):
            now = player_now(summary, nm) if nm else None
            if now is not None and now >= season_high_min("nfl"):
                pid = boxscore_id(summary, nm)
                rows.append({"name": nm, "id": pid, "team": play["team"]["abbreviation"], "fp": now})
        h = _mark_season_highs(h, season_highs("nfl", rows, event), "<div class='nm'>", "pp5")
    except Exception as e:  # noqa: BLE001
        _log_plain(f"season highs skipped: {e}")
    return h



# ------------------------------------------------ animated cards for the big moments (looping GIFs)
# Touchdowns, late field goals, game-changers, overtime, season highs and trades get a 2-second looping animation.
# All frames are captured in ONE Chrome pass (a grid of frames, each paused at a different moment), then stitched
# into a GIF with Pillow. Anything goes wrong -> the normal still image is posted instead.

ANIM_FRAMES = 40                        # 20 frames a second: smooth motion
ANIM_SECONDS = 2.0
ANIM_WIDTH = 1200                       # GIF width (Discord shows ~550px wide, so this stays sharp)

ANIM_CSS = """
@keyframes a_pop{0%{transform:scale(1)}8%{transform:scale(1.16)}22%{transform:scale(.98)}32%{transform:scale(1)}100%{transform:scale(1)}}
@keyframes a_glow{0%,100%{filter:drop-shadow(0 0 0 transparent)}8%{filter:drop-shadow(0 0 34px var(--hc,#fff))}50%{filter:drop-shadow(0 0 14px var(--hc,#fff))}}
@keyframes a_bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-7px)}}
@keyframes a_flash{0%,100%{text-shadow:0 0 0 rgba(255,255,255,0)}50%{text-shadow:0 0 26px rgba(255,255,255,.95)}}
@keyframes a_pulse{0%,100%{box-shadow:0 0 0 2px #ffb000,0 0 18px rgba(255,140,0,.55)}50%{box-shadow:0 0 0 3px #ffd600,0 0 46px rgba(255,140,0,1)}}
@keyframes a_fall{from{background-position:0 0,11px 7px,23px 13px,5px 19px,17px 3px}
 to{background-position:0 29px,11px 48px,23px 36px,5px 66px,17px 34px}}
@keyframes a_fall2{from{background-position:0 0,31px 17px,13px 29px,47px 7px}to{background-position:0 106px,31px 139px,13px 123px,47px 141px}}
@keyframes a_shine{from{background-position:0 0}to{background-position:240px 0}}
@keyframes a_tape{from{background-position:0 0}to{background-position:62.2px 0}}
@keyframes a_ot{0%,100%{transform:skew(-8deg) scale(1)}50%{transform:skew(-8deg) scale(1.07)}}
@keyframes a_siren{0%,49%{background:#ff2d3a;color:#fff;box-shadow:0 0 30px rgba(255,45,58,.9)}50%,100%{background:#fff;color:#ff2d3a;box-shadow:0 0 30px rgba(255,255,255,.9)}}
@keyframes a_win{0%,100%{box-shadow:0 0 18px rgba(255,190,70,.25)}50%{box-shadow:0 0 44px rgba(255,190,70,.7)}}
.sbx .sc.hot{display:inline-block;transform-origin:50% 60%;animation:a_pop 2s ease-out infinite,a_glow 2s ease-out infinite}
.sbx .plus{animation:a_bounce 1s ease-in-out infinite}
.sbx .yd{animation:a_flash 1s ease-in-out infinite}
.pr2.sh,.tpr.sh,.pp5.sh{animation:a_pulse 1s ease-in-out infinite}
.pr2.sh::before,.tpr.sh::before{animation:a_fall 1s linear infinite}
.card.shc::after{animation:a_fall2 2s linear infinite}
.shr{background:repeating-linear-gradient(90deg,#ff3d00 0,#ff9100 60px,#ffd600 120px,#ff9100 180px,#ff3d00 240px)!important;
 background-size:240px 100%!important;animation:a_shine 1s linear infinite}
.tape{animation:a_tape 1s linear infinite}
.otw{animation:a_ot 1s ease-in-out infinite}
.vd.fleece{animation:a_siren .5s linear infinite}
.ts.win{animation:a_win 1s ease-in-out infinite}
@keyframes a_stamp{0%,100%{transform:rotate(-4deg) scale(1)}50%{transform:rotate(-4deg) scale(1.1)}}
.stp.in .stamp,.stp.bye .stamp{animation:a_stamp 1s ease-in-out infinite}
@keyframes a_champ{0%,100%{box-shadow:0 0 30px rgba(255,209,102,.8)}50%{box-shadow:0 0 60px rgba(255,209,102,1)}}
.bkch img,.bkch .bkav{animation:a_champ 1s ease-in-out infinite}
"""
ANIM_FREEZE = ("*,*::before,*::after{animation-play-state:paused!important;"
               "animation-delay:calc(var(--t,0) * -1s)!important}")


def wants_animation(html_text):
    """Only the big moments move: scoring scoreboards, overtime, season highs and trades."""
    if not (CONFIG.get("animations") or {}).get("enabled", True):
        return False
    return any(k in html_text for k in ("class='sc hot'", "class='otw'", " sh'", "class='shr'", "class='tw", "class='vd ",
                                        "class='stamp'", "class='card shc'"))


def render_anim(html_text, width=None):
    """Looping GIF of a card (bytes), or None if anything fails."""
    import subprocess
    import tempfile
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        _log_plain("animations off: Pillow is not installed")
        return None
    chrome = find_chrome()
    if not chrome:
        return None
    try:
        html_text = inline_images(html_text)
    except Exception:  # noqa: BLE001
        pass
    if width is None:
        m = re.search(r"body,\.card\{width:(\d+)px", html_text)
        width = int(m.group(1)) if m else 880
    seek = ("<script>(function(){var t=new URLSearchParams(location.search).get('t');"
            "if(t)document.documentElement.style.setProperty('--t',t);})();</script>")
    card = html_text.replace("</style>", ANIM_CSS + ANIM_FREEZE + "</style>", 1).replace("</head>", seek + "</head>", 1)
    with tempfile.TemporaryDirectory() as d:
        src, grid, out = Path(d) / "card.html", Path(d) / "grid.html", Path(d) / "grid.png"
        src.write_text(card)
        base = _gif_nice() + [chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=15000"]
        try:
            dom = subprocess.run(base + [f"--window-size={width},3000", "--dump-dom", src.as_uri()],
                                 check=True, timeout=60, capture_output=True, text=True).stdout
            m = re.search(r'data-h="(\d+)"', dom)
            height = int(m.group(1)) if m else 900
            cols = 8
            rows = (ANIM_FRAMES + cols - 1) // cols
            cells = "".join(
                f"<iframe src='card.html?t={i * ANIM_SECONDS / ANIM_FRAMES:.3f}' width='{width}' height='{height}' "
                f"style='border:0;display:block;float:left' scrolling='no'></iframe>" for i in range(ANIM_FRAMES))
            grid.write_text(f"<!doctype html><html><head><style>html,body{{margin:0;background:#0d1220}}</style></head>"
                            f"<body style='width:{width * cols}px'>{cells}</body></html>")
            subprocess.run(base + ["--force-device-scale-factor=1", f"--window-size={width * cols},{height * rows}",
                                   f"--screenshot={out}", grid.as_uri()], check=True, timeout=120, capture_output=True)
            sheet = Image.open(out).convert("RGB")
            frames = []
            scale = ANIM_WIDTH / width if width > ANIM_WIDTH else 1.0
            for i in range(ANIM_FRAMES):
                x, y = (i % cols) * width, (i // cols) * height
                f = sheet.crop((x, y, x + width, y + height))
                if scale != 1.0:
                    f = f.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
                frames.append(f)
            # one shared palette for every frame, built from frames across the whole loop (so glows and flashes
            # have their colours too) and applied without dithering: static areas are identical in every frame
            picks = frames[::5]
            pw, ph = max(1, width // 2), max(1, height // 2)
            sheet2 = Image.new("RGB", (pw, ph * len(picks)))
            for k, f0 in enumerate(picks):
                sheet2.paste(f0.resize((pw, ph)), (0, ph * k))
            pal = sheet2.quantize(colors=255, method=Image.Quantize.MEDIANCUT)
            q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
            buf = __import__("io").BytesIO()
            q[0].save(buf, format="GIF", save_all=True, append_images=q[1:], loop=0,
                      duration=int(ANIM_SECONDS * 1000 / ANIM_FRAMES), optimize=True, disposal=1)
            data = buf.getvalue()
            if len(data) > 9_000_000:
                _log_plain(f"animation too big ({len(data) // 1024} KB): posting the still image")
                return None
            return data
        except Exception as e:  # noqa: BLE001
            _log_plain(f"animation failed, posting the still image: {e}")
            return None


_post_card_still = _post_card


def _post_card(hook, html_text, content, embed, name):
    """Big moments are posted as a looping animation; everything else (and any failure) as the still card."""
    if (CONFIG.get("bot") or {}).get("image_only", True) and wants_animation(html_text):
        for nm in list(_SH_HEAD):                             # same SEASON HIGH headline as the still path
            for k in ("img_title", "title"):
                t = embed.get(k)
                if t and nm in t and "SEASON HIGH" not in t:
                    embed[k] = f"🔥 SEASON HIGH! {t}"
        _SH_HEAD.clear()
        gif = render_anim(_inject_headline(html_text, embed))
        if gif:
            return discord(hook, [], name, files={name.rsplit(".", 1)[0] + ".gif": gif})
    return _post_card_still(hook, html_text, content, embed, name)



# ------------------------------------------------ playoffs: clinched / eliminated posts and the bracket

def completed_week(sport, settings):
    """Last week whose games are all finished. NFL: the current week counts as soon as its last game ends."""
    s = settings.get("settings") or {}
    done = int(s.get("last_scored_leg") or 0)
    if sport == "nfl":
        leg = int(s.get("leg") or 0)
        if leg > done:
            try:
                evs = fetch(f"football/nfl/scoreboard?seasontype=2&week={leg}").get("events", [])
                if evs and all(e["status"]["type"].get("completed") for e in evs):
                    done = leg
            except Exception:  # noqa: BLE001
                pass
    return done


def _byes(n_po):
    size = 1
    while size < n_po:
        size *= 2
    return size - n_po                                     # 6-team playoffs -> 2 byes


def playoff_status(settings, rec, week):
    """Per roster: 'bye' (bye clinched), 'in' (spot clinched), 'out' (eliminated) or 'alive', plus the magic number.
    Mathematically certain: ties always count against the team, so nothing is called early."""
    s = settings.get("settings") or {}
    po, n = int(s.get("playoff_week_start") or 15), int(s.get("playoff_teams") or 6)
    byes = _byes(n)
    rem = max(po - 1 - week, 0)
    wins = {r: rec[r]["w"] + 0.5 * rec[r]["t"] for r in rec}
    out = {}
    if rem == 0:                                          # regular season over: the real seeding decides
        order = sorted(rec, key=lambda r: (-wins[r], -rec[r]["pf"]))
        for i, r in enumerate(order):
            out[r] = {"status": "bye" if i < byes else ("in" if i < n else "out"), "magic": None, "seed": i + 1}
        return out
    for r in rec:
        others = [t for t in rec if t != r]
        can_catch = sum(1 for t in others if wins[t] + rem >= wins[r])
        if byes and can_catch <= byes - 1:
            st_ = "bye"
        elif can_catch <= n - 1:
            st_ = "in"
        elif sum(1 for t in others if wins[t] > wins[r] + rem) >= n:
            st_ = "out"
        else:
            st_ = "alive"
        magic = None
        if st_ == "alive":
            magic = next((k for k in range(1, rem + 1)
                          if sum(1 for t in others if wins[t] + rem >= wins[r] + k) <= n - 1), None)
        out[r] = {"status": st_, "magic": magic}
    for i, r in enumerate(sorted(rec, key=lambda r: (-wins[r], -rec[r]["pf"]))):
        out[r]["seed"] = i + 1
    return out


PO_CSS = """
.pw{display:grid;grid-template-columns:520px 1fr;gap:16px;margin-top:12px}
.nw{display:flex;flex-direction:column;gap:10px}
.stp{display:grid;grid-template-columns:64px 1fr auto;gap:14px;align-items:center;border-radius:16px;padding:12px 16px;
 background:rgba(255,255,255,.06);border:2px solid rgba(255,255,255,.12)}
.stp img,.stp .av{width:64px;height:64px;border-radius:50%;object-fit:cover;background:#2a3350;display:flex;align-items:center;
 justify-content:center;font-weight:900;font-size:26px}
.stp b{display:block;font-size:26px;font-weight:900}.stp small{font-size:15px;color:#cfd5e1;font-weight:700}
.stamp{font-size:20px;font-weight:900;letter-spacing:2px;padding:8px 14px;border-radius:10px;transform:rotate(-4deg);white-space:nowrap}
.stp.in{border-color:#1faa59;background:rgba(31,170,89,.16)}.stp.in .stamp{background:#1faa59;color:#fff;box-shadow:0 0 22px rgba(31,170,89,.7)}
.stp.bye{border-color:#ffd166;background:rgba(255,209,102,.14)}.stp.bye .stamp{background:linear-gradient(90deg,#ffd166,#ffb347);color:#1b1305;
 box-shadow:0 0 24px rgba(255,190,70,.8)}
.stp.out{border-color:#7a1f2b;background:rgba(122,31,43,.25)}.stp.out .stamp{background:#3a1218;color:#ff9a9e;border:2px solid #ff9a9e}
.ps{background:rgba(255,255,255,.05);border-radius:14px;padding:8px 12px}
.pr3{display:grid;grid-template-columns:34px 1fr 64px 70px 170px 70px;gap:10px;align-items:center;height:40px;font-size:17px;
 font-variant-numeric:tabular-nums;border-radius:8px;padding:0 10px}
.pr3:nth-child(odd){background:rgba(255,255,255,.035)}
.pr3.h{height:26px;font-size:11.5px;letter-spacing:1.4px;color:#9aa3b5;font-weight:800;background:none}
.pr3 .m{font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pr3 .sd{color:#9aa3b5;font-weight:900}
.pr3.cut{box-shadow:inset 0 -2px 0 rgba(255,209,102,.7)}.pr3.byecut{box-shadow:inset 0 -2px 0 rgba(255,255,255,.25)}
.chip{font-size:11.5px;font-weight:900;letter-spacing:1px;border-radius:6px;padding:3px 8px;text-align:center;white-space:nowrap}
.chip.bye{background:linear-gradient(90deg,#ffd166,#ffb347);color:#1b1305}.chip.in{background:#1faa59;color:#fff}
.chip.out{background:#3a1218;color:#ff9a9e}.chip.alive{background:rgba(255,255,255,.1);color:#dfe5f1}
.chip.mir{background:linear-gradient(90deg,#6d28d9,#a855f7);color:#fff;box-shadow:0 0 12px rgba(168,85,247,.55)}
.pr3 .pc{text-align:right;font-weight:900}
.pr3.new{outline:2px solid #ffd166;outline-offset:-2px}
"""


def _avatar(pe):
    import html as H
    return (f"<img src='https://sleepercdn.com/avatars/thumbs/{H.escape(pe['avatar'])}'>" if pe.get("avatar")
            else f"<div class='av'>{H.escape(pe['manager'][:1].upper())}</div>")


def playoff_picture_html(sport, settings, rec, status, changes, odds, week):
    import html as H
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    people = _league_people(sport)
    s = settings.get("settings") or {}
    n, po = int(s.get("playoff_teams") or 6), int(s.get("playoff_week_start") or 15)
    byes = _byes(n)
    words = {"bye": ("BYE CLINCHED", "a first-round bye is locked in"), "in": ("CLINCHED", "a playoff spot is locked in"),
             "out": ("ELIMINATED", "no way back into the top " + str(n))}
    tiles = []
    for rid, old, new in changes:
        pe = people.get(rid, {"manager": "?"})
        r = rec[rid]
        tiles.append(f"<div class='stp {new}'>{_avatar(pe)}<div><b>{H.escape(pe['manager'])}</b>"
                     f"<small>{r['w']}-{r['l']} · {H.escape(words[new][1])}</small></div>"
                     f"<div class='stamp'>{'🔒 ' if new != 'out' else '❌ '}{words[new][0]}</div></div>")
    rows = ["<div class='pr3 h'><span>#</span><span>MANAGER</span><span>W-L</span><span>PF</span><span>STATUS</span>"
            "<span style='text-align:right'>PLAYOFFS</span></div>"]
    newset = {c[0] for c in changes}
    for rid in sorted(rec, key=lambda r: status[r]["seed"]):
        x, stt = rec[rid], status[rid]
        if stt["status"] == "alive" and odds.get(rid, (0, 0))[0] < 0.5:
            chip = "<span class='chip mir'>NEEDS A MIRACLE</span>"      # still possible on paper, never happens in the sims
        elif stt["status"] == "alive":
            chip = (f"<span class='chip alive'>{stt['magic']} WIN{'S' if stt['magic'] != 1 else ''} TO CLINCH</span>"
                    if stt["magic"] else "<span class='chip alive'>IN THE HUNT</span>")
        else:
            chip = f"<span class='chip {stt['status']}'>{ {'bye': 'BYE ✓', 'in': 'CLINCHED ✓', 'out': 'ELIMINATED'}[stt['status']] }</span>"
        cls = " cut" if stt["seed"] == n else (" byecut" if stt["seed"] == byes and byes else "")
        rows.append(f"<div class='pr3{cls}{' new' if rid in newset else ''}'><span class='sd'>{stt['seed']}</span>"
                    f"<span class='m'>{H.escape(people.get(rid, {}).get('manager', '?'))}</span>"
                    f"<span>{x['w']}-{x['l']}{'-' + str(x['t']) if x['t'] else ''}</span><span>{x['pf']:.0f}</span>{chip}"
                    f"<span class='pc'>{odds.get(rid, (0, 0))[0]:.0f}%</span></div>")
    ins = [people[c[0]]["manager"] for c in changes if c[2] in ("in", "bye") and c[0] in people]
    outs = [people[c[0]]["manager"] for c in changes if c[2] == "out" and c[0] in people]
    head = " · ".join(x for x in (
        (" & ".join(ins) + (" clinch" if len(ins) > 1 else " clinches") + " a playoff spot!") if ins else "",
        (" & ".join(outs) + (" are" if len(outs) > 1 else " is") + " eliminated") if outs else "") if x)
    left = max(po - 1 - week, 0)
    body = (f"<div class='top'><div class='lg'><img src='{logo}'>{H.escape((settings.get('name') or '').upper())} · PLAYOFF PICTURE"
            f" · AFTER WEEK {week}</div><div class='pill'>{left} WEEK{'S' if left != 1 else ''} LEFT · TOP {n} · {byes} BYES</div></div>"
            f"<div class='hl'><div class='h1'>{H.escape(head)}</div>"
            f"<div class='h2'>The gold line is the playoff cut; the top {byes} skip the first round.</div></div>"
            f"<div class='pw'><div class='nw'>{''.join(tiles)}</div><div class='ps'>{''.join(rows)}</div></div>")
    html_text = _card("--a:#1faa59;--h:#d4af37", body, None, HL_CSS + PO_CSS + "body,.card{width:1400px!important}", width=1400)
    return _nba_theme(html_text, None, "PLAYOFF PICTURE") if sport == "nba" else html_text


# ---- bracket

def team_model(scores):
    """Mean and spread of each team's weekly score (shrunk towards the league average)."""
    allp = [p for v in scores.values() for p in v if p > 0]
    lg = sum(allp) / len(allp) if allp else 100
    sd = ((sum((p - lg) ** 2 for p in allp) / max(1, len(allp) - 1)) ** 0.5 if allp else 20) * 0.85
    return {r: ((sum(p for p in v if p > 0) + 2 * lg) / (len([p for p in v if p > 0]) + 2)) for r, v in scores.items()}, max(sd, 1.0)


def build_bracket(sport, settings, status, week):
    """Seeds, byes, every playoff game so far (with scores) and what's next. Standard Sleeper format:
    round 1 = 3v6 and 4v5, then 1 plays the 4/5 winner and 2 the 3/6 winner, then the final."""
    s = settings.get("settings") or {}
    po = int(s.get("playoff_week_start") or 15)
    seed = {status[r]["seed"]: r for r in status if status[r]["seed"] <= int(s.get("playoff_teams") or 6)}
    lid = str(CONFIG[sport].get("sleeper_league_id")).strip()

    def pts(wk):
        if wk > week:
            return {}
        try:
            return {m["roster_id"]: float(m.get("points") or 0) for m in fetch_url(f"{SLEEPER}/league/{lid}/matchups/{wk}")}
        except Exception:  # noqa: BLE001
            return {}

    def game(a, b, wk):
        p = pts(wk)
        g = {"a": a, "b": b, "wk": wk, "pa": p.get(a), "pb": p.get(b), "win": None}
        if a is not None and b is not None and wk <= week and g["pa"] is not None and g["pb"] is not None:
            g["win"] = a if g["pa"] >= g["pb"] else b
        return g
    r1 = [game(seed.get(4), seed.get(5), po), game(seed.get(3), seed.get(6), po)]
    r2 = [game(seed.get(1), r1[0]["win"], po + 1), game(seed.get(2), r1[1]["win"], po + 1)]
    fin = game(r2[0]["win"], r2[1]["win"], po + 2)
    return {"seed": seed, "r1": r1, "r2": r2, "final": fin, "champ": fin["win"], "po": po}


def bracket_odds(br, scores, sims=6000):
    """Title chances from here: simulate every unplayed game with each team's scoring level."""
    import random as _r
    mean, sd = team_model(scores)
    rnd = _r.Random(11)
    wins = {}

    def play(a, b):
        return a if rnd.gauss(mean.get(a, 100), sd) >= rnd.gauss(mean.get(b, 100), sd) else b
    for _ in range(sims):
        w1 = [g["win"] or play(g["a"], g["b"]) for g in br["r1"]]
        w2 = [br["r2"][i]["win"] or play(br["r2"][i]["a"], w1[i]) for i in range(2)]
        ch = br["final"]["win"] or play(w2[0], w2[1])
        wins[ch] = wins.get(ch, 0) + 1
    return {r: 100.0 * c / sims for r, c in wins.items()}


BR_CSS = """
.bkg{display:grid;grid-template-columns:1fr 1fr 1fr 300px;gap:22px;margin-top:14px;align-items:stretch}
.bkc{display:flex;flex-direction:column;gap:10px}.bkc .bkm{flex:1;display:flex;flex-direction:column;justify-content:space-around;gap:14px}
.bkc .bkh{font-size:12px;letter-spacing:2px;color:#9aa3b5;font-weight:900;text-align:center}
.bkmu{background:rgba(255,255,255,.06);border-radius:14px;padding:6px;display:flex;flex-direction:column;gap:5px;position:relative}
.bks{display:grid;grid-template-columns:30px 40px 1fr auto;gap:10px;align-items:center;border-radius:10px;padding:6px 10px;
 background:rgba(0,0,0,.22);min-height:54px}
.bks img,.bks .bkav{width:40px;height:40px;border-radius:50%;object-fit:cover;background:#2a3350;display:flex;align-items:center;
 justify-content:center;font-weight:900;font-size:17px}
.bks .bksd{font-weight:900;color:#ffd166;text-align:center;font-size:16px}
.bks b{display:block;font-size:18px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bks small{font-size:12.5px;color:#aab3c5;font-weight:700}
.bks .bksc{font-size:22px;font-weight:900;font-variant-numeric:tabular-nums}.bks .bkod{font-size:15px;font-weight:900;color:#7ee2a8}
.bks.bkw{background:linear-gradient(90deg,rgba(255,209,102,.28),rgba(255,209,102,.08));box-shadow:inset 4px 0 0 #ffd166}
.bks.bkl{opacity:.42}.bks.bkt b{color:#8b95a8}
.bkbye{font-size:12px;letter-spacing:1.5px;font-weight:900;color:#ffd166;text-align:center;padding:4px}
.bkch{border-radius:18px;padding:18px;text-align:center;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;
 background:radial-gradient(circle at 50% 30%,rgba(255,209,102,.35),rgba(255,209,102,.05) 70%);border:2px solid rgba(255,209,102,.6)}
.bkch .bktr{font-size:70px;line-height:1}.bkch b{font-size:28px;font-weight:900}.bkch small{font-size:14px;color:#dfe5f1;font-weight:700}
.bkch img,.bkch .bkav{width:96px;height:96px;border-radius:50%;object-fit:cover;background:#2a3350;border:4px solid #ffd166;
 box-shadow:0 0 30px rgba(255,209,102,.8);display:flex;align-items:center;justify-content:center;font-weight:900;font-size:36px}
.bkodl{display:flex;flex-direction:column;gap:6px;width:100%;margin-top:6px}
.bkodl div{display:grid;grid-template-columns:1fr 52px;font-size:15px;font-weight:800;text-align:left}
.bkodl i{grid-column:1/-1;height:7px;border-radius:4px;background:rgba(255,255,255,.08);overflow:hidden;font-style:normal}
.bkodl i em{display:block;height:7px;background:linear-gradient(90deg,#ffd166,#ffb347)}
"""


def _bkav(pe):
    return _avatar(pe).replace("class='av'", "class='bkav'")


def bracket_html(sport, settings, br, rec, odds, stage):
    import html as H
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    people = _league_people(sport)
    seed_of = {r: s for s, r in br["seed"].items()}

    def slot(rid, g=None, side=None):
        if rid is None:
            return "<div class='bks bkt'><span class='bksd'>?</span><div class='bkav'>?</div><div><b>To be decided</b></div><span></span></div>"
        pe = people.get(rid, {"manager": "?"})
        x = rec.get(rid, {"w": 0, "l": 0})
        cls, right = "", (f"<span class='bkod'>{odds.get(rid, 0):.0f}%</span>" if odds else "<span></span>")
        if g and g.get("win"):
            cls = " bkw" if g["win"] == rid else " bkl"
            right = f"<span class='bksc'>{(g['pa'] if side == 'a' else g['pb']):.1f}</span>"
        return (f"<div class='bks{cls}'><span class='bksd'>{seed_of.get(rid, '')}</span>{_bkav(pe)}"
                f"<div><b>{H.escape(pe['manager'])}</b><small>{x['w']}-{x['l']}</small></div>{right}</div>")

    def mu(g, note=""):
        return f"<div class='bkmu'>{slot(g['a'], g, 'a')}{slot(g['b'], g, 'b')}{note}</div>"
    po = br["po"]
    r1 = (f"<div class='bkc'><div class='bkh'>ROUND 1 · WEEK {po}</div><div class='bkm'>"
          f"<div class='bkmu'>{slot(br['seed'].get(1))}<div class='bkbye'>BYE TO THE SEMI-FINALS</div></div>"
          f"{mu(br['r1'][0])}{mu(br['r1'][1])}"
          f"<div class='bkmu'>{slot(br['seed'].get(2))}<div class='bkbye'>BYE TO THE SEMI-FINALS</div></div></div></div>")
    r2 = (f"<div class='bkc'><div class='bkh'>SEMI-FINALS · WEEK {po + 1}</div><div class='bkm'>"
          f"{mu(br['r2'][0])}{mu(br['r2'][1])}</div></div>")
    fn = f"<div class='bkc'><div class='bkh'>FINAL · WEEK {po + 2}</div><div class='bkm'>{mu(br['final'])}</div></div>"
    if br["champ"]:
        pe = people.get(br["champ"], {"manager": "?"})
        side = (f"<div class='bkch'><div class='bktr'>🏆</div>{_bkav(pe)}<b>{H.escape(pe['manager'])}</b>"
                f"<small>{H.escape((settings.get('name') or 'League'))} champion {settings.get('season', '')}</small></div>")
    else:
        alive = sorted(odds.items(), key=lambda kv: -kv[1])
        bars = "".join(f"<div><span>{H.escape(people.get(r, {}).get('manager', '?'))}</span><span>{p:.0f}%</span>"
                       f"<i><em style='width:{max(p, 2):.0f}%'></em></i></div>" for r, p in alive)
        side = f"<div class='bkch'><div class='bktr'>🏆</div><small>TITLE ODDS · SIMULATED 6,000 TIMES</small><div class='bkodl'>{bars}</div></div>"
    titles = {0: "THE PLAYOFFS ARE SET", 1: "ROUND 1 RESULTS", 2: "THE FINAL IS SET", 3: "CHAMPION CROWNED"}
    head = {0: "Six teams, one trophy: the bracket is set",
            1: "Round 1 is done: the semi-finals are set",
            2: "Two left standing: it's the final",
            3: f"🏆 {people.get(br['champ'], {}).get('manager', '?')} is the champion!"}.get(stage, "")
    body = (f"<div class='top'><div class='lg'><img src='{logo}'>{H.escape((settings.get('name') or '').upper())} · PLAYOFFS "
            f"{settings.get('season', '')}</div><div class='pill'>{titles.get(stage, 'PLAYOFFS')}</div></div>"
            f"<div class='hl'><div class='h1'>{H.escape(head)}</div></div>"
            f"<div class='bkg'>{r1}{r2}{fn}{side}</div>")
    html_text = _card("--a:#d4af37;--h:#5b7bd5", body, None,
                      HL_CSS + BR_CSS + "body,.card{width:1560px!important}",
                      width=1560)
    if br["champ"]:
        html_text = html_text.replace("<div class='card'", "<div class='card shc'", 1).replace("</style>", SH_CSS + "</style>", 1)
    return _nba_theme(html_text, None, "PLAYOFFS") if sport == "nba" else html_text


def maybe_playoffs(st):
    """Post when someone clinches / is eliminated, the bracket when the playoffs are set, after each round, and the champion."""
    for sport in ("nfl", "nba"):
        if not CONFIG[sport].get("sleeper_league_id") or not (CONFIG[sport].get("playoff_posts") or {}).get("enabled", True):
            continue
        try:
            settings = league_settings(sport)
            s = settings.get("settings") or {}
            po = int(s.get("playoff_week_start") or 15)
            week = completed_week(sport, settings)
            if week < 1:
                continue
            reg = min(week, po - 1)
            scores, rec = season_history(sport, reg)
            if not rec:
                continue
            status = playoff_status(settings, rec, reg)
            key = f"{sport}-{settings.get('season')}"
            store = st.setdefault("playoffs", {}).setdefault(key, {})
            done_at = store.setdefault("week_done_at", {}).setdefault(str(week), time.time())
            recap_done = f"{sport}-{settings.get('season')}-{week}" in (st.get("recaps") or [])
            after_recap = recap_done or time.time() - done_at >= 40 * 3600   # goes out right below the weekly recap
            prev = store.get("status")
            if prev is None:                                  # first look: remember quietly
                store["status"] = {str(r): v["status"] for r, v in status.items()}
            else:
                changes = [(r, prev.get(str(r)), v["status"]) for r, v in status.items()
                           if v["status"] in ("in", "bye", "out") and prev.get(str(r)) != v["status"]
                           and not (prev.get(str(r)) == "bye" and v["status"] == "in")]
                if changes and not after_recap:
                    continue                                  # hold it for the recap; nothing is lost
                if changes:
                    odds, _, _ = simulate_odds(sport, settings, scores, rec, reg)
                    hook = NFL_HOOK if sport == "nfl" else NBA_HOOK
                    html_text = playoff_picture_html(sport, settings, rec, status, changes, odds, reg)
                    post = _nba_post if sport == "nba" else (lambda h, c, e, n: _post_card(hook, h, c, e, n))
                    if post(html_text, "🔒 **PLAYOFF PICTURE**", {}, "playoffs.png"):
                        store["status"] = {str(r): v["status"] for r, v in status.items()}
                else:
                    store["status"] = {str(r): v["status"] for r, v in status.items()}
            if week >= po - 1 and after_recap:                # bracket: set, after each round, champion (after the recap)
                stage = min(week - (po - 1), 3)
                if store.get("bracket_stage", -1) < stage:
                    br = build_bracket(sport, settings, status, week)
                    hscores, _ = season_history(sport, po - 1)
                    odds = {} if br["champ"] else bracket_odds(br, hscores)
                    html_text = bracket_html(sport, settings, br, rec, odds, stage)
                    hook = NFL_HOOK if sport == "nfl" else NBA_HOOK
                    post = _nba_post if sport == "nba" else (lambda h, c, e, n: _post_card(hook, h, c, e, n))
                    if post(html_text, "🏆 **PLAYOFFS**", {}, "bracket.png"):
                        store["bracket_stage"] = stage
        except Exception as ex:  # noqa: BLE001
            log(f"{sport} playoff posts error: {ex}")


_maybe_daily_race_po = maybe_daily_race


def maybe_daily_race(st):
    try:
        maybe_playoffs(st)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"playoff check failed: {ex}")
    return _maybe_daily_race_po(st)



# ------------------------------------------------ go-live safety: restart only between games, and a self-test

HANDOFF_AFTER_MIN = 180         # after 3 hours, restart at the first quiet moment (hard limit stays MAX_RUNTIME)


def quiet_now(margin_min=15):
    """True when no NFL/NBA game is live and nothing kicks off in the next `margin_min` minutes."""
    now = now_utc()
    paths = ["football/nfl/scoreboard"]
    today = datetime.now(ET).date()
    paths += [f"basketball/nba/scoreboard?dates={(today + timedelta(days=d)).strftime('%Y%m%d')}" for d in (-1, 0, 1)]
    try:
        for p in paths:
            for e in fetch(p).get("events", []):
                stt = e["status"]["type"]["state"]
                if stt == "in":
                    return False
                if stt == "pre":
                    mins = (parse_time(e["date"]) - now).total_seconds() / 60
                    if -30 < mins < margin_min:              # about to start (or late starting)
                        return False
        return True
    except Exception:  # noqa: BLE001
        return False                                         # can't tell: keep running, the hard limit still applies


def _code_version():
    import hashlib
    try:
        return hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]
    except Exception:  # noqa: BLE001
        return "?"


def maybe_selftest(st):
    """Once per code update: build a real animated touchdown card on this server, time it, and report privately."""
    ver = _code_version()
    if not ALERT_HOOK or st.get("selftest") == ver or SILENT or DRY_RUN or CAPTURE is not None:
        return
    st["selftest"] = ver
    lines, ok = [], True

    def check(name, fn):
        nonlocal ok
        t0 = time.time()
        try:
            res = fn()
            lines.append(f"✅ {name}" + (f" · {res}" if res else "") + f" ({time.time() - t0:.1f}s)")
            return True
        except Exception as e:  # noqa: BLE001
            ok = False
            lines.append(f"❌ {name}: {str(e)[:120]}")
            return False
    check("ESPN", lambda: f"{len(fetch('football/nfl/scoreboard').get('events', []))} NFL games this week")
    check("Sleeper", lambda: f"week {fetch_url(SLEEPER + '/state/nfl').get('week')}")
    check("Chrome + Pillow", lambda: "ready" if _animations_ready() else (_ for _ in ()).throw(RuntimeError("animations unavailable")))
    check("Webhooks", lambda: "NFL + NBA set" if NFL_HOOK and NBA_HOOK else (_ for _ in ()).throw(RuntimeError("a webhook is missing")))
    build = {}

    def card():
        wk = max(int(fetch_url(SLEEPER + "/state/nfl").get("week") or 2) - 1, 1)
        evs = fetch(f"football/nfl/scoreboard?seasontype=2&week={wk}").get("events", [])
        for e in evs:
            if not e["status"]["type"].get("completed"):
                continue
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            tds = [p for p in summary.get("scoringPlays") or [] if "Touchdown" in (p.get("type") or {}).get("text", "")]
            if not tds:
                continue
            ctx = nfl_ctx(st, evs)
            html_text = nfl_td_fantasy_html(e, tds[0], summary, ctx["live"], ctx["ol"], ctx["base"], "")
            t0 = time.time()
            gif = render_anim(html_text)
            build["secs"] = time.time() - t0
            if not gif:
                raise RuntimeError("the animation could not be built (posts would fall back to still images)")
            build["gif"] = gif
            return f"{len(gif) // 1024} KB, built in {build['secs']:.0f}s (head start is {PREP_SECONDS}s)"
        raise RuntimeError("no finished game with a touchdown found")
    check("Animated touchdown card", card)
    if build.get("secs", 0) > PREP_SECONDS - 5:
        ok = False
        lines.append(f"⚠️ Animated cards take {build['secs']:.0f}s here: posts could land a few seconds late. "
                     f"Raise PREP_SECONDS or lower ANIM_FRAMES.")
    where = "GitHub's server" if os.environ.get("GITHUB_ACTIONS") == "true" else "the Oracle server"
    head = f"✅ **Self-test passed** on {where}" if ok else "⚠️ **Self-test found a problem**"
    msg = f"{head} (code {ver})\n" + "\n".join(lines)
    _POSTER["name"] = "Donald 🩺"
    _IN_ALERT["on"] = True
    try:
        discord(ALERT_HOOK, [], "selftest", content=msg[:1900],
                files={"selftest.gif": build["gif"]} if build.get("gif") else None)
    finally:
        _POSTER.clear()
        _IN_ALERT["on"] = False


_maybe_daily_race_st = maybe_daily_race


def maybe_daily_race(st):
    try:
        maybe_selftest(st)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"self-test failed to run: {ex}")
    return _maybe_daily_race_st(st)



# ------------------------------------------------ quality of life: live switches, catch-up, weekly matchups board, corrections, report

# ---- pause / test mode switches, re-read from GitHub every 2 minutes (no restart needed)
_LIVE_CFG = {"t": 0.0, "paused": None}
ENV_FILE = Path("/etc/sports-bot.env")                    # server secrets (e.g. a new DISCORD_BOT_TOKEN)


def _env_mtime():
    try:
        return ENV_FILE.stat().st_mtime                   # readable even though the file itself is root-only
    except OSError:
        return None


_ENV_AT_START = _env_mtime()


def refresh_switches():
    """Pick up "paused" and "test_mode" from config.json on GitHub (edit it there; takes effect within ~2 minutes)."""
    on_server = os.environ.get("GITHUB_ACTIONS") != "true" and (ROOT / ".git").exists() and \
        os.environ.get("KEEPALIVE") == "1"
    if time.time() - _LIVE_CFG["t"] < 120 or not (os.environ.get("GITHUB_ACTIONS") == "true" or on_server):
        return
    _LIVE_CFG["t"] = time.time()
    import subprocess
    try:
        subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=ROOT, capture_output=True, timeout=30)
        txt = subprocess.run(["git", "show", "origin/main:config.json"], cwd=ROOT, capture_output=True, text=True,
                             timeout=15).stdout
        fresh = json.loads(txt)
        if on_server:                     # new code uploaded to GitHub? restart onto it at the next quiet moment
            import hashlib
            remote = subprocess.run(["git", "show", "origin/main:bot.py"], cwd=ROOT, capture_output=True,
                                    timeout=15).stdout
            local = (ROOT / "bot.py").read_bytes()
            new = bool(remote) and hashlib.sha1(remote).digest() != hashlib.sha1(local).digest()
            remote_cfg = txt.encode()
            new = new or (bool(remote_cfg) and remote_cfg != (ROOT / "config.json").read_bytes())
            if _ENV_AT_START is not None and _env_mtime() not in (None, _ENV_AT_START):
                if not _LIVE_CFG.get("env_changed"):
                    _log_plain("server settings changed (/etc/sports-bot.env): will restart at the next quiet moment")
                _LIVE_CFG["env_changed"] = True
                new = True
            if new and not _LIVE_CFG.get("new_code"):
                _log_plain("new code/config on GitHub: will restart onto it at the next quiet moment")
            _LIVE_CFG["new_code"] = new
    except Exception as e:  # noqa: BLE001
        _log_plain(f"switch refresh skipped: {e}")
        return
    was = bool(CONFIG.get("paused"))
    CONFIG["paused"] = bool(fresh.get("paused"))
    CONFIG["test_mode"] = fresh.get("test_mode") or {}
    if CONFIG["paused"] != was:
        held = sum(_HELD.values())
        _ALERTED.pop("pause", None)
        alert("⏸️ Posting PAUSED: nothing goes to the main channel until you switch it back" if CONFIG["paused"] else
              f"▶️ Posting RESUMED · {held} held post{'s' if held != 1 else ''} going out now", "pause", 0)
        if not CONFIG["paused"]:
            _HELD.clear()


def _avatar_or_head(url):
    import html as H
    return f"<img src='{H.escape(url or '')}'>"


# ---- 2) morning catch-up (09:00 UK): everything that finished overnight
CUP_CSS = """
.cg{display:grid;grid-template-columns:1fr 1.15fr;gap:16px;margin-top:12px}
.sec2{font-size:12.5px;letter-spacing:2px;color:#9aa3b5;font-weight:900;margin-bottom:8px}
.cts{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.ct{display:grid;grid-template-columns:30px 1fr 10px 1fr 30px;align-items:center;gap:4px;background:rgba(255,255,255,.06);border-radius:10px;padding:8px}
.ct img{width:30px;height:30px;object-fit:contain}.ct b{font-size:20px;font-weight:900;text-align:center}.ct b.lo{color:#8b95a8}.ct i{color:#6f7788;font-style:normal}
.cr{display:grid;grid-template-columns:46px 1fr 78px 140px;align-items:center;gap:10px;background:rgba(255,255,255,.06);border-radius:10px;padding:6px 10px;margin-bottom:6px}
.cr img{width:46px;height:46px;border-radius:50%;object-fit:cover;object-position:top;background:#1d2436}
.cr b{display:block;font-size:17px;font-weight:900}.cr small{font-size:13px;color:#aab3c5;font-weight:700}
.cr .fp{font-size:22px;font-weight:900;text-align:center;background:linear-gradient(160deg,#ffe08a,#f5b942);color:#1b1305;border-radius:8px;padding:3px 0}
.cr em{font-style:normal;font-size:11px;font-weight:900;letter-spacing:1px;color:#ffb000}
.cr.sh{box-shadow:0 0 0 2px #ffb000,0 0 18px rgba(255,140,0,.5)}
.mg{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
.mg div{background:rgba(255,255,255,.06);border-radius:10px;padding:10px 12px}.mg small{font-size:11.5px;letter-spacing:1.5px;color:#9aa3b5;font-weight:900}
.mg b{display:block;font-size:19px;font-weight:900;margin-top:4px}
"""


def overnight_games(sport):
    """Finished games that tipped / kicked off between 21:00 last night and 06:00 this morning (UK)."""
    now = datetime.now(UK)
    lo = (now - timedelta(days=1)).replace(hour=21, minute=0, second=0, microsecond=0)
    hi = now.replace(hour=6, minute=0, second=0, microsecond=0)
    out = []
    days = {(now - timedelta(days=1)).astimezone(ET).strftime("%Y%m%d"), now.astimezone(ET).strftime("%Y%m%d"),
            (now - timedelta(days=1)).strftime("%Y%m%d"), now.strftime("%Y%m%d")}
    path = "football/nfl/scoreboard?dates=" if sport == "nfl" else "basketball/nba/scoreboard?dates="
    seen = set()
    for d in sorted(days):
        for e in fetch(path + d).get("events", []):
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            if e["status"]["type"].get("completed") and lo <= parse_time(e["date"]).astimezone(UK) < hi:
                out.append(e)
    return sorted(out, key=lambda e: e["date"])


def catchup_html(sport, events):
    import html as H
    rows = []
    for e in events:
        summ = fetch(f"{'football/nfl' if sport == 'nfl' else 'basketball/nba'}/summary?event={e['id']}")
        if sport == "nba":
            for r in nba_league_rows(summ):
                rows.append(dict(r, _e=e, _pts=r["fp"], _img=r.get("hs")))
        else:
            for p in nfl_fantasy(summ):
                o = league_owner(p.get("id"), p["name"])
                rows.append(dict(p, owner=o["manager"] if o else None, starting=bool(o and o["starting"]), _e=e,
                                 _pts=p["pts"], _img=nfl_headshot(p.get("id"))))
    owned = sorted([r for r in rows if r.get("owner")], key=lambda r: -r["_pts"])
    if not owned:
        return None, None
    highs = {}
    for r in owned[:8]:
        r2 = dict(r, fp=r["_pts"])
        highs.update(season_highs(sport, [r2], r["_e"]))
    night = {}
    for r in owned:
        if r["starting"]:
            night[r["owner"]] = night.get(r["owner"], 0) + r["_pts"]
    tiles = "".join(
        f"<div class='ct'><img src='{H.escape(a['team'].get('logo', ''))}'><b class='{'lo' if int(num(a.get('score'))) < int(num(h.get('score'))) else ''}'>"
        f"{a.get('score')}</b><i>–</i><b class='{'lo' if int(num(h.get('score'))) < int(num(a.get('score'))) else ''}'>{h.get('score')}</b>"
        f"<img src='{H.escape(h['team'].get('logo', ''))}'></div>" for e in events for a, h in [teams_of(e)])
    tops = "".join(
        f"<div class='cr{' sh' if r['name'] in highs else ''}'>{_avatar_or_head(r.get('_img'))}<div><b>{H.escape(r['name'])}</b>"
        f"<small>{H.escape(r['owner'])} · {'starting' if r['starting'] else 'bench'}</small></div>"
        f"<span class='fp'>{fmt_pts(r['_pts'])}</span><em>{'🔥 SEASON HIGH' if r['name'] in highs else ''}</em></div>"
        for r in owned[:6])
    best = max(night, key=night.get) if night else None
    quiet = min(night, key=night.get) if night else None
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    league = CONFIG[sport].get("league_name", "League")
    n = len(events)
    head = f"🌙 While you were asleep: {n} game{'s' if n != 1 else ''}" + (f", {len(highs)} season high{'s' if len(highs) != 1 else ''}" if highs else "")
    mgr = ""
    if best:
        mgr = (f"<div class='mg'><div><small>🏆 BEST NIGHT</small><b>{H.escape(best)} · {night[best]:.1f}</b></div>"
               + (f"<div><small>🥶 QUIET NIGHT</small><b>{H.escape(quiet)} · {night[quiet]:.1f}</b></div>" if quiet != best else "<div></div>")
               + "</div>")
    body = (f"<div class='top'><div class='lg'><img src='{logo}'>{H.escape(league.upper())} · OVERNIGHT</div>"
            f"<div class='pill'>🌙 WHILE YOU WERE ASLEEP</div></div>"
            f"<div class='hl'><div class='h1'>{H.escape(head)}</div>"
            f"<div class='h2'>Top of the night: <b>{H.escape(owned[0]['name'])}</b> {fmt_pts(owned[0]['_pts'])} for {H.escape(owned[0]['owner'])}</div></div>"
            f"<div class='cg'><div><div class='sec2'>FINAL SCORES</div><div class='cts'>{tiles}</div>{mgr}</div>"
            f"<div><div class='sec2'>TOP FANTASY PERFORMANCES</div>{tops}</div></div>")
    html_text = _card("--a:#ee6730;--h:#ffb347" if sport == "nba" else "--a:#1faa59;--h:#5b7bd5", body, None,
                      HL_CSS + CUP_CSS + "body,.card{width:1400px!important}", width=1400)
    return (_nba_theme(html_text, None, "MORNING CATCH-UP") if sport == "nba" else html_text), head


def maybe_catchup(st):
    now = datetime.now(UK)
    hour = int((CONFIG.get("catchup") or {}).get("hour_uk", 9))
    if not (CONFIG.get("catchup") or {}).get("enabled", True) or not (hour <= now.hour < 12):
        return
    for sport in ("nfl", "nba"):
        key = f"{sport}-{now:%Y-%m-%d}"
        done = st.setdefault("catchups", [])
        if key in done:
            continue
        try:
            evs = overnight_games(sport)
            html_text, head = catchup_html(sport, evs) if evs else (None, None)
            if not html_text:
                done.append(key)
                continue
            ok = (_nba_post(html_text, "🌙 **MORNING CATCH-UP**", {}, "catchup.png") if sport == "nba"
                  else _post_card(NFL_HOOK, html_text, "🌙 **MORNING CATCH-UP**", {}, "catchup.png"))
            if ok:
                done.append(key)
        except Exception as ex:  # noqa: BLE001
            log(f"{sport} morning catch-up error: {ex}")
        st["catchups"] = done[-30:]


def discord_edit(hook, message_id, png=None, name="live.png", content=None):
    """Edit one of our own webhook messages: swap its image and/or text. Posts in the newer layout are edited in
    that layout (image in a media gallery, text as a text block); classic posts the classic way."""
    tries = []
    if _v2_on() and (png or content is not None):
        pl = {"flags": V2_FLAG, "components": _v2_components(name, content)}
        if png:
            pl["attachments"] = [{"id": 0, "filename": name}]
        tries.append((pl, True))
    classic = {}
    if content is not None:
        classic["content"] = content
    if png:
        classic["attachments"] = [{"id": 0, "filename": name}]
    tries.append((classic, False))
    for pl, v2 in tries:
        body, ctype = _post_body(pl, {name: png} if png else None)
        if not png:
            body, ctype = json.dumps(pl).encode(), "application/json"
        req = urllib.request.Request(f"{hook}/messages/{message_id}" + ("?with_components=true" if v2 else ""),
                                     data=body, method="PATCH",
                                     headers={"Content-Type": ctype, "User-Agent": "DiscordBot (sports-bot, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:200]
            if v2 and e.code == 400:
                continue                                # a classic post: edit it the classic way
            _log_plain(f"message edit failed ({e.code}): {msg}")
            return e.code == 404 and None
        except Exception as e:  # noqa: BLE001
            _log_plain(f"message edit failed: {e}")
            return False
    return False


# ---- 3) weekly matchups board: ONE message per fantasy week, pinned, edited in place
#   NFL: posted 00:00 UK on the day of the week's first kick-off (normally Friday = Thursday night), pinned,
#        unpinned Tuesday 12:00 UK.
#   NBA: posted Monday 12:00 UK, pinned, replaced the next Monday 12:00 UK (old one unpinned, new one pinned).
#   Each matchup: points now, projected final (points + Sleeper projections still to come), win chance,
#   players / games left. Edited every 5 minutes while games are live (spoiler-safe: a snapshot is only shown
#   spoiler_delay() after it was taken), hourly otherwise (lineups and projections move), once more at the end.
#   Pinning needs a Discord bot token (DISCORD_BOT_TOKEN): webhooks can't pin. Without one the boards still post.
import math as _math

WB_CSS = """
.wbh{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;margin:12px 2px 0}
.wbh .h1{font-size:31px;font-weight:900;letter-spacing:-.3px;line-height:1.1;white-space:nowrap}
.wbh .h2{font-size:17px;font-weight:700;color:#cfd5e1;text-align:right;line-height:1.3;white-space:nowrap}
.wbh .h2 b{color:#fff;font-weight:900}
.wbg{display:grid;grid-template-columns:1fr 1fr;gap:14px 16px;margin-top:6px}
.wch{display:grid;grid-template-columns:minmax(0,1fr) 128px 100px;gap:10px;padding:0 14px 0 70px;
 font-size:13.5px;font-weight:900;letter-spacing:2px;color:#9aa3b5}
.wch span{text-align:right}.wch span.w{text-align:center}.wch span.p{color:#ffd166}
.wm{position:relative;background:rgba(255,255,255,.065);border-radius:14px;padding:3px 14px}
.wm.close{box-shadow:inset 0 0 0 2px rgba(255,209,102,.6)}
.wr{display:grid;grid-template-columns:46px minmax(0,1fr) 128px 100px;gap:10px;align-items:center;height:80px}
.wr+.wr{border-top:1px solid rgba(255,255,255,.08)}
.wr .wav,.wr img{width:46px;height:46px;border-radius:50%;object-fit:cover;background:#2a3246;display:flex;align-items:center;
 justify-content:center;font-size:21px;font-weight:900;color:#cfd5e1}
.wr .wn{font-size:24px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.15;letter-spacing:-.2px}
.wr .wn.s{font-size:20px}
.wr .ws{font-size:18px;font-weight:700;color:#aab3c5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:3px}
.wr .ws b{color:#fff;font-weight:900}.wr .ws .lv{color:#5ef08f;font-weight:800}.wr .ws .dn{color:#8b95a8}
.wr .wnum{text-align:right;font-variant-numeric:tabular-nums;line-height:1}
.wr .wpj{font-size:37px;font-weight:900;color:#ffd166;letter-spacing:-.6px}
.wr .wno{font-size:18px;font-weight:800;color:#e6e9ef;margin-top:5px}.wr .wno i{font-style:normal;color:#9aa3b5;font-weight:700}
.wr .wp{height:56px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:34px;font-weight:900;
 font-variant-numeric:tabular-nums;letter-spacing:-.6px;background:rgba(255,255,255,.07);color:#8b95a8}
.wr .wp small{font-size:18px;margin-left:1px}
.wr.fav .wp{background:linear-gradient(180deg,#4be38e,#1faa59);color:#062312;box-shadow:0 0 16px rgba(46,204,113,.35)}
.wr.won .wp{background:linear-gradient(180deg,#ffe08a,#f5b942);color:#1b1305;box-shadow:0 0 16px rgba(245,185,66,.45);font-size:26px}
.wr.lost .wp{font-size:24px}
.wr.dog .wn,.wr.lost .wn{color:#c3c9d6}
.wr.lost .wpj{color:#9aa3b5}
.wtag{position:absolute;top:-10px;right:14px;font-size:12px;font-weight:900;letter-spacing:1.4px;padding:2px 9px;border-radius:6px;
 background:#ffd166;color:#1b1305}
.wtag.lk{background:#3a4357;color:#e6e9ef}.wtag.fn{background:#e6e9ef;color:#101522}.wtag.cb{background:#ff8a3d;color:#1b0d03}
.lv2{display:inline-block;background:#ff3d3d;color:#fff;font-weight:900;font-size:12px;letter-spacing:1.5px;padding:2px 8px;
 border-radius:5px;margin-right:8px}
.lv2.f{background:#4b5563}.lv2.p{background:#1faa59}
.top{font-size:21px!important;letter-spacing:1.5px!important}.top img{width:32px!important;height:32px!important}
.top .pill{font-size:20px!important;padding:5px 12px!important}.lv2{font-size:18px!important}
.nbar{font-size:22px!important;padding:8px 32px!important}
.wbh .h2{font-size:22px!important}
.wch{font-size:19px!important;padding-left:72px!important}
.wr{height:80px!important}.wr .ws{font-size:22px!important}.wr .wno{font-size:22px!important}
.wbh{flex-wrap:wrap;row-gap:4px}.wbh .h1{font-size:30px}.wr .wpj{font-size:35px}
.wtag{font-size:16px!important;top:-17px!important;padding:1px 9px!important;line-height:1.3!important}
"""

_WB_PENDING = {}
_WB_CACHE = {}
# spread of one player's score (a + b * projection) and a whole-team share on top (injuries, rest days, lineups)
_WB_SD = {"nfl": (2.0, 0.35, 0.10), "nba": (3.0, 0.22, 0.08)}


def _wb_cfg(sport):
    return (CONFIG.get(sport) or {}).get("weekly_board") or {}


def _wb_cached(key, ttl, fn):
    hit = _WB_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _WB_CACHE[key] = (time.time(), v)
    return v


def _wb_side(sport, points, items, left, on_now):
    """items: (projection, share of it still to come) for every starter-game still to play."""
    a, b, team = _WB_SD[sport]
    rem = sum(pj * f for pj, f in items)
    var = sum(((a + b * pj) ** 2) * f for pj, f in items) + (team * rem) ** 2
    return {"points": points, "proj": points + rem, "var": var, "left": left, "on": on_now}


def _wb_win(sa, sb):
    """Win chance (0-100) for side a: normal approximation on the projected finals."""
    d, v = sa["proj"] - sb["proj"], sa["var"] + sb["var"]
    if v <= 1e-6:
        return 100.0 if d > 0.05 else (0.0 if d < -0.05 else 50.0)
    return 100 * 0.5 * (1 + _math.erf(d / (2 * v) ** 0.5))


def _wb_people(sport):
    return _wb_cached(("people", sport), 600, lambda: _league_people(sport))


def wb_nfl_data(week, events, live=None, proj=None, people=None):
    """Everything the NFL board shows. events = ESPN games of that NFL week."""
    if live is None:
        live = league_live("nfl", week)
        if live and live.get("pairs"):
            live = espn_topup(live, events)
    if not live or not live.get("pairs"):
        return None
    proj = proj if proj is not None else _wb_cached(("nflproj", week), 1800, lambda: _nfl_proj_fetch(week))
    people = people if people is not None else _wb_people("nfl")
    players = sleeper_players("nfl")
    gofteam = {}
    for e in events:
        for t in teams_of(e):
            gofteam[sl_team(t["team"]["abbreviation"])] = e
    sides = {}
    for rid, x in live["rosters"].items():
        items, left, on, lp = [], 0, 0, []
        for p in x["starters"] or []:
            e = gofteam.get((players.get(p) or {}).get("team"))
            if e is None:                                   # bye week / no team: nothing to come
                continue
            f = 1 - game_elapsed(e)
            if f <= 0.001:
                continue
            left += 1
            if e["status"]["type"]["state"] == "in":
                on += 1
                lp.append((p, float(x["pp"].get(p) or 0)))
            items.append((max(0.0, float(proj.get(p) or 0)), f))
        sides[rid] = _wb_side("nfl", float(x["points"]), items, left, on)
        sides[rid]["lp"] = _wb_live_names("nfl", lp)
    st_ = [e["status"]["type"]["state"] for e in events]
    kicks = sorted(parse_time(e["date"]) for e in events if e["status"]["type"]["state"] == "pre")
    return {"sport": "nfl", "week": week, "pairs": live["pairs"], "sides": sides, "rosters": live["rosters"],
            "people": people, "games": len(events), "done": st_.count("post"), "live": st_.count("in"),
            "next_kick": kicks[0] if kicks else None}


def nba_lockin_value(score, games, sd_of=lambda mu: 3.0 + 0.22 * mu):
    """Sleeper NBA lock-in scoring: a starter's week score is ONE game - the game he is locked in on, or his latest
    game if never locked. With games still to come the manager keeps the score he has or plays on, so the
    expected final is the optimal-stopping value: V = max(score, C), where C (value of playing on) is worked out
    backwards over his remaining games: C_last = mean_last, C_k = E[max(X_k, C_k+1)].
    games: [(mean, sd)] in order. Returns (expected final, variance, can still change)."""
    c = None
    for mu, sd in reversed([(m, d if d is not None else sd_of(m)) for m, d in games]):
        if c is None:
            c = mu
        else:
            sd = max(sd, 0.1)
            z = (c - mu) / sd
            cdf = 0.5 * (1 + _math.erf(z / 2 ** 0.5))
            pdf = _math.exp(-z * z / 2) / (2 * _math.pi) ** 0.5
            c = c * cdf + mu * (1 - cdf) + sd * pdf
    if c is None:
        return (score or 0.0), 0.0, False
    spread = 0.8 * sum((d if d is not None else sd_of(m)) for m, d in games) / len(games)
    if score is None:
        return c, spread ** 2, True
    return max(score, c), (spread * (0.5 if score >= c else 1.0)) ** 2, True


def wb_nba_data(week, events_today=None, live=None, rows=None, people=None, day0=None):
    """Everything the NBA board shows, with the league's LOCK-IN scoring: "now" is Sleeper's own matchup score
    (each starter counts one game); the projection is each starter's lock-in value (see nba_lockin_value)."""
    if live is None:
        live = league_live("nba", week)
    if not live or not live.get("pairs"):
        return None
    rows = rows if rows is not None else _wb_cached(("nbarows", week), 1800, lambda: _nba_rows_fetch(week))
    people = people if people is not None else _wb_people("nba")
    if day0 is None:
        day0, today_evs = _wb_nba_today()
        events_today = today_evs if events_today is None else events_today
    d0 = day0
    events_today = events_today or []
    tmap = {}
    for e in events_today:
        for t in teams_of(e):
            a = t["team"]["abbreviation"]
            tmap[NBA_ESPN_TO_SLEEPER.get(a, a)] = e
    by_day = {}
    for pid, d, v in rows:
        by_day.setdefault(d, {})[pid] = v
    players = sleeper_players("nba")
    sides, days = {}, set()
    for rid, x in live["rosters"].items():
        proj = var = 0.0
        left = on = 0
        lp = []
        for p in x["starters"] or []:
            pts = float(x["pp"].get(p) or 0)
            played = pts != 0 or any(p in by_day[d] for d in by_day if d < d0)
            score = pts if played else None
            games = []
            if p in by_day.get(d0, {}):
                e = tmap.get((players.get(p) or {}).get("team"))
                f = game_frac(e, "nba") if e is not None else 0.0
                if f is not None:                           # today's game still to finish
                    mu = by_day[d0][p]
                    if f > 0:
                        on += 1
                        lp.append((p, pts))
                        games.append((pts + mu * (1 - f), (3.0 + 0.22 * mu) * (1 - f) ** 0.5))
                        score = None                         # this game's score replaces any earlier one
                    else:
                        games.append((mu, None))
                    days.add(d0)
                else:
                    score = pts                             # played today: that's his current score
            for d in sorted(by_day):
                if d > d0 and p in by_day[d] and by_day[d][p] > 0:
                    games.append((by_day[d][p], None))
                    days.add(d)
            v, vv, more = nba_lockin_value(score, games)
            proj += v
            var += vv
            left += more
        sides[rid] = {"points": float(x["points"]), "proj": max(proj, float(x["points"])), "var": var,
                      "left": left, "on": on, "lp": _wb_live_names("nba", lp)}
    st_ = [e["status"]["type"]["state"] for e in events_today]
    return {"sport": "nba", "week": week, "pairs": live["pairs"], "sides": sides, "rosters": live["rosters"],
            "people": people, "live": st_.count("in"), "days_left": len(days),
            "games": sum(s["left"] for s in sides.values())}


def _wb_state(data):
    """pre (nothing played yet) / live / mid (between games) / final (nothing left for anyone)."""
    if not any(s["left"] for s in data["sides"].values()):
        return "final"
    if data.get("live"):
        return "live"
    if not any(abs(s["points"]) > 0.01 for s in data["sides"].values()):
        return "pre"
    return "mid"


# ---- who is playing right now, on each manager's row: "● Hurts 12.4 · Lamb 8.7 +1" (as many as fit, never cut)
_WB_SUFFIX = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v"}
_WB_SUB_W = 204          # px for the sub line (the row text column is 210px at 1160px wide), measured
_WB_CHAR = {" ": .27, ".": .27, "·": .3, "●": .62, "+": .6, "'": .22, "-": .33, "i": .27, "l": .27, "j": .28, "I": .29,
            "f": .34, "t": .37, "r": .4, "m": .9, "w": .82, "M": .86, "W": .97}


def _wb_text_w(t, px=22):
    """Rough width of the board's sub-line text (Inter 700 at px), on the generous side."""
    w = 0.0
    for c in t:
        w += _WB_CHAR.get(c, .62 if c.isdigit() else (.66 if c.isupper() else .58))
    return w * px * 1.04


def _wb_live_names(sport, lp):
    """[(short name, live points)] for starters in a game right now, highest first."""
    players = sleeper_players(sport)
    out = []
    for p, pts in sorted(lp, key=lambda x: -x[1]):
        pl = players.get(p) or {}
        if sport == "nfl" and str(p).isalpha() and str(p).isupper():
            nm = f"{p} D"
        else:
            parts = [w for w in (pl.get("full_name") or pl.get("last_name") or str(p)).split() if w.lower() not in _WB_SUFFIX]
            nm = pl.get("last_name") or (parts[-1] if parts else str(p))
            nm = " ".join(w for w in nm.split() if w.lower() not in _WB_SUFFIX) or nm
            first = (pl.get("first_name") or (parts[0] if parts else ""))[:1]
            out.append([nm, round(pts, 1), first])
            continue
        out.append([nm, round(pts, 1), ""])
    seen = {}
    for x in out:
        seen[x[0]] = seen.get(x[0], 0) + 1
    return [(f"{x[2]}. {x[0]}" if seen[x[0]] > 1 and x[2] else x[0], x[1]) for x in out]


def _wb_busy(data):
    """Too many games at once to list names: NFL Sunday 18:00-21:00 UK, NBA 6+ games live."""
    if data["sport"] == "nfl":
        now = datetime.now(UK)
        return now.weekday() == 6 and 18 <= now.hour < 21
    return (data.get("live") or 0) >= 6


def _wb_live_sub(data, s, base, small_name=False):
    """Sub line with the live players (and their points) that fit; the compact '● N live' when busy or none fit."""
    import html as H
    compact = f"{base} · <span class='lv'>● {s['on']} live</span>"
    lp = s.get("lp") or []
    if not lp or _wb_busy(data):
        return compact
    room = _WB_SUB_W
    shown = []
    for i, (nm, pts) in enumerate(lp):
        rest = len(lp) - i - 1
        txt = "● " + " · ".join(f"{a} {b:.1f}" for a, b in shown + [(nm, pts)])
        if _wb_text_w(txt + (f" +{rest}" if rest else "")) > room:
            break
        shown.append((nm, pts))
    if not shown:
        return compact
    more = len(lp) - len(shown)
    body = " · ".join(f"{H.escape(a)} <b>{b:.1f}</b>" for a, b in shown)
    plain = "● " + " · ".join(f"{a} {b:.1f}" for a, b in shown) + (f" +{more}" if more else "")
    out = f"<span class='lv'>● {body}{f' +{more}' if more else ''}</span>"
    tail = re.sub(r"<[^>]+>", "", base)
    if _wb_text_w(f"{tail} · {plain}") <= room:            # room for the "N left" count too
        return f"{base} · {out}"
    return out


def wb_html(data, stamp=None):
    """The board card: 2 x 3 matchup tiles, each with points now, projected final, win chance and players left."""
    import html as H
    sport = data["sport"]
    state = _wb_state(data)
    people = data.get("people") or {}
    unit = "left"
    if sport == "nfl" and state == "pre":
        unit = "to play"
    tiles, closest = [], None
    for ra, rb in data["pairs"]:
        sa, sb = data["sides"][ra], data["sides"][rb]
        wa = _wb_win(sa, sb)
        done = not sa["left"] and not sb["left"]
        if not done and (closest is None or abs(wa - 50) < abs(closest[0] - 50)):
            closest = (wa, ra, rb)
        rows = []
        for rid, s, w in ((ra, sa, wa), (rb, sb, 100 - wa)):
            pe = people.get(rid) or {}
            name = pe.get("manager") or data["rosters"][rid]["manager"]
            av = (f"<img src='https://sleepercdn.com/avatars/thumbs/{H.escape(pe['avatar'])}'>" if pe.get("avatar")
                  else f"<div class='wav'>{H.escape(name[:1].upper())}</div>")
            if done:
                cls = "won" if w > 50 else ("lost" if w < 50 else "fav")
                pill = "WON" if w > 50 else ("LOST" if w < 50 else "TIE")
            else:
                cls = "fav" if w >= 50 else "dog"
                shown = ">99" if 99.5 <= w < 100 else ("<1" if 0 < w <= 0.5 else f"{w:.0f}")
                pill = f"{shown}<small>%</small>"
            other = sb if s is sa else sa
            if done:
                m = abs(s["points"] - other["points"])
                sub = f"<span class='dn'>{'won' if w > 50 else 'lost' if w < 50 else 'tied'}{f' by {m:.1f}' if m else ''}</span>"
            elif not s["left"]:
                sub = "<span class='dn'>all played ✓</span>"
            else:
                sub = f"<b>{s['left']}</b> {unit}"
                if s["on"]:
                    sub = _wb_live_sub(data, s, sub, len(name) > 14)
            pj = f"{s['proj']:.1f}" if sport == "nfl" or done else f"{s['proj']:.0f}"
            now_txt = "" if state == "pre" else f"<div class='wno'><i>now</i> {s['points']:.1f}</div>"
            if done:
                pj, now_txt = f"{s['points']:.1f}", ""
            rows.append(
                f"<div class='wr {cls}'>{av}<div style='min-width:0'><div class='wn{' s' if len(name) > 14 else ''}'>"
                f"{H.escape(name)}</div><div class='ws'>{sub}</div></div>"
                f"<div class='wnum'><div class='wpj'>{pj}</div>{now_txt}</div><div class='wp'>{pill}</div></div>")
        tag, cls = "", ""
        fav = sa if wa > 50 else sb
        fav_trails = (sa["points"] - sb["points"]) * (1 if wa > 50 else -1)
        under_way = fav["points"] >= 0.3 * max(fav["proj"], 1)       # no "comeback" before the favourite has played
        if done:
            tag = "<div class='wtag fn'>FINAL</div>" if state != "final" else ""
        elif 40 <= wa <= 60:
            tag, cls = f"<div class='wtag'>🔥 {'TOO CLOSE TO CALL' if state == 'pre' else 'COIN FLIP'}</div>", " close"
        elif max(wa, 100 - wa) >= 99.5:
            tag = "<div class='wtag lk'>🔒 ALL BUT OVER</div>"
        elif state != "pre" and under_way and fav_trails <= -(15 if sport == "nfl" else 60):
            tag = "<div class='wtag cb'>⚡ COMEBACK ON</div>"
        tiles.append(f"<div class='wm{cls}'>{tag}{rows[0]}{rows[1]}</div>")
    # headline + top-right status
    stamp = stamp or datetime.now(UK).strftime("%H:%M")
    if sport == "nfl":
        left_all = sum(s["left"] for s in data["sides"].values())
        if state == "final":
            h1 = f"Week {data['week']} is in the books"
        elif state == "pre":
            k = data.get("next_kick")
            h1 = f"Week {data['week']} · " + (f"kicks off {k.astimezone(UK):%A %H:%M}" if k else "projections")
        else:
            h1 = f"{data['done']} of {data['games']} games done · {left_all} to play"
    else:
        dl = data.get("days_left", 0)
        if state == "final":
            h1 = f"Week {data['week']} is in the books"
        else:
            h1 = (f"Week {data['week']} · " if state == "pre" else "") + \
                 f"{data['games']} starters to play · {dl} day{'s' if dl != 1 else ''} left"
    if state == "final":
        h2 = "Final scores · winners in gold"
    elif closest:
        wa, ra, rb = closest
        na = (people.get(ra) or {}).get("manager") or data["rosters"][ra]["manager"]
        nb = (people.get(rb) or {}).get("manager") or data["rosters"][rb]["manager"]
        h2 = f"Closest: <b>{H.escape(na)}</b> v <b>{H.escape(nb)}</b> {max(wa, 100 - wa):.0f}–{min(wa, 100 - wa):.0f}"
    else:
        h2 = ""
    if state == "live":
        pill = f"<span class='lv2'>● LIVE</span>UPDATED {stamp} · EVERY 5 MIN"
    elif state == "final":
        pill = "<span class='lv2 f'>FINAL</span>WEEK COMPLETE"
    elif state == "pre":
        pill = f"<span class='lv2 p'>PROJECTED</span>UPDATED {stamp}"
    else:
        pill = f"UPDATED {stamp}"
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    league = CONFIG[sport].get("league_name", "League").upper()
    colh = ("<div class='wch'><span></span><span class='p'>" + ("FINAL" if state == "final" else "PROJECTED")
            + "</span><span class='w'>" + ("RESULT" if state == "final" else "WIN") + "</span></div>")
    title = f"{H.escape(league)} · WEEK {data['week']} MATCHUPS" if sport == "nfl" else f"{H.escape(league)} LEAGUE"
    body = (f"<div class='top'><div class='lg'><img src='{logo}'>{title}</div>"
            f"<div class='pill'>{pill}</div></div>"
            f"<div class='wbh'><div class='h1'>{H.escape(h1)}</div><div class='h2'>{h2}</div></div>"
            f"<div class='wbg'>{colh}{colh}{''.join(tiles)}</div>")
    css = WB_CSS + "body,.card{width:1160px!important}.wbg .wch{margin-top:2px}"
    html_text = _card("--a:#ee6730;--h:#ffb347" if sport == "nba" else "--a:#1faa59;--h:#5b7bd5", body, None, css,
                      width=1160)
    return _nba_theme(html_text, None, f"WEEK {data['week']} MATCHUPS") if sport == "nba" else html_text


def _wb_sig(data):
    return json.dumps([_wb_state(data)] + [[round(s["points"], 1), round(s["proj"], 1), s["left"], s["on"]]
                                            for _, s in sorted(data["sides"].items())])


# -- which week, and when it's on the board

def _wb_nfl_events(week):
    return _wb_cached(("nflweek", week), 240,
                      lambda: fetch(f"football/nfl/scoreboard?seasontype=2&week={week}").get("events", []))


def wb_nfl_window(week, events=None):
    """(start, end) UK datetimes: 00:00 on the day of the week's first kick-off, never later than Friday 00:00
    (Thursday night) -> Tuesday 12:00 after the last game."""
    events = events if events is not None else _wb_nfl_events(week)
    if not events:
        return None
    kicks = sorted(parse_time(e["date"]).astimezone(UK) for e in events)
    first, last = kicks[0], kicks[-1]
    day = first.replace(hour=0, minute=0, second=0, microsecond=0)
    if first.weekday() in (4, 5, 6):                       # Fri/Sat/Sun first game: from Friday 00:00
        day -= timedelta(days=first.weekday() - 4)
    start = day                                            # Wed/Thu games (Thanksgiving, Christmas): that day 00:00
    end = (last + timedelta(days=(1 - last.weekday()) % 7)).replace(hour=12, minute=0, second=0, microsecond=0)
    if end <= last:
        end += timedelta(days=7)
    return start, min(end, start + timedelta(days=7, hours=12))


def wb_nba_window(now=None):
    """(start, end): Monday 12:00 UK -> next Monday 12:00 UK, for the window containing `now`."""
    now = now or datetime.now(UK)
    mon = (now - timedelta(days=now.weekday())).replace(hour=12, minute=0, second=0, microsecond=0)
    if mon > now:
        mon -= timedelta(days=7)
    return mon, mon + timedelta(days=7)


def wb_nba_week(monday):
    """Sleeper NBA week whose games fall in the Monday-Sunday starting at `monday` (UK date)."""
    sw = int(_wb_sleeper_week("nba") or 1)
    lo, hi = monday.strftime("%Y-%m-%d"), (monday + timedelta(days=6)).strftime("%Y-%m-%d")
    for w in (sw, sw + 1, sw - 1):
        if w < 1:
            continue
        ds = [d for _, d, _ in _wb_cached(("nbarows", w), 1800, lambda w=w: _nba_rows_fetch(w))]
        if any(lo <= d <= hi for d in ds):
            return w
    return sw


def _wb_sleeper_week(sport):
    return _wb_cached(("state", sport), 300, lambda: fetch_url(f"{SLEEPER}/state/{sport}").get("week"))


def _wb_nba_today():
    d0 = (datetime.now(ET) - timedelta(hours=6)).strftime("%Y-%m-%d")
    return d0, _wb_cached(("nbatoday", d0), 60,
                          lambda: fetch(f"basketball/nba/scoreboard?dates={d0.replace('-', '')}").get("events", []))


def _wb_live_now(sport, b):
    evs = _wb_nfl_events(b["week"]) if sport == "nfl" else _wb_nba_today()[1]
    return any(e["status"]["type"]["state"] == "in" for e in evs)


def _wb_current(sport, lead):
    """(key, week, start, end) of the board that should be up now (or starts within `lead` seconds), else None."""
    now = datetime.now(UK)
    if sport == "nfl":
        sw = int(_wb_sleeper_week("nfl") or 0)
        for w in (sw, sw + 1):
            win = wb_nfl_window(w) if w else None
            if win and win[0] - timedelta(seconds=lead) <= now < win[1]:
                return f"nfl-{w}", w, win[0], win[1]
        return None
    start, end = wb_nba_window(now + timedelta(seconds=lead))
    week = _wb_cached(("nbaweek", start.isoformat()), 6 * 3600, lambda: wb_nba_week(start))
    return f"nba-{start:%Y-%m-%d}", week, start, end


def _wb_wake(t):
    """Make the main loop wake up at time t (it otherwise idles for a few minutes between checks)."""
    if t > time.time() - 1 and (_NEXT_WAKE["t"] is None or t < _NEXT_WAKE["t"]):
        _NEXT_WAKE["t"] = max(t, time.time() + 1)


def _wb_hook(b):
    if b.get("where") == "alert":
        return globals().get("ALERT_HOOK")
    return NFL_HOOK if b["sport"] == "nfl" else NBA_HOOK


def _wb_routed_to_alert():
    tm = CONFIG.get("test_mode") or {}
    return bool(tm.get("enabled") and "live" in (tm.get("kinds") or []) and globals().get("ALERT_HOOK"))


def _hook_message(hook, mid):
    """Our own webhook message (dict with channel_id), or None."""
    try:
        req = urllib.request.Request(f"{hook}/messages/{mid}", headers={"User-Agent": "DiscordBot (sports-bot, 1.0)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


def _wb_adopt(st, sport, b):
    """First run after the switch: turn tonight's still-running game-night scoreboard into this week's board
    (edited in place) instead of leaving it stuck on LIVE and posting a second one."""
    old = st.get("live_boards") or {}
    cands = [(v.get("t", 0), k) for k, v in old.items()
             if k.startswith(sport + "-") and v.get("msg") and not v.get("final")
             and v.get("t", 0) >= b["start"] - 86400]
    if not cands:
        return
    k = max(cands)[1]
    hook = NFL_HOOK if sport == "nfl" else NBA_HOOK
    m = _hook_message(hook, old[k]["msg"]) if hook and not (DRY_RUN or CAPTURE is not None) else None
    if m and m.get("channel_id"):
        b.update(msg=str(old[k]["msg"]), ch=str(m["channel_id"]), where="main", posted=time.time(), adopted=k)
        _log_plain(f"{sport} weekly board: took over tonight's scoreboard message {old[k]['msg']}")
    old.pop(k, None)


def _wb_next_start(sport, b, data):
    """Unix time of the next kick-off / tip-off of this board's week (the board starts updating then), or None."""
    if sport == "nfl":
        k = data.get("next_kick")
        return int(k.timestamp()) if k else None
    now = datetime.now(ET)
    for i in range(7):
        d = (now - timedelta(hours=6) + timedelta(days=i)).strftime("%Y%m%d")
        evs = (_wb_nba_today()[1] if i == 0 else
               _wb_cached(("nbaday", d), 6 * 3600, lambda d=d: fetch(f"basketball/nba/scoreboard?dates={d}").get("events", [])))
        ts = sorted(parse_time(e["date"]).timestamp() for e in evs if e["status"]["type"]["state"] == "pre")
        ts = [t for t in ts if t < b.get("end", 9e12)]
        if ts:
            return int(ts[0])
    return None


def _wb_line(b, data, last, live_next=None):
    """One text line on the board: when it was drawn and when it will next change (Discord shows both live)."""
    state = _wb_state(data)
    lt = int(last)
    if state == "final":
        return f"✅ Final · last updated <t:{lt}:R>"
    nxt = int(live_next or last + 300) if state == "live" else _wb_next_start(b["sport"], b, data)
    if not nxt or nxt <= time.time():
        return f"🕒 Last updated <t:{lt}:R>"
    return f"🕒 Last updated <t:{lt}:R> · Next update <t:{nxt}:R> (<t:{nxt}:t>)"


def _wb_publish(st, b, pend):
    sport = b["sport"]
    if b.get("msg"):
        if CONFIG.get("paused"):
            return
        r = discord_edit(_wb_hook(b), b["msg"], pend["png"], "live.png", content=pend.get("line"))
        if r is None:                                       # someone deleted it: post a new one next time
            _log_plain(f"{sport} weekly board message is gone: posting a fresh one")
            b["msg"] = None
            b["pin"] = {"state": "none"}
        elif r:
            b.update(final=pend["final"], sig=pend["sig"], shown=time.time(), layout=pend.get("layout"),
                     line=pend.get("line"), drawn=pend["t"])
        return
    _LAST_MSG.clear()
    routed = _wb_routed_to_alert()
    if sport == "nba":
        _POSTER["name"] = (CONFIG.get("nba") or {}).get("bot_name") or "Donald 🏀"
    try:
        ok = discord(NFL_HOOK if sport == "nfl" else NBA_HOOK, [], "live", content=pend.get("line"),
                     files={"live.png": pend["png"]})
    finally:
        _POSTER.clear()
    if ok and _LAST_MSG.get("id"):
        b.update(msg=str(_LAST_MSG["id"]), ch=str(_LAST_MSG.get("channel_id") or ""), where="alert" if routed else "main",
                 posted=time.time(), final=pend["final"], sig=pend["sig"], shown=time.time(), layout=pend.get("layout"),
                 line=pend.get("line"), drawn=pend["t"])
        b["pin"] = {"state": "none"}
        _log_plain(f"{sport} weekly board posted for week {b['week']}")
    elif ok and CAPTURE is not None:
        b.update(msg="capture", posted=time.time(), final=pend["final"], sig=pend["sig"])


def wb_snapshot(sport, b):
    """Data for the board right now, or None."""
    if sport == "nfl":
        evs = _wb_nfl_events(b["week"])
        return wb_nfl_data(b["week"], evs)
    return wb_nba_data(b["week"])


def maybe_live_board(st):
    """Weekly matchups boards (NFL + NBA): post when the week opens, keep them fresh, pin/unpin on time."""
    boards = st.setdefault("weekly_boards", {})
    delay = spoiler_delay()
    lead = delay + 30
    for sport in ("nfl", "nba"):
        cfg = _wb_cfg(sport)
        if not cfg.get("enabled", True):
            continue
        try:
            cur = _wb_current(sport, lead)
            soon = _wb_current(sport, lead + 900)              # a new week opening in the next 15 minutes?
            if soon and (not cur or soon[0] != cur[0]):
                _wb_wake(soon[2].timestamp() - lead)
            if not cur:
                continue
            key, week, start, end = cur
            b = boards.get(key)
            if b is None:
                b = {"sport": sport, "week": week, "start": start.timestamp(), "end": end.timestamp(), "msg": None,
                     "t": 0, "pin": {"state": "none"}}
                if time.time() >= b["start"]:
                    _wb_adopt(st, sport, b)
                boards[key] = b
            now = time.time()
            pend = _WB_PENDING.get(key)
            if pend:
                if now >= pend["show"]:
                    _WB_PENDING.pop(key, None)
                    _wb_publish(st, b, pend)
                else:
                    if _NEXT_WAKE["t"] is None or pend["show"] < _NEXT_WAKE["t"]:
                        _NEXT_WAKE["t"] = pend["show"]
                continue
            relayout = (bool(b.get("msg")) and b.get("layout") != _code_version()   # new code: redraw once
                        and now - b.get("t", 0) >= 120)
            if (b.get("final") and b.get("msg") and not relayout) or CONFIG.get("paused"):
                continue
            if not b.get("msg") and now - b.get("t", 0) < 60:
                continue                                    # last post attempt failed: try again in a minute
            # when: every 5 min while games are live, once more when they end, never in between
            # (plus the new board when the week opens and one redraw after a code/layout update)
            live_now = _wb_live_now(sport, b)
            idle = int(cfg.get("idle_refresh_min", 0) or 0) * 60
            just_ended = b.get("was_live") and not live_now
            if b.get("msg") and not just_ended and not relayout:
                if live_now:
                    if now < b.get("t", 0) + 300:
                        continue
                elif not idle or now < b.get("t", 0) + idle:
                    continue
            if now < b["start"] - lead:
                w = b["start"] - lead
                if _NEXT_WAKE["t"] is None or w < _NEXT_WAKE["t"]:
                    _NEXT_WAKE["t"] = w
                continue
            data = wb_snapshot(sport, b)
            b["t"] = now
            if not data:
                continue
            state = _wb_state(data)
            ended = b.get("was_live") and state != "live"
            b["was_live"] = state == "live"
            sig = _wb_sig(data)
            if b.get("msg") and sig == b.get("sig") and not ended and not relayout:
                # nothing moved: keep the image; while live, only the "next update" time moves on (text-only edit)
                line = _wb_line(b, data, b.get("drawn") or b.get("shown") or now, now + 300)
                if state == "live" and line != b.get("line") and not CONFIG.get("paused"):
                    if discord_edit(_wb_hook(b), b["msg"], None, "live.png", content=line):
                        b["line"] = line
                continue
            png = render_card(wb_html(data))
            if png:
                _WB_PENDING[key] = {"png": png, "t": now, "show": max(now + delay, b["start"]),
                                    "final": state == "final", "sig": sig, "layout": _code_version(),
                                    "line": _wb_line(b, data, now, now + 300)}
                w = _WB_PENDING[key]["show"]
                if _NEXT_WAKE["t"] is None or w < _NEXT_WAKE["t"]:
                    _NEXT_WAKE["t"] = w
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"{sport} weekly board skipped: {ex}")
    for k in sorted(boards, key=lambda k: boards[k].get("start", 0))[:-6]:
        if (boards[k].get("pin") or {}).get("state") != "pinned":
            boards.pop(k, None)
    try:
        wb_pins(st)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"weekly board pins skipped: {ex}")


# -- pins: catch-up every loop, so a restart never double-pins or misses an unpin

def _pinned_ids(ch):
    """Message ids pinned in the channel right now, or None if Discord can't be asked."""
    code, js = discord_api("GET", f"/channels/{ch}/messages/pins?limit=50")
    if code == 200 and isinstance(js, dict):
        return {str((i.get("message") or {}).get("id")) for i in js.get("items") or []}
    code, js = discord_api("GET", f"/channels/{ch}/pins")
    if code == 200 and isinstance(js, list):
        return {str(m.get("id")) for m in js}
    return None


def _wb_drop_notice(ch, mid, tries=3):
    """Delete Discord's "<bot> pinned a message" line (message type 6 pointing at our message).
    True when deleted, False when not found yet, None when not allowed."""
    for i in range(tries):
        code, msgs = discord_api("GET", f"/channels/{ch}/messages?limit=25")
        for m in msgs or []:
            if m.get("type") == 6 and str((m.get("message_reference") or {}).get("message_id")) == str(mid):
                c, _ = discord_api("DELETE", f"/channels/{ch}/messages/{m['id']}")
                if c in (200, 204, 404):
                    return True
                return None if c == 403 else False
        if code in (401, 403):
            return None
        time.sleep(1.5 * (i + 1))
    return False


def _wb_fail(b, what, code):
    p = b["pin"]
    p["tries"] = p.get("tries", 0) + 1
    p["next"] = time.time() + min(1800, 60 * 2 ** min(p["tries"], 5))
    why = {401: "the bot token is wrong or was reset", 403: "the bot is missing the Pin Messages / Manage Messages "
           "permission in the channel", 404: "Discord can't find the message", 0: "Discord couldn't be reached"}.get(
        code, f"Discord said {code}")
    _log_plain(f"weekly board {what} failed ({code}) for {b['sport']} week {b['week']}, retrying: {why}")
    if p["tries"] in (3, 10):
        alert(f"📌 Couldn't {what} the {b['sport'].upper()} week {b['week']} matchups board: {why}",
              f"wbpin-{b['sport']}-{what}", 6)


def wb_pins(st):
    """Pin the board that should be pinned now, unpin the ones whose time is up, and remove Discord's pin notice."""
    boards = st.get("weekly_boards") or {}
    if not BOT_TOKEN or DRY_RUN or CAPTURE is not None or SILENT:
        if not BOT_TOKEN and any(b.get("msg") for b in boards.values()) and not (DRY_RUN or CAPTURE is not None) \
                and any(_wb_cfg(s).get("pin", True) for s in ("nfl", "nba")) \
                and time.time() - st.get("wb_token_alert", 0) > 7 * 86400:
            st["wb_token_alert"] = time.time()             # once a week, also across restarts
            alert("📌 Weekly matchups boards are posting but can't be pinned yet: no Discord bot token "
                  "(DISCORD_BOT_TOKEN) on the server", "wb-notoken", 24 * 7)
        return
    now = time.time()
    newest = {}
    for k, b in boards.items():
        if b.get("msg") and b["msg"] != "capture" and b.get("ch"):
            if b["sport"] not in newest or b["start"] > boards[newest[b["sport"]]]["start"]:
                newest[b["sport"]] = k
    # unpins first, so a Monday NBA swap never has two boards pinned at once
    for k in sorted(boards, key=lambda k: (boards[k].get("start", 0))):
        b = boards[k]
        if not b.get("msg") or b["msg"] == "capture" or not b.get("ch"):
            continue
        p = b.setdefault("pin", {"state": "none"})
        want = (_wb_cfg(b["sport"]).get("pin", True) and b["start"] - 120 <= now < b["end"]
                and newest.get(b["sport"]) == k)
        if p["state"] == "pinned" and now < b["end"] < now + 900:
            _wb_wake(b["end"])                              # unpin on the minute, not at the next idle check
        if now < p.get("next", 0):
            continue
        if want and p["state"] != "pinned":
            ids = _pinned_ids(b["ch"])
            if ids is not None and b["msg"] in ids:
                code = 204                                  # already pinned (e.g. before a restart): don't pin twice
            else:
                code = _pin(b["ch"], b["msg"], True)
            if code in (200, 204):
                p.update(state="pinned", at=now, tries=0, next=0, checked=now)
                _log_plain(f"pinned {b['sport']} week {b['week']} matchups board")
                if ids is None or b["msg"] not in ids:
                    p["notice"] = _wb_drop_notice(b["ch"], b["msg"])
                else:
                    p["notice"] = True
            else:
                _wb_fail(b, "pin", code)
        elif not want and p["state"] == "pinned":
            code = _pin(b["ch"], b["msg"], False)
            if code in (200, 204, 404):
                p.update(state="unpinned", at=now, tries=0, next=0)
                _log_plain(f"unpinned {b['sport']} week {b['week']} matchups board")
            else:
                _wb_fail(b, "unpin", code)
        elif not want and p["state"] == "none" and not p.get("end_checked"):
            ids = _pinned_ids(b["ch"])                     # crash between pin and save? make sure it's not left up
            if ids is not None:
                p["end_checked"] = True
                if b["msg"] in ids and _pin(b["ch"], b["msg"], False) in (200, 204, 404):
                    _log_plain(f"unpinned {b['sport']} week {b['week']} matchups board (left over)")
        elif want and p["state"] == "pinned":
            if p.get("notice") is False and p.get("notice_tries", 0) < 5:
                p["notice_tries"] = p.get("notice_tries", 0) + 1
                p["notice"] = _wb_drop_notice(b["ch"], b["msg"], tries=1)
            if now - p.get("checked", 0) > 3600:           # hourly: is it really still pinned?
                ids = _pinned_ids(b["ch"])
                p["checked"] = now
                if ids is not None and b["msg"] not in ids:
                    p["state"] = "none"
                    _log_plain(f"{b['sport']} week {b['week']} board was not pinned any more: pinning it again")


# ---- 4) touchdowns taken away on review (or re-credited) get corrected in place
_TD_HTML = {}
_last_td_html = {"h": None}
_nfl_td_html_track = nfl_td_fantasy_html


def nfl_td_fantasy_html(event, play, summary, live, ol, base, *a, **k):
    h = _nfl_td_html_track(event, play, summary, live, ol, base, *a, **k)
    _last_td_html["h"] = h
    return h


_post_td_track = post_td_fantasy


def post_td_fantasy(event, play, summary, ctx):
    _LAST_MSG.clear()
    _last_td_html["h"] = None
    ok = _post_td_track(event, play, summary, ctx)
    if ok and _LAST_MSG.get("id") and _NFL_ST.get("st") is not None:
        st = _NFL_ST["st"]
        st.setdefault("td_posts", {})[str(play["id"])] = {
            "game": event["id"], "msg": _LAST_MSG["id"], "t": time.time(), "scorer": td_parse(play).get("scorer", ""),
            "team": (play.get("team") or {}).get("abbreviation", ""),
            "file": next((f for _, f in _LAST_MSG.get("att") or [] if f), "td.png")}
        if _last_td_html["h"]:
            _TD_HTML[str(play["id"])] = _last_td_html["h"]
    return ok


OVERTURN_HTML = ("<div style='position:fixed;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;z-index:9'>"
                 "<div style='transform:rotate(-10deg);border:8px solid #ff3d3d;color:#ff3d3d;font:900 64px Inter,Arial;letter-spacing:6px;"
                 "padding:6px 28px;border-radius:16px;background:rgba(18,4,8,.72);box-shadow:0 0 40px rgba(255,61,61,.6);text-align:center'>"
                 "❌ OVERTURNED<div style='font-size:20px;letter-spacing:2px;color:#ffd0d0;margin-top:4px'>REVIEW: NO TOUCHDOWN · POINTS REMOVED</div>"
                 "</div></div>")
_OVR = {"t": 0.0}


def check_overturns(st):
    """Every minute: a touchdown we posted that has vanished (or changed scorer) gets its post corrected."""
    if time.time() - _OVR["t"] < 60:
        return
    _OVR["t"] = time.time()
    posts = st.get("td_posts") or {}
    if not posts or not NFL_HOOK:
        return
    by_game = {}
    for pid, r in list(posts.items()):
        if time.time() - r.get("t", 0) > 3 * 3600 or r.get("done"):
            if time.time() - r.get("t", 0) > 24 * 3600:
                posts.pop(pid, None)
            continue
        by_game.setdefault(r["game"], []).append(pid)
    for gid, pids in by_game.items():
        try:
            summ = fetch(f"football/nfl/summary?event={gid}")
        except Exception:  # noqa: BLE001
            continue
        plays = {str(p.get("id")): p for p in summ.get("scoringPlays") or []}
        if not plays:
            continue                                         # feed hiccup: never treat an empty list as "overturned"
        for pid in pids:
            r = posts[pid]
            if pid not in plays:
                same = [q for q in plays.values() if td_parse(q).get("scorer") == r.get("scorer")
                        and (q.get("team") or {}).get("abbreviation") == r.get("team")]
                if same:                                     # ESPN renumbered the play: it still stands
                    continue
                html_text = _TD_HTML.get(pid)
                png = None
                if html_text:
                    html_text = html_text.replace("<div class='card'", "<div class='card' style='filter:grayscale(.75) brightness(.8)'", 1)
                    png = render_card(html_text.replace("</body>", OVERTURN_HTML + "</body>"))
                ok = discord_edit(NFL_HOOK, r["msg"], png, "td.png" if png else (r.get("file") or "td.png"),
                                  content=None if png else "❌ **OVERTURNED** · this touchdown was taken away on review")
                if ok is not False:
                    r["done"] = "overturned"
                    _log_plain(f"touchdown {pid} overturned: post corrected")
            else:
                now_scorer = td_parse(plays[pid]).get("scorer", "")
                if now_scorer and r.get("scorer") and now_scorer != r["scorer"]:
                    ok = discord_edit(NFL_HOOK, r["msg"], None, r.get("file") or "td.png",
                                      content=f"✏️ **CORRECTED** · this touchdown is now credited to **{now_scorer}** (not {r['scorer']})")
                    if ok is not False:
                        r["scorer"] = now_scorer
                        _log_plain(f"touchdown {pid} re-credited to {now_scorer}")


# ---- 5) the try after a touchdown (extra point / 2-pt): accurate score on the card, edited in when it happens
#   ESPN adds the try to the touchdown's own scoring play (text "(Jake Bates Kick)", score +1) once it's kicked.
#   Spoiler rule: the try only appears on a card once its own moment is spoiler_delay() old. Its moment is not in
#   the feed, so we use a time that is never earlier than it: when we first saw it, or the next play's wallclock.
#   A touchdown posted before that shows the score after the touchdown only ("+6 · conversion to come"); the
#   same Discord message is then edited (image only, no text) to the full score with the result of the try.
import copy as _copy

TRY_CSS = """
.sbx .tryt{display:inline-block;margin:6px 0 0 6px;border-radius:8px;padding:3px 9px;font-size:13px;font-weight:900;
 letter-spacing:1px;vertical-align:top;white-space:nowrap}
.sbx .tryt.ok{background:#1faa59;color:#fff}.sbx .tryt.no{background:#5b6273;color:#fff}
.sbx .tryt.wait{background:rgba(255,255,255,.12);color:#e3e7ef}
"""
TRY_GIVE_UP = 600                                          # no try after 10 minutes: stop waiting
_TRY_TAG = {"tag": None}
_TEAM_SIDE = {}                                            # team abbreviation -> "away" / "home" (current games)


def _note_sides(event):
    for t in teams_of(event):
        _TEAM_SIDE[t["team"]["abbreviation"]] = t.get("homeAway") or ""


def _td_prev_score(play, summary):
    """(away, home) just before this scoring play, from ESPN's list of scoring plays."""
    sps = summary.get("scoringPlays") or []
    ids = [str(p.get("id")) for p in sps]
    i = ids.index(str(play.get("id"))) if str(play.get("id")) in ids else len(sps)
    if i == 0:
        return 0, 0
    p = sps[i - 1]
    return int(num(p.get("awayScore"))), int(num(p.get("homeScore")))


def _td_drive_play(summary, pid):
    """(the touchdown's play in the drive chart, wallclock of the first play after it or None)."""
    drives = summary.get("drives") or {}
    pool = [p for d in list(drives.get("previous") or []) + ([drives["current"]] if drives.get("current") else [])
            for p in d.get("plays") or []]
    seen, uniq = set(), []
    for p in pool:                                  # ESPN can list the drive in progress twice (previous + current)
        if str(p.get("id")) not in seen:
            seen.add(str(p.get("id")))
            uniq.append(p)
    for i, p in enumerate(uniq):
        if str(p.get("id")) == str(pid):
            mine = p.get("wallclock") or ""
            nxt = next((q.get("wallclock") for q in uniq[i + 1:] if q.get("wallclock") and q["wallclock"] > mine), None)
            return p, nxt
    return None, None


def _try_gain(play, summary):
    """(points the scoring team got on top of the 6, points the other team got) according to ESPN's score,
    or (None, None) when the earlier scores can't explain this play (e.g. an incomplete feed)."""
    a0, h0 = _td_prev_score(play, summary)
    away = teams_of_play_side(play, summary)
    a1, h1 = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    mine = (a1 - a0 if away else h1 - h0) - 6
    theirs = (h1 - h0) if away else (a1 - a0)
    if not (0 <= mine <= 2 and theirs in (0, 2)):
        return None, None
    return mine, theirs


def td_try(play, summary):
    """Result of the try after this touchdown, or None while it isn't in yet (or never comes).
    dict(label, cls ok/no). The score is the judge: a "good" try only counts once ESPN's score shows it."""
    m = re.search(r"\(([^()]*)\)\s*$", play.get("text") or "")
    pat = (m.group(1) if m else "").lower()
    dp, _ = _td_drive_play(summary, play.get("id"))
    pa = (dp or {}).get("pointAfterAttempt") or {}
    pa_id = pa.get("id") if pa.get("id") not in (0, None) else None
    mine, theirs = _try_gain(play, summary)
    known = mine is not None
    if known and theirs == 2:
        return {"label": "✗ DEFENSIVE 2-PT RETURN", "cls": "no"}
    if not pat and pa_id is None and (not known or mine == 0):
        return None                                        # nothing yet
    two = bool(re.search(r"two-point|2pt|2-pt", pat)) or pa_id in (15, 16) or mine == 2
    failed = bool(re.search(r"fail|miss|block|no good", pat)) or pa_id in (62, 43) \
        or (pa_id in (15, 16) and not pa.get("value"))
    if known and not failed and mine < (2 if two else 1):
        return None                                        # feed says good but the score isn't there yet
    if two:
        return {"label": "✓ 2-PT GOOD", "cls": "ok"} if not failed else {"label": "✗ 2-PT FAILED", "cls": "no"}
    if not failed:
        return {"label": "✓ EXTRA POINT GOOD", "cls": "ok"}
    return {"label": "✗ EXTRA POINT BLOCKED" if ("block" in pat or pa_id == 43) else "✗ EXTRA POINT MISSED",
            "cls": "no"}


def teams_of_play_side(play, summary):
    """True when the scoring team is the away team."""
    comps = ((summary.get("header") or {}).get("competitions") or [{}])[0].get("competitors") or []
    abbr = (play.get("team") or {}).get("abbreviation")
    for c in comps:
        if (c.get("team") or {}).get("abbreviation") == abbr and c.get("homeAway"):
            return c.get("homeAway") == "away"
    if abbr in _TEAM_SIDE:                                 # header can't tell: from the scoreboard
        return _TEAM_SIDE[abbr] == "away"
    a0, h0 = _td_prev_score(play, summary)
    return int(num(play.get("awayScore"))) - a0 >= 6


def td_only_play(play, summary):
    """The scoring play as it stood the moment the touchdown was scored: no try, score +6 only."""
    p = _copy.deepcopy(play)
    p["text"] = re.sub(r"\s*\([^()]*\)\s*$", "", p.get("text") or "")
    mine, theirs = _try_gain(play, summary)
    if mine is None:                                       # earlier scores unknown: go by the try itself
        res = td_try(play, summary) or {}
        mine, theirs = (2 if "2-PT GOOD" in res.get("label", "") else 1 if "POINT GOOD" in res.get("label", "")
                        else 0), (2 if "DEFENSIVE" in res.get("label", "") else 0)
    a1, h1 = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    if teams_of_play_side(play, summary):
        p["awayScore"], p["homeScore"] = a1 - mine, h1 - theirs
    else:
        p["awayScore"], p["homeScore"] = a1 - theirs, h1 - mine
    return p


def _try_moment(st, play, summary):
    """A time never earlier than the try: first time we saw it, or the wallclock of the next play if earlier."""
    seen = st.setdefault("td_try_seen", {})
    pid = str(play.get("id"))
    seen.setdefault(pid, time.time())
    for k in list(seen):
        if time.time() - seen[k] > 86400:
            seen.pop(k, None)
    t0 = seen[pid]
    dp, nxt = _td_drive_play(summary, pid)
    try:
        if nxt:
            t0 = min(t0, _wallclock_ts(nxt))
        if (dp or {}).get("wallclock"):
            t0 = max(t0, _wallclock_ts(dp["wallclock"]))     # never before the touchdown itself
    except Exception:  # noqa: BLE001
        pass
    return t0


_nfl_td_html_try = nfl_td_fantasy_html


def nfl_td_fantasy_html(event, play, summary, live, ol, base, *a, **k):
    """Adds the try result (or "conversion to come") next to the +N chip on the scoreboard."""
    h = _nfl_td_html_try(event, play, summary, live, ol, base, *a, **k)
    tag = _TRY_TAG.get("tag")
    if tag and "<div class='plus'>" in h:
        i = h.find("</div>", h.find("<div class='plus'>")) + len("</div>")
        h = h[:i] + f"<div class='tryt {tag['cls']}'>{tag['label']}</div>" + h[i:]
        h = h.replace("</style>", TRY_CSS + "</style>", 1)
        _last_td_html["h"] = h
    return h


_post_td_try = post_td_fantasy


def post_td_fantasy(event, play, summary, ctx):
    """Post the touchdown with the try only if it's old enough to show; otherwise remember to edit it in."""
    _note_sides(event)
    st = _NFL_ST.get("st")
    res = None
    try:
        res = td_try(play, summary)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"try check skipped: {ex}")
    send_at = max(_SEND_AT.get("t") or 0, time.time())    # a late post goes out now
    show = res is not None and st is not None and _try_moment(st, play, summary) + spoiler_delay() <= send_at + 1
    if res is not None and st is None:
        show = True
    live_game = event["status"]["type"].get("state") == "in"
    if show:
        _TRY_TAG["tag"] = res
        p = play
    else:
        p = td_only_play(play, summary)
        _TRY_TAG["tag"] = {"label": "⏳ CONVERSION TO COME", "cls": "wait"} if live_game else None
    try:
        ok = _post_td_try(event, p, summary, ctx)
    finally:
        _TRY_TAG["tag"] = None
    if ok and st is not None:
        r = (st.get("td_posts") or {}).get(str(play["id"]))
        if r is not None:
            r["try"] = "shown" if show else "pending"
    return ok


def _rebuild_td_image(event, play, summary, ctx, tag):
    """Build the touchdown card again exactly as a post would (still or animated), without posting.
    Returns (bytes, filename, html) or (None, None, None)."""
    global CAPTURE
    keep_cap, keep_send = CAPTURE, dict(_SEND_AT)
    CAPTURE = []
    _TRY_TAG["tag"] = tag
    _last_td_html["h"] = None
    try:
        _post_td_track(event, play, summary, ctx)
        got = CAPTURE[-1] if CAPTURE else None
    finally:
        CAPTURE = keep_cap
        _TRY_TAG["tag"] = None
        _SEND_AT.update(keep_send)
    if not isinstance(got, dict) or not got.get("attachments"):
        return None, None, None
    path = Path(got["attachments"][0].replace("file://", ""))
    return path.read_bytes(), path.name, _last_td_html["h"]


def td_try_tick(st, slot, getctx):
    """Edit posted touchdowns once their try is in (and old enough to show). True while one is still waiting."""
    posts = st.get("td_posts") or {}
    waiting = False
    for e in slot:
        pend = [(pid, r) for pid, r in posts.items()
                if r.get("game") == e["id"] and r.get("try") == "pending" and not r.get("done")]
        if not pend:
            continue
        summary = fetch(f"football/nfl/summary?event={e['id']}")
        plays = {str(p.get("id")): p for p in summary.get("scoringPlays") or []}
        over = e["status"]["type"]["state"] == "post"
        for pid, r in pend:
            p = plays.get(pid)
            age = time.time() - r.get("t", time.time())
            res = td_try(p, summary) if p else None
            if res is None:
                if age > TRY_GIVE_UP or (over and age > 300) or (p is None and age > 300):
                    r["try"] = "gave_up"                   # no try (walk-off, game over, feed never had it)
                    _log_plain(f"touchdown {pid}: no try after {age / 60:.0f} min")
                    ctx = getctx() if p else None
                    if ctx:                                # take "conversion to come" off the card (nothing new shown)
                        img, name, _ = _rebuild_td_image(e, td_only_play(p, summary), summary, ctx, None)
                        if img and not r.get("done"):
                            if discord_edit(NFL_HOOK, r["msg"], img, name):
                                _gif_after_edit(NFL_HOOK, r["msg"])
                else:
                    waiting = True
                continue
            target = _try_moment(st, p, summary) + spoiler_delay()
            if time.time() < target - PREP_SECONDS:
                waiting = True
                w = target - PREP_SECONDS
                if _NEXT_WAKE["t"] is None or w < _NEXT_WAKE["t"]:
                    _NEXT_WAKE["t"] = w
                continue
            ctx = getctx()
            if not ctx:
                waiting = True
                continue
            img, name, html_text = _rebuild_td_image(e, p, summary, ctx, res)
            if not img:
                r["try_fail"] = r.get("try_fail", 0) + 1
                if r["try_fail"] >= 3:
                    r["try"] = "gave_up"
                continue
            if r.get("done"):                              # overturned while we were building
                continue
            _SEND_AT.update(t=target, t0=target - spoiler_delay(), label=f"try {res['label']}")
            _wait_for_send_slot()
            ok = discord_edit(NFL_HOOK, r["msg"], img, name)
            _record_send(ok is True)
            if ok:
                _gif_after_edit(NFL_HOOK, r["msg"])
            if ok is not False:
                r["try"] = "done" if ok else "gone"
                r["try_result"] = res["label"]
                if html_text:
                    _TD_HTML[pid] = html_text
                _log_plain(f"touchdown {pid}: post updated with the try ({res['label']})")
    return waiting


_td_ready_try = td_ready


def td_ready(summary, play, first_seen):
    """Also note the moment we first see the try of a touchdown that isn't posted yet (for the spoiler rule)."""
    st = _NFL_ST.get("st")
    if st is not None and (play.get("type") or {}).get("abbreviation") == "TD":
        try:
            if td_try(play, summary) is not None:
                _try_moment(st, play, summary)
        except Exception:  # noqa: BLE001
            pass
    return _td_ready_try(summary, play, first_seen)


_nfl_prime_slot_try = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    for e in slot:
        _note_sides(e)
    active = _nfl_prime_slot_try(slot, st, getctx, force)
    try:
        if td_try_tick(st, slot, getctx):
            active = True
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"try edit skipped: {ex}")
    return active


# ---- 6) daily report in #rick-only (08:00 UK)
def daily_report_text(st):
    now = time.time()
    day = [p for p in (st.get("post_log") or []) if now - p["t"] < 24 * 3600 and p.get("where") != "alert"]
    nfl = sum(1 for p in day if p["where"] == "nfl")
    nba = sum(1 for p in day if p["where"] == "nba")
    trades = sum(1 for p in day if p["kind"] == "trade")
    tim = [x for x in (st.get("post_timing") or []) if now - x.get("sent", 0) < 24 * 3600]
    target = spoiler_delay()
    if tim:
        avg = sum(x["after"] for x in tim) / len(tim)
        late = sum(1 for x in tim if x["after"] > target + 5)
        slow = max(x["after"] for x in tim)
        timing = f"⏱️ In-game timing: average **{avg:.1f}s** after the play · slowest {slow:.1f}s · {late} late (target {target:.0f}s)"
    else:
        timing = "⏱️ In-game timing: no in-game posts in the last 24h"
    builds = [b for b in (st.get("anim_builds") or []) if now - b[0] < 24 * 3600]
    anim = (f"🎞️ Animated cards: {len(builds)} · average build {sum(b[1] for b in builds) / len(builds):.0f}s"
            if builds else "🎞️ Animated cards: none")
    errs = [a for a in (st.get("alert_log") or []) if now - a < 24 * 3600]
    held = sum(_HELD.values())
    starts = [datetime.fromtimestamp(t, UK).strftime("%H:%M") for t in (st.get("run_starts") or []) if now - t < 24 * 3600]
    nxt = []
    try:
        for e in fetch("football/nfl/scoreboard").get("events", []):
            t = parse_time(e["date"])
            if e["status"]["type"]["state"] == "pre" and (t - now_utc()).total_seconds() < 36 * 3600:
                away, home = teams_of(e)
                nxt.append(f"{away['team']['abbreviation']} @ {home['team']['abbreviation']} {t.astimezone(UK):%a %H:%M}")
        nba_n = len([e for e in fetch("basketball/nba/scoreboard").get("events", []) if e["status"]["type"]["state"] == "pre"])
        if nba_n:
            nxt.append(f"NBA {nba_n} game{'s' if nba_n != 1 else ''} tonight")
    except Exception:  # noqa: BLE001
        pass
    lines = [f"📋 **Daily bot report** · {datetime.now(UK):%a %-d %b}",
             f"✅ **{len(day)} post{'s' if len(day) != 1 else ''}** · NFL {nfl} · NBA {nba} · trades {trades}",
             timing, anim,
             f"🛑 Alerts: {len(errs) if errs else 'none'} · held back: {held if held else 'none'}"
             + (" · ⏸️ PAUSED" if CONFIG.get("paused") else ""),
             f"🔁 Restarts: {' · '.join(starts) if starts else 'none'}",
             f"📅 Next: {' · '.join(nxt[:4]) if nxt else 'nothing scheduled in the next 36h'}"]
    return "\n".join(lines)


def maybe_daily_report(st):
    now = datetime.now(UK)
    if not ALERT_HOOK or now.hour < int((CONFIG.get("daily_report") or {}).get("hour_uk", 8)):
        return
    key = f"{now:%Y-%m-%d}"
    if st.get("daily_report") == key:
        return
    st["daily_report"] = key
    _POSTER["name"] = "Donald 🩺"
    _IN_ALERT["on"] = True
    try:
        discord(ALERT_HOOK, [], "report", content=daily_report_text(st)[:1900])
    finally:
        _POSTER.clear()
        _IN_ALERT["on"] = False


# ---- bookkeeping the report needs
_render_anim_timed = render_anim


def render_anim(html_text, width=None):
    t0 = time.time()
    gif = _render_anim_timed(html_text, width)
    if gif:
        _ANIM_BUILDS.append([round(time.time()), round(time.time() - t0, 1)])
        del _ANIM_BUILDS[:-100]
    return gif


_ANIM_BUILDS = []
_RUN_START = {"t": time.time(), "saved": False}
_alert_plain = alert


def alert(msg, key=None, every_h=3.0):
    _ALERT_TIMES.append(round(time.time()))
    del _ALERT_TIMES[:-200]
    return _alert_plain(msg, key, every_h)


_ALERT_TIMES = []
_save_state_qol = save_state


def save_state(st):
    st["post_log"] = (st.get("post_log") or [])
    known = {(p["t"], p["kind"]) for p in st["post_log"]}
    st["post_log"] = (st["post_log"] + [p for p in _POSTLOG if (p["t"], p["kind"]) not in known])[-600:]
    st["anim_builds"] = ((st.get("anim_builds") or []) + [b for b in _ANIM_BUILDS if b not in (st.get("anim_builds") or [])])[-100:]
    st["alert_log"] = sorted(set((st.get("alert_log") or []) + _ALERT_TIMES))[-200:]
    if not _RUN_START["saved"] and os.environ.get("KEEPALIVE") == "1":
        st["run_starts"] = ((st.get("run_starts") or []) + [round(_RUN_START["t"])])[-20:]
        _RUN_START["saved"] = True
    return _save_state_qol(st)


# ---- wire it all into the main loop
_run_nfl_qol = run_nfl


def run_nfl(st, date=None, force=False):
    active = _run_nfl_qol(st, date, force)
    if date is None and not SILENT:
        for fn in (refresh_switches, lambda: check_overturns(st), lambda: maybe_live_board(st)):
            try:
                fn()
            except Exception as ex:  # noqa: BLE001
                _log_plain(f"quality-of-life task skipped: {ex}")
    return active


_maybe_daily_race_qol = maybe_daily_race


def maybe_daily_race(st):
    for fn in (maybe_daily_report, maybe_catchup):
        try:
            fn(st)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"{fn.__name__} skipped: {ex}")
    return _maybe_daily_race_qol(st)



# ------------------------------------------------ extras: power rankings, waiver wire, trade check-ups, league records

def power_rankings(sport, upto):
    """Order of teams by all-play record (55%), points for (25%) and last-3-weeks form (20%)."""
    scores, rec = season_history(sport, upto)
    if not scores:
        return [], {}, {}
    weeks = max(len(v) for v in scores.values())
    ap = {r: [0, 0] for r in scores}
    for w in range(weeks):
        wk = {r: v[w] for r, v in scores.items() if w < len(v) and v[w] > 0}
        for r, p in wk.items():
            ap[r][0] += sum(1 for q in wk.values() if q < p)
            ap[r][1] += sum(1 for q in wk.values() if q > p)
    pf = {r: rec[r]["pf"] for r in rec}
    form = {r: sum(scores[r][-3:]) / max(1, len(scores[r][-3:])) for r in scores}

    def nrm(d, r):
        lo, hi = min(d.values()), max(d.values())
        return (d[r] - lo) / ((hi - lo) or 1)
    score = {r: 0.55 * (ap[r][0] / max(1, sum(ap[r]))) + 0.25 * nrm(pf, r) + 0.20 * nrm(form, r) for r in rec if r in ap}
    return sorted(score, key=lambda r: -score[r]), ap, form


PWR_CSS = """
.pwl{display:flex;flex-direction:column;gap:2px}
.pwl div{display:grid;grid-template-columns:26px 40px 1fr 64px;gap:6px;font-size:15px;align-items:center}
.pwl b.rk{color:#ffd166;font-weight:900}.pwl .up{color:#7ee2a8;font-weight:900}.pwl .dn{color:#ff9a9e;font-weight:900}.pwl .z{color:#6f7788}
.pwl .m{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.pwl .ap{color:#aab3c5;text-align:right;font-variant-numeric:tabular-nums}
"""

_recap_wide_plain = recap_wide_html


def recap_wide_html(sport, week, live, settings):
    a, b = _recap_wide_plain(sport, week, live, settings)
    import html as H
    names = {rid: x["manager"] for rid, x in live["rosters"].items()}
    # power rankings box in the title-race sidebar
    try:
        now_o, ap, _ = power_rankings(sport, week)
        prev_o, _, _ = power_rankings(sport, week - 1) if week > 1 else (now_o, None, None)
        rows = []
        for i, r in enumerate(now_o, 1):
            mv = (prev_o.index(r) + 1 - i) if r in prev_o else 0
            arrow = f"<span class='up'>▲{mv}</span>" if mv > 0 else (f"<span class='dn'>▼{-mv}</span>" if mv < 0 else "<span class='z'>—</span>")
            rows.append(f"<div><b class='rk'>{i}</b>{arrow}<span class='m'>{H.escape(names.get(r, '?'))}</span>"
                        f"<span class='ap'>{ap[r][0]}-{ap[r][1]}</span></div>")
        box = (f"<div class='col'><div class='ch'>POWER RANKINGS · ALL-PLAY RECORD</div><div class='pwl'>{''.join(rows)}</div></div>")
        b = b.replace("<div class='col'><div class='ch'>THE RACE</div>", box + "<div class='col'><div class='ch'>THE RACE</div>", 1)
        b = b.replace("</style>", PWR_CSS + "</style>", 1)
    except Exception as e:  # noqa: BLE001
        _log_plain(f"power rankings skipped: {e}")
    # league record: best team week ever (this league's history in Sleeper)
    try:
        scores, _ = season_history(sport, week - 1) if week > 1 else ({}, {})
        prev_best = max((p for v in scores.values() for p in v), default=0)
        top = max((x for x in live["rosters"].values() if x.get("matchup_id")), key=lambda x: x["points"])
        if prev_best and top["points"] > prev_best:
            stamp = (f"<div class='h2' style='margin-top:6px'><span style='background:linear-gradient(90deg,#6d28d9,#a855f7,#d4af37);"
                     f"color:#fff;font-weight:900;border-radius:999px;padding:2px 10px'>🏛️ NEW LEAGUE RECORD</span> "
                     f"<b>{H.escape(top['manager'])}</b> {top['points']:.1f} is the best week in league history (old best {prev_best:.1f})</div>")
            a = a.replace("</div></div><div class='rw'>", stamp + "</div></div><div class='rw'>", 1) if "</div></div><div class='rw'>" in a else a
    except Exception as e:  # noqa: BLE001
        _log_plain(f"team record check skipped: {e}")
    return a, b


# ---- league record: best single-player game (NFL; one game a week so Sleeper's weekly points are per game)
_REC_CACHE = {}


def nfl_player_game_record(before_week):
    """(points, player name, week) of the best game by any rostered player in weeks before `before_week`."""
    if before_week in _REC_CACHE:
        return _REC_CACHE[before_week]
    lid = str(CONFIG["nfl"].get("sleeper_league_id")).strip()
    best = (0.0, "", 0)
    allp = sleeper_players("nfl")
    for w in range(1, before_week):
        try:
            for m in fetch_url(f"{SLEEPER}/league/{lid}/matchups/{w}") or []:
                for pid, pts in (m.get("players_points") or {}).items():
                    if pts and float(pts) > best[0] and (allp.get(pid) or {}).get("position") != "DEF":
                        best = (float(pts), (allp.get(pid) or {}).get("full_name") or pid, w)
        except Exception:  # noqa: BLE001
            continue
    _REC_CACHE[before_week] = best
    return best


REC_CSS = """
.pr2.rec{position:relative;overflow:visible!important;isolation:isolate;margin-top:10px;
 background:linear-gradient(100deg,rgba(124,58,237,.45),rgba(212,175,55,.28) 55%,rgba(124,58,237,.35))!important;
 box-shadow:0 0 0 2px #d4af37,0 0 28px rgba(168,85,247,.7)!important}
.pr2.rec .fp{background:linear-gradient(160deg,#fff4c2,#d4af37 55%,#a16207)!important;color:#1b1305!important;box-shadow:0 0 20px rgba(212,175,55,.9)!important}
.recr{position:absolute;top:-13px;left:64px;z-index:5;white-space:nowrap;font-size:13px;font-weight:900;letter-spacing:1.3px;color:#fff;
 padding:3px 12px;border-radius:999px;background:linear-gradient(90deg,#6d28d9,#a855f7,#d4af37);box-shadow:0 0 16px rgba(168,85,247,.9)}
.recr em{font-style:normal;font-weight:800;opacity:.9;margin-left:6px}
"""

_nfl_game_html_norec = nfl_game_fantasy_html


def nfl_game_fantasy_html(event, summary, live, ol, base, max_rows=30):
    h = _nfl_game_html_norec(event, summary, live, ol, base, max_rows)
    try:
        if not event["status"]["type"].get("completed") or not live:
            return h
        import html as H
        rec_pts, rec_name, rec_wk = nfl_player_game_record(int(live["week"]))
        if not rec_pts:
            return h
        for p in sorted(nfl_fantasy(summary), key=lambda p: -p["pts"])[:3]:
            if p["pts"] <= rec_pts or str(p.get("id", "")).startswith("def:") or not league_owner(p.get("id"), p["name"]):
                continue
            i = h.find(f"<div class='nm2'>{H.escape(p['name'])}")
            j = h.rfind("<div class='pr2", 0, i)
            if i < 0 or j < 0:
                continue
            k = h.find(">", j)
            opening = h[j:k].replace(" sh'", "'").replace("class='pr2", "class='pr2 rec", 1)
            rest = re.sub(r"^<span class='shr'>.*?</span>", "", h[k + 1:], count=1)
            ribbon = (f"<span class='recr'>🏛️ NEW LEAGUE RECORD<em>best game in league history · old {rec_pts:.1f} "
                      f"({H.escape(rec_name)}, wk {rec_wk})</em></span>")
            h = h[:j] + opening + ">" + ribbon + rest
            h = h.replace("</style>", REC_CSS + "</style>", 1)
            break
    except Exception as e:  # noqa: BLE001
        _log_plain(f"record check skipped: {e}")
    return h


# ---- trade check-ups: 4 weeks after a trade the bot posted, who's actually winning it
def sleeper_week_points(sport, week):
    if sport == "nfl":
        return nfl_week_points(week)
    key = ("nba", week)
    if key in _SH_WEEKS:
        return _SH_WEEKS[key]
    out = {}
    try:
        season = fetch_url(f"{SLEEPER}/state/nba").get("season")
        sc = dict(league_scoring("nba"))
        for pid, st_ in (fetch_url(f"{SLEEPER}/stats/nba/regular/{season}/{week}") or {}).items():
            if isinstance(st_, dict):
                out[pid] = sum(float(v or 0) * float(sc[k]) for k, v in st_.items() if k in sc and isinstance(v, (int, float)))
    except Exception as e:  # noqa: BLE001
        _log_plain(f"NBA week points unavailable: {e}")
    _SH_WEEKS[key] = out
    return out


TC_CSS = """
.tcw{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px}
.tcs{background:rgba(255,255,255,.06);border-radius:16px;padding:12px 14px;display:flex;flex-direction:column;gap:8px;border:2px solid transparent}
.tcs.win{border-color:rgba(255,209,102,.75);box-shadow:0 0 24px rgba(255,190,70,.25)}
.tch{display:flex;justify-content:space-between;align-items:baseline}.tch b{font-size:24px;font-weight:900}.tch span{font-size:30px;font-weight:900;color:#ffd166}
.tca{display:grid;grid-template-columns:48px minmax(0,1fr) 72px 100px;gap:10px;align-items:center;background:rgba(0,0,0,.22);border-radius:10px;padding:6px 10px}
.tca img{width:48px;height:48px;border-radius:50%;object-fit:cover;object-position:top;background:#1d2436}
.tca b{display:block;font-size:17px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tca small{font-size:12.5px;color:#aab3c5;font-weight:700}
.tca .p{font-size:20px;font-weight:900;text-align:right}.tca .v{font-size:12px;font-weight:900;text-align:right;color:#cfd5e1}
"""


def trade_checkup_html(sport, t, done_week):
    import html as H
    sides = trade_sides(t)
    people = _league_people(sport)
    allp = sleeper_players(sport)
    weeks = list(range(int(t.get("leg") or 1), done_week + 1))
    pts_w = {w: sleeper_week_points(sport, w) for w in weeks}
    fc = fantasycalc_values()["players"] if sport == "nfl" else {}
    cols, totals = [], {}
    for rid, sd in sides.items():
        rows, tot = [], 0.0
        for pid in sd["players"]:
            pp = allp.get(pid) or {}
            since = sum(pts_w[w].get(pid, 0) for w in weeks)
            tot += since
            val = (f"<span class='v'>VALUE {(fc.get(pid) or {}).get('value', 0):,.0f}</span>" if sport == "nfl" else "<span></span>")
            rows.append(f"<div class='tca'><img src='https://sleepercdn.com/content/{sport}/players/thumb/{pid}.jpg'><div>"
                        f"<b>{H.escape(pp.get('full_name') or pid)}</b><small>{pp.get('position', '')} · {pp.get('team') or 'FA'}</small></div>"
                        f"<span class='p'>{since:.1f}</span>{val}</div>")
        for pk in sd["picks"]:
            v = f"VALUE {pick_value(pk['season'], pk['round']):,.0f}" if sport == "nfl" else ""
            rows.append(f"<div class='tca'><div style='font-size:28px;text-align:center'>🎟️</div><div><b>{pk['season']} {_ord(pk['round'])}-round pick</b>"
                        f"<small>not used yet</small></div><span class='p'>—</span><span class='v'>{v}</span></div>")
        totals[rid] = tot
        cols.append((rid, rows, tot))
    if len(totals) < 2:
        return None, None
    lead = max(totals, key=totals.get)
    other = min(totals, key=totals.get)
    html_cols = "".join(f"<div class='tcs{' win' if rid == lead else ''}'><div class='tch'><b>{H.escape(people.get(rid, {}).get('manager', '?'))} got</b>"
                        f"<span>{tot:.1f}</span></div>{''.join(rows)}</div>" for rid, rows, tot in cols)
    head = f"So far: {people.get(lead, {}).get('manager', '?')} winning by {totals[lead] - totals[other]:.1f} points"
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    body = (f"<div class='top'><div class='lg'><img src='{logo}'>🤝 TRADE CHECK-UP · {len(weeks)} WEEKS ON</div>"
            f"<div class='pill'>WEEK {t.get('leg')} TRADE</div></div>"
            f"<div class='hl'><div class='h1'>{H.escape(head)}</div><div class='h2'>Fantasy points each side's players have scored since the "
            f"trade (weeks {weeks[0]}–{weeks[-1]}){', plus today' + chr(39) + 's dynasty value' if sport == 'nfl' else ''}</div></div>"
            f"<div class='tcw'>{html_cols}</div>")
    html_text = _card("--a:#d97757;--h:#5b7bd5" if sport == "nfl" else "--a:#ee6730;--h:#ffb347", body, None,
                      HL_CSS + TC_CSS + "body,.card{width:1100px!important}", width=1100)
    return (_nba_theme(html_text, None, "TRADE CHECK-UP") if sport == "nba" else html_text), head


def maybe_trade_checkups(st):
    due = st.setdefault("trade_checks", {})
    for tid, x in list(due.items()):
        if x.get("done") or time.time() < x["due"]:
            continue
        sport = x["sport"]
        try:
            lid = str(CONFIG[sport].get("sleeper_league_id")).strip()
            t = next((tt for tt in fetch_url(f"{SLEEPER}/league/{lid}/transactions/{x['leg']}") or []
                      if str(tt.get("transaction_id")) == tid), None)
            done_week = int((league_settings(sport).get("settings") or {}).get("last_scored_leg") or 0)
            if not t or done_week < int(x["leg"]):
                x["done"] = True
                continue
            html_text, _ = trade_checkup_html(sport, t, done_week)
            if not html_text:
                x["done"] = True
                continue
            ok = (_nba_post(html_text, "🤝 **TRADE CHECK-UP**", {}, "checkup.png") if sport == "nba"
                  else _post_card(NFL_HOOK, html_text, "🤝 **TRADE CHECK-UP**", {}, "checkup.png"))
            if ok:
                x["done"] = True
        except Exception as ex:  # noqa: BLE001
            log(f"trade check-up error ({tid}): {ex}")
    for tid in [k for k, v in due.items() if v.get("done") and time.time() - v["due"] > 30 * 86400]:
        due.pop(tid, None)


_post_trade_nocheck = post_trade


def post_trade(sport, t):
    ok = _post_trade_nocheck(sport, t)
    if ok and _NFL_ST.get("st") is not None and (CONFIG.get("trade_checkups") or {}).get("enabled", True):
        days = float((CONFIG.get("trade_checkups") or {}).get("after_days", 28))
        _NFL_ST["st"].setdefault("trade_checks", {})[str(t.get("transaction_id"))] = {
            "sport": sport, "leg": int(t.get("leg") or 1), "due": (t.get("created") or time.time() * 1000) / 1000 + days * 86400}
    return ok


# ---- weekly NFL waiver wire (Wednesday morning)
WW_CSS = """
.ww{display:grid;grid-template-columns:1fr;gap:8px;margin-top:12px}
.wr{display:grid;grid-template-columns:56px 1fr 190px 110px;align-items:center;gap:12px;background:rgba(255,255,255,.06);border-radius:12px;padding:8px 12px}
.wr img{width:56px;height:56px;border-radius:50%;object-fit:cover;object-position:top;background:#1d2436}
.wr b{display:block;font-size:19px;font-weight:900}.wr small{font-size:13.5px;color:#aab3c5;font-weight:700}
.sp{display:flex;align-items:flex-end;gap:5px;height:44px}.sp i{width:30px;border-radius:4px 4px 0 0;background:linear-gradient(180deg,#7ee2a8,#1faa59);
 font-style:normal;font-size:11px;font-weight:900;color:#0d1220;text-align:center;line-height:14px}
.pj{text-align:center;font-weight:900;font-size:20px;background:rgba(255,209,102,.16);border-radius:8px;padding:4px 0;color:#ffd166}
.pj small{display:block;font-size:10px;letter-spacing:1px;color:#cfd5e1}
"""


def waiver_html(done_week, next_week):
    import html as H
    rp = roster_pid_index("nfl")
    allp = sleeper_players("nfl")
    wks = [w for w in (done_week - 2, done_week - 1, done_week) if w >= 1]
    pts = {w: nfl_week_points(w) for w in wks}
    proj = nfl_projections(next_week)
    fa = []
    for pid, pp in allp.items():
        if pid in rp or pp.get("position") not in ("QB", "RB", "WR", "TE") or not pp.get("team"):
            continue
        if pp.get("injury_status") in ("Out", "IR", "PUP", "Sus"):
            continue
        last = [pts[w].get(pid, 0) for w in wks]
        nxt = proj.get(pid, 0)
        if not any(last) and not nxt:
            continue
        fa.append((pid, sum(last) / len(last), last, nxt))
    fa.sort(key=lambda x: -(0.6 * x[1] + 0.4 * x[3]))
    if not fa:
        return None
    top = fa[:6]
    mxv = max(max(x[2]) for x in top) or 1
    cells = []
    for pid, avg, last, nxt in top:
        pp = allp[pid]
        bars = "".join(f"<i style='height:{max(14, 44 * v / mxv):.0f}px'>{v:.0f}</i>" for v in last)
        cells.append(f"<div class='wr'><img src='https://sleepercdn.com/content/nfl/players/thumb/{pid}.jpg'><div><b>{H.escape(pp.get('full_name', pid))}</b>"
                     f"<small>{pp.get('position')} · {pp.get('team')} · {avg:.1f} a week lately</small></div>"
                     f"<div class='sp'>{bars}</div><div class='pj'>{nxt:.1f}<small>WEEK {next_week} PROJ</small></div></div>")
    league = CONFIG["nfl"].get("league_name", "League")
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>{H.escape(league.upper())} · WAIVER WIRE</div>"
            f"<div class='pill'>WEEK {next_week}</div></div>"
            f"<div class='hl'><div class='h1'>Best free agents right now: {H.escape(allp[top[0][0]].get('full_name', ''))} leads the way</div>"
            f"<div class='h2'>Unowned in the league · weeks {wks[0]}–{wks[-1]} in your scoring (bars) + week {next_week} projection</div></div>"
            f"<div class='ww'>{''.join(cells)}</div>")
    return _card("--a:#1faa59;--h:#5b7bd5", body, None, HL_CSS + WW_CSS + "body,.card{width:1000px!important}", width=1000)


def maybe_waivers(st):
    cfg = CONFIG.get("waiver_wire") or {}
    if not cfg.get("enabled", True):
        return
    now = datetime.now(UK)
    if now.strftime("%A") != cfg.get("weekday", "Wednesday") or now.hour < int(cfg.get("hour_uk", 10)):
        return
    settings = league_settings("nfl") or {}
    s = settings.get("settings") or {}
    done_week = int(s.get("last_scored_leg") or 0)
    next_week = done_week + 1
    if done_week < 3 or next_week > int(s.get("playoff_week_start") or 15) + 2:
        return
    key = f"{settings.get('season')}-{next_week}"
    if st.get("waivers_done") == key:
        return
    html_text = waiver_html(done_week, next_week)
    if not html_text or _post_card(NFL_HOOK, html_text, "🧲 **WAIVER WIRE**", {}, "waivers.png"):
        st["waivers_done"] = key


_maybe_daily_race_extras = maybe_daily_race


def maybe_daily_race(st):
    _NFL_ST["st"] = st                                  # post_trade needs it to schedule check-ups
    for fn in (maybe_trade_checkups, maybe_waivers):
        try:
            fn(st)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"{fn.__name__} skipped: {ex}")
    return _maybe_daily_race_extras(st)


# ------------------------------------------------ landscape moment cards: TD / FG / game-changer go wide (scoreline on top,
# players on the left, fantasy impact on the right) so they read in Discord without opening

MOMENT_W = 1000
LSX_CSS = (f"body,.card{{width:{MOMENT_W}px!important}}"
           ".lsx{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:12px;align-items:start}"
           ".lsx>div>*:first-child{margin-top:0!important}"
           ".lsx .pair{grid-template-columns:1fr!important}"
           ".lsx .rcol .cv{margin-top:0!important;margin-bottom:10px}"
           ".lsx .rcol .imp{margin-top:0!important}"
           ".lsx .lcol>*+*{margin-top:10px}"
           ".lsx .im{grid-template-columns:18px minmax(0,1fr) auto auto!important;row-gap:3px!important;column-gap:16px!important}"
           ".lsx .im>*{grid-row:1}.lsx .im>:nth-child(1){grid-column:1}.lsx .im>:nth-child(2){grid-column:2}"
           ".lsx .im>:nth-child(3){grid-column:2/-1;grid-row:2;font-size:13.5px;color:#cfd5e1}"
           ".lsx .im>:nth-child(4){grid-column:3}.lsx .im>:nth-child(5){grid-column:4}"
           ".lsx .im .who5{font-size:16px!important}")


def _div_end(h, start):
    """Index just after the </div> that closes the <div ...> opening at start."""
    depth, i = 0, start
    while True:
        o, c = h.find("<div", i), h.find("</div>", i)
        if c < 0:
            return -1
        if 0 <= o < c:
            depth, i = depth + 1, o + 4
        else:
            depth, i = depth - 1, c + 6
            if depth == 0:
                return i


def _moment_landscape(html_text):
    try:
        if "class='lsx'" in html_text or re.search(r"body,\.card\{width:\d+px", html_text):
            return html_text
        s = html_text.find("<div class='sbx'>")
        tail = html_text.rfind("</div></div><script>")
        if s < 0 or tail < 0:
            return html_text
        e = _div_end(html_text, s)
        if e < 0 or e > tail:
            return html_text
        rest = html_text[e:tail]
        cut = min([k for k in (rest.find("<div class='cv'>"), rest.find("<div class='imp'>"), rest.find("<div class='fx'>"))
                   if k >= 0], default=-1)
        if cut <= 0 or not rest[:cut].strip():
            return html_text
        block = f"<div class='lsx'><div class='lcol'>{rest[:cut]}</div><div class='rcol'>{rest[cut:]}</div></div>"
        html_text = html_text[:e] + block + html_text[tail:]
        return html_text.replace("</style>", LSX_CSS + "</style>", 1)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"landscape layout skipped: {ex}")
        return html_text


OT_WIDE_CSS = ("body,.card{width:1440px!important}"
               ".otg{display:grid;grid-template-columns:1.08fr 1fr;gap:26px;align-items:center;margin-bottom:14px}"
               ".otg .otw{font-size:122px!important;margin-top:6px!important}"
               ".otg .otl{margin-top:0!important}.otg .otv{margin-top:12px!important}")
RACE_WIDE_CSS = ("body,.card{width:1260px!important}"
                 ".cmp{grid-template-columns:1fr 1fr 1fr!important;gap:6px 14px!important}"
                 ".cm{grid-template-columns:minmax(0,1fr) 160px minmax(0,1fr)!important;gap:8px!important}")


def _shape_more(html_text):
    """Overtime alarm card: hero on the left, what's on the line on the right. Playoff race: 3-across matchups."""
    try:
        if "<div class='otw'" in html_text and "<div class='otl'>" in html_text and "class='otg'" not in html_text:
            a = html_text.find("<div class='otw'")
            b = html_text.find("<div class='otl'>")
            e = _div_end(html_text, b)
            if 0 <= a < b < e:
                html_text = (html_text[:a] + "<div class='otg'><div>" + html_text[a:b] + "</div>" + html_text[b:e] + "</div>"
                             + html_text[e:])
                return html_text.replace("</style>", OT_WIDE_CSS + "</style>", 1)
        if "<div class='rt'>" in html_text and "<div class='cmp'>" in html_text and "body,.card{width:1100px" in html_text:
            return html_text.replace("</style>", RACE_WIDE_CSS + "</style>", 1).replace(
                "body,.card{width:1100px", "body,.card{width:1260px")
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"wide layout skipped: {ex}")
    return html_text


_render_card_tall = render_card
_render_anim_tall = render_anim


def render_card(html_text, width=None):
    return _render_card_tall(_shape_more(_moment_landscape(html_text)), width)


def render_anim(html_text, width=None):
    return _render_anim_tall(_shape_more(_moment_landscape(html_text)), width)


# ------------------------------------------------ readable without opening: in-game cards sized for Discord's inline view
#   Donald attaches each card as a plain image; Discord desktop shows it scaled to fit about 550 x 350 px, so a card
#   W px wide shows its text at 550 / W of its size. Target: the smallest meaningful text >= ~11px on screen, i.e.
#   about W / 50 px in the card. Less text, bigger: no top strip (the scoreboard says it), commentary kept to one
#   line, no per-row matchup column on game cards (the pinned weekly board has those), and long player
#   lists show the top 8 with the rest in one line. Few rows -> bigger rows that fill the panel.
#   tools/inline_size.py measures any card.

INLINE_W, INLINE_H = 550, 350
# Every in-game card is exactly W x (W * 350/550): the shape Discord shows at its full inline width (also in the
# components-v2 media gallery Donald posts with: same box shape, ~0.64 height/width), and the
# same size before and after an edit. Short content stretches to fill; long content is zoomed to fit.
FIT_CSS = (".card{height:%dpx!important;overflow:hidden!important}"
           ".card>.in{min-height:100%%;display:flex;flex-direction:column}"
           ".card>.in>.lsx{flex:1}")
_FIT_JS = ("<script>document.addEventListener('DOMContentLoaded',()=>{const fit=()=>{const c=document.querySelector('.card'),"
           "n=c&&c.querySelector('.in');if(!n)return;let z=1;n.style.zoom=1;"
           "for(let i=0;i<16&&c.scrollHeight>c.clientHeight+1;i++){z*=0.96;n.style.zoom=z;}};"
           "(document.fonts&&document.fonts.ready?document.fonts.ready:Promise.resolve()).then(fit);});</script>")
_MIN_TEXT_JS = ("<script>document.addEventListener('DOMContentLoaded',()=>{const f=%s;"
                "document.querySelectorAll('.card *').forEach(el=>{if(![...el.childNodes].some(n=>n.nodeType==3&&n.textContent.trim()))return;"
                "const s=parseFloat(getComputedStyle(el).fontSize);if(s<f)el.style.fontSize=f+'px';});});</script>")

BIG_MOMENT_CSS = """
.in>.top{display:none!important}
.in>.hl{margin:0 0 12px!important;padding:10px 18px!important}
.hl .h1{font-size:28px!important;line-height:1.18!important}
.hl .h2{display:-webkit-box!important;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden;
 font-size:20px!important;line-height:1.3!important;margin-top:4px!important}
.sbx{padding:10px 22px!important;margin-top:0!important;gap:12px!important}
.sbx .disc{width:80px!important;height:80px!important}.sbx .disc img{width:62px!important;height:62px!important}
.sbx .tm{gap:14px!important}
.sbx .nm{font-size:24px!important}
.sbx .sc{font-size:84px!important}
.sbx .clk{font-size:22px!important}
.sbx .yd{font-size:34px!important;margin-top:4px!important}
.sbx .st{font-size:22px!important;margin-top:6px!important;padding:3px 14px!important;display:inline-block!important;vertical-align:middle}
.sbx .plus{font-size:24px!important;padding:1px 12px!important;margin-top:4px!important}
.sbx .tryt{font-size:22px!important;padding:3px 12px!important;margin:6px 0 0 8px!important;vertical-align:middle}
.lsx{gap:14px!important;margin-top:12px!important;align-items:stretch!important}
.lsx>.lcol,.lsx>.rcol{display:flex;flex-direction:column;gap:12px}
.lsx .lcol>*{margin-top:0!important}
.lsx .lcol>.pair{flex:1;display:flex!important;flex-direction:column;gap:12px!important;margin:0!important}
.lsx .lcol>.pair>.pp5{flex:1}
.lsx .rcol>.imp,.lsx .rcol>.sw5{flex:1}
.lsx .cv{margin:0!important}
.pp5{grid-template-columns:92px minmax(0,1fr)!important;gap:16px!important;padding:10px 16px!important;align-items:center!important}
.pp5 img{width:92px!important;height:92px!important;border-radius:14px!important}
.pp5 .role{display:none!important}
.pp5 .nm{font-size:26px!important;white-space:normal;overflow:visible;line-height:1.1}
.pp5 .nm.lng,.lsx .lcol .pp5 .nm.lng{font-size:22px!important}
.pp5 .gn{gap:10px!important;margin-top:2px!important}
.pp5 .gn b{font-size:40px!important}
.pp5 .gn span{font-size:22px!important}.pp5 .gn span em{font-size:26px!important}
.pb5:not(:has(.beat)){display:none!important}
.pb5{margin-top:6px!important;display:block!important}.pb5 .bar{display:none!important}
.pb5 .beat{display:inline-block;max-width:100%;white-space:normal!important;font-size:21px!important;font-weight:900!important;padding:4px 12px!important;letter-spacing:.4px!important;
 background:#ffd166!important;color:#140d00!important;box-shadow:none!important;text-shadow:none!important;border-radius:9px!important}
.pjn{font-size:22px!important;margin-top:4px!important}.pjn b{font-size:22px!important}
.lsx .lcol:not(:has(.pp5+.pp5)) .pp5{grid-template-columns:124px minmax(0,1fr)!important}
.lsx .lcol:not(:has(.pp5+.pp5)) .pp5 img{width:124px!important;height:124px!important}
.lsx .lcol:not(:has(.pp5+.pp5)) .pp5 .nm{font-size:30px!important}
.lsx .lcol:not(:has(.pp5+.pp5)) .pp5 .gn b{font-size:50px!important}
.lsx .lcol:not(:has(.pp5+.pp5)) .pjn,.lsx .lcol:not(:has(.pp5+.pp5)) .pp5 .ow3{font-size:24px!important}
.pp5 .ow3{font-size:22px!important;margin-top:4px!important}
.cv{font-size:20px!important;padding:8px 14px!important;gap:8px!important;flex-wrap:nowrap!important;white-space:nowrap;overflow:hidden}
.cv .tg,.cv .ok,.cv .no{font-size:20px!important}.cv .cvw{font-size:21px!important}
.imp{padding:12px 18px!important;display:flex;flex-direction:column}
.imp .ih{font-size:21px!important;letter-spacing:1.5px!important;margin-bottom:2px!important}

.lsx .im{grid-template-columns:26px max-content minmax(0,1fr)!important;font-size:22px!important;padding:10px 0!important;
 column-gap:12px!important;row-gap:4px!important}
.lsx .im>*{grid-row:auto!important}
.lsx .im>:nth-child(1){grid-column:1!important;grid-row:1!important}
.lsx .im>:nth-child(2){grid-column:2!important;grid-row:1!important}
.lsx .im>:nth-child(4){grid-column:3!important;grid-row:1!important;text-align:right;white-space:normal!important;min-width:0;font-size:21px!important}
.lsx .im>:nth-child(3){grid-column:2/-1!important;grid-row:2!important;font-size:22px!important}
.lsx .im>:nth-child(5){grid-column:2/-1!important;grid-row:3!important}
.lsx .im>:nth-child(5):empty{display:none}
.lsx .im .who5{font-size:27px!important}
.lsx .im .who5{white-space:normal!important;overflow:visible!important;text-overflow:clip!important;min-width:0;overflow-wrap:anywhere}
.lsx .imp .im .who5.lng.lng.lng.lng{font-size:24px!important}
.lsx .im .ar{font-size:22px!important}
.imp:not(:has(.im+.im+.im)) .im .who5{font-size:31px!important}
.imp:not(:has(.im+.im+.im)) .im{font-size:24px!important}
.imp:not(:has(.im+.im+.im)) .im>:nth-child(3){font-size:24px!important}
.imp:has(.im+.im+.im) .im{grid-template-columns:26px max-content minmax(0,1fr)!important;padding:7px 0!important;column-gap:10px!important}
.imp:has(.im+.im+.im) .im>:nth-child(5){display:none!important}
.imp:has(.im+.im+.im) .im .who5{font-size:25px!important}
.imp:not(:has(.im+.im)) .im .who5{font-size:36px!important}.imp:not(:has(.im+.im)) .im{font-size:27px!important}
.imp:not(:has(.im+.im)) .im>:nth-child(3){font-size:27px!important}
.sw5{padding:14px 20px!important;display:flex;flex-direction:column;justify-content:space-evenly}
.sw5 .ih{font-size:21px!important}.sw5 .lb{font-size:21px!important}
.sw5 .big{font-size:40px!important}.sw5 .row{grid-template-columns:120px 1fr!important;margin-top:10px!important}
.sw5 .bar,.sw5 .bar div{height:46px!important;border-radius:0}.sw5 .bar{border-radius:12px!important}
.sw5 .sg{font-size:23px!important}
"""

BIG_LIST_CSS = """
body,.card{width:1100px!important}
.card>.in{display:grid!important;align-content:start}
.in{grid-template-columns:minmax(0,1fr) auto!important;column-gap:14px!important}
.in>.top{display:none!important}
.in>.nbar{font-size:22px!important}
.in>.hl{margin-top:0!important;display:flex;flex-direction:column;justify-content:center}
.in>.sb5,.in>.sbx{margin-top:0!important}
.hl .h1{font-size:27px!important;line-height:1.18!important}.hl .h2{font-size:21px!important;line-height:1.3!important;margin-top:6px!important}
.sb5{padding:12px 20px!important;gap:14px!important;grid-template-columns:auto auto auto!important;justify-content:center}.sb5 .sc{font-size:68px!important}
.sb5 .tm>span:not(.sc){display:none!important}
.sb5 .tm img{width:58px!important;height:58px!important}.sb5 .clk{font-size:22px!important;line-height:1.15}
.sbx{padding:12px 20px!important;gap:14px!important;grid-template-columns:auto auto auto!important;justify-content:center}.sbx .sc{font-size:68px!important}.sbx .nm{display:none!important}
.sbx .disc{width:62px!important;height:62px!important}.sbx .disc img{width:48px!important;height:48px!important}
.sbx .clk{font-size:22px!important}
.hd{margin-top:12px!important}.hd .s2{display:none!important}.hd .big2{font-size:27px!important}
.pl2{column-gap:12px!important;row-gap:8px!important;margin-top:8px!important}
.pr2{grid-template-columns:58px minmax(0,1fr) 118px!important;grid-template-rows:auto auto;column-gap:12px!important;row-gap:2px!important;
 padding:8px 12px!important;height:auto!important;min-height:0!important;align-items:center!important}
.pr2>img{grid-column:1;grid-row:1/3;width:58px!important;height:58px!important}
.pr2>div:nth-of-type(1){grid-column:2;grid-row:1}
.pr2>.fp{grid-column:3;grid-row:1/3}
.pr2>div:nth-of-type(3){grid-column:2;grid-row:2;display:flex;align-items:center;gap:10px;min-width:0}
.pr2 .mt{display:none!important}
.pr2 .nm2{font-size:24px!important;white-space:normal!important;overflow:visible!important;line-height:1.1}.pr2 .nm2 span{display:none!important}
.pr2 .ln{font-size:20px!important;margin-top:2px!important;white-space:normal!important;overflow:visible!important;line-height:1.15!important}
.pr2 .fp{font-size:31px!important;padding:5px 0!important;line-height:1.05!important}
.pr2 .fp small,.pr2 .fp.pjx small{font-size:20px!important;letter-spacing:0!important}
.pr2 .fp.pjx i{font-size:20px!important}
.pr2 .ow{font-size:22px!important;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pr2 .bd{font-size:18px!important;padding:2px 8px!important;margin-top:0!important;flex:none;letter-spacing:1px!important}
.pr2 .shr{font-size:16px!important}.pr2 .shr em{font-size:14px!important}
.more{grid-column:1/-1;font-size:20px!important;line-height:1.4!important;margin-top:10px!important;color:#c3cad8!important}
.in>.lg2{display:none!important}
.pr2.sh{padding-top:16px!important}.pr2 .shr{left:auto!important;right:134px!important;top:-13px!important}
"""

BIG_GROUP_CSS = """
body,.card{width:1100px!important}
.card>.in{display:block!important}
.in>.top{display:none!important}
.in>.nbar{font-size:22px!important}
.hl{margin-top:0!important}.hl .h1{font-size:28px!important;line-height:1.2!important}
.hl .h2{font-size:21px!important;line-height:1.35!important}
.rows{display:grid!important;grid-template-columns:1fr 1fr;gap:12px!important;margin-top:12px}
.gl{grid-template-columns:minmax(0,1fr) auto!important;padding:12px 14px!important;row-gap:10px!important;align-content:start}
.gl .sc2{grid-template-columns:40px auto auto 16px auto auto 40px!important;justify-content:start;font-size:22px!important;gap:8px!important}
.gl .sc2 img{width:40px!important;height:40px!important}
.gl .sc2 b{font-size:34px!important}
.gl .st{font-size:19px!important;justify-self:end}
.gl .f1{grid-column:1/-1;font-size:20px!important;white-space:normal!important}
.tpr{grid-template-columns:30px minmax(0,1fr) 78px!important;grid-template-rows:auto auto;row-gap:2px!important;
 font-size:22px!important;padding:8px 12px!important}
.tpr .rk5{grid-row:1/3}
.tpr .nm5{grid-column:2;font-size:23px!important}.tpr .nm5 small{font-size:17px!important}
.tpr .fp5{grid-column:3;grid-row:1/3;font-size:27px!important;padding:4px 0!important}
.tpr .pj5{display:none!important}
.tpr .ow5{grid-column:2;grid-row:2;font-size:20px!important;display:flex;gap:8px;align-items:center;min-width:0}
.tpr .ow5 em{font-size:15px!important}
.tpr .ln5{display:none!important}
.tpr .shr{display:none!important}
"""

BIG_OT_CSS = """
body,.card{width:1100px!important}
.card>.in{display:block!important}
.otg{grid-template-columns:1fr 1.15fr!important;gap:20px!important}
.otg .otw{font-size:104px!important}
.ots{font-size:24px!important}
.otv .nm{font-size:24px!important}.otv .sc{font-size:70px!important}.otv .vs{font-size:24px!important}
.otq{font-size:20px!important}
.otl{padding:12px 18px!important}.otl .ih{font-size:19px!important;line-height:1.3}
.otr{grid-template-columns:minmax(0,1fr) auto!important;row-gap:4px!important;padding:10px 0!important}
.otr .mg{font-size:26px!important}
.otr .ch{justify-content:flex-end}.otr .ch span{font-size:21px!important;padding:3px 9px!important}
.otr .pj{grid-column:1/-1;text-align:left!important;font-size:21px!important}.otr .pj b{font-size:22px!important}
.otr .flip{font-size:17px!important}
.tape span{font-size:18px!important}
"""


DENSE_CSS = """
.card>.in{display:block!important}
.r2 .n{white-space:normal!important;overflow:visible!important;text-overflow:clip!important;line-height:1.1}
.rr2 .m,.tr2 .m,.cm .n,.card .m{overflow:visible!important;text-overflow:clip!important}
.tr2 .m,.cm .n{white-space:normal!important;line-height:1.1}
"""
RACE_BIG_CSS = """
.rr2{grid-template-columns:34px minmax(0,1fr) 228px 70px 80px 80px 250px 170px!important;height:44px!important;font-size:22px!important}
.rr2.h{height:30px!important;font-size:16px!important;letter-spacing:1px!important}
.rr2 .chip{font-size:16px!important;padding:4px 10px!important;letter-spacing:.6px!important}
.rr2 .o{grid-template-columns:62px 1fr 54px!important}.rr2 .o2{grid-template-columns:60px 1fr!important}
.mv{font-size:18px!important}
.race-note{display:none!important}
.cmp{grid-template-columns:1fr 1fr!important;gap:8px 14px!important}
.cmp .cm{font-size:21px!important;padding:8px 14px!important;grid-template-columns:minmax(0,1fr) 170px minmax(0,1fr)!important}
.cmp .cm .n{white-space:nowrap;overflow:visible!important;text-overflow:clip!important}
.cmp .mt2 b{font-size:20px!important}
"""


DENSE_FIT_CSS = ".card{max-height:%dpx!important;overflow:hidden!important}"   # never taller than the box; else natural


def _dense_floor(html_text):
    """Trades and the race read better with a text floor; recaps / playoff cards keep their own sizes (sidebars)."""
    return 8.0 if ("<div class='rt'>" in html_text or "TRADE" in html_text) else 0


def _dense_css(html_text):
    return DENSE_CSS + (RACE_BIG_CSS if "<div class='rt'>" in html_text else "")


def _dense_html(html_text):
    html_text = re.sub(r"<span class='m'>([^<]{13,})</span>", r"<span class='m' style='font-size:.82em'>\1</span>", html_text)
    return html_text.replace("PLAYOFFS · SINCE YESTERDAY", "PLAYOFFS")


def _card_width(html_text):
    m = re.findall(r"body,\.card\{width:(\d+)px", html_text)
    return int(m[-1]) if m else 880


def _cap_rows(html_text, keep=8):
    """Long game cards: the top `keep` players in full, everyone else in one 'also' line."""
    s = html_text.find("<div class='pl2'>")
    if s < 0:
        return html_text
    e = _div_end(html_text, s)
    inner = html_text[s + len("<div class='pl2'>"):e - len("</div>")]
    rows, i = [], 0
    while True:
        j = inner.find("<div class='pr2", i)
        if j < 0:
            break
        k = _div_end(inner, j)
        rows.append(inner[j:k])
        i = k
    if len(rows) <= keep:
        return html_text
    import html as H
    extra = []
    for r in rows[keep:]:
        nm = re.search(r"class='nm2'>([^<]+)", r)
        pts = re.search(r"class='fp[^']*'>([^<]+)", r)
        ow = re.search(r"class='ow'>([^<]+)", r)
        if nm:
            extra.append(f"<b>{H.escape(H.unescape(nm.group(1)).strip())}</b> {pts.group(1) if pts else ''}"
                         + (f" ({H.escape(H.unescape(ow.group(1)))})" if ow else ""))
    more = f"<div class='more'>Also: {' · '.join(extra)}</div>"
    n = keep
    html_text = (html_text[:s] + "<div class='pl2'>" + "".join(rows[:keep]) + "</div>" + more + html_text[e:])
    return re.sub(r"\.pl2\{grid-template-rows:repeat\(\d+,auto\)\}", f".pl2{{grid-template-rows:repeat({(n + 1) // 2},auto)}}",
                  html_text)


def readable_inline(html_text):
    """Re-size an in-game card so it reads in Discord without opening it (see the note above)."""
    try:
        floor_k = 9.5
        if "class='wbg'" in html_text or "class='wn'" in html_text:
            return html_text                                  # weekly boards / pre-game card: sized by their own code
        if "<div class='rt'>" in html_text or "class='rc'" in html_text or "class='card shc'" not in html_text \
                and not any(k in html_text for k in ("class='otg'", "class='lsx'", "class='pl2'", "class='gl'")):
            css, w, floor_k = _dense_css(html_text), _card_width(html_text), _dense_floor(html_text)   # trades, recaps, race
            html_text = _dense_html(html_text)
        elif "class='otg'" in html_text:
            css, w = BIG_OT_CSS, 1100
            html_text = html_text.replace("<br>win ", " · win ")
        elif "class='lsx'" in html_text:
            css, w = BIG_MOMENT_CSS, MOMENT_W
            html_text = re.sub(r"<b class='who5'>([^<]{13,})</b>", r"<b class='who5 lng'>\1</b>", html_text)
            html_text = re.sub(r"<div class='nm'>([^<]{14,})</div>", r"<div class='nm lng'>\1</div>", html_text)
            m = re.search(r"<div class='tryt [a-z]+'>[^<]*</div>", html_text)
            if m and "<div class=st>" in html_text:          # try tag under the lead line, not under the +N chip
                html_text = html_text.replace(m.group(0), "", 1)
                k = html_text.find("</div>", html_text.find("<div class=st>")) + len("</div>")
                html_text = html_text[:k] + m.group(0) + html_text[k:]
            html_text = (html_text.replace("<div class='pjn'>projected ", "<div class='pjn'>proj ")
                         .replace("<span class='tg'>EXTRA POINT</span>", "<span class='tg'>XP</span>")
                         .replace("<span class='tg'>2-PT CONVERSION</span>", "<span class='tg'>2-PT</span>"))
            c, l = html_text.find("<div class='cv'>"), html_text.find("<div class='lcol'>")
            if c > 0 and l > 0 and c > l:                     # kicker / 2-pt strip under the players: evens the columns
                ce = _div_end(html_text, c)
                cv = html_text[c:ce]
                html_text = html_text[:c] + html_text[ce:]
                le = _div_end(html_text, l) - len("</div>")
                html_text = html_text[:le] + cv + html_text[le:]
        elif "<div class='pl2'>" in html_text and "class='pr2" in html_text:
            busy = "<div class='h2'>" in html_text or "class='nbar'" in html_text
            html_text = _cap_rows(html_text, 6 if busy else 8)       # extra headline lines: fewer full rows
            css, w = BIG_LIST_CSS, 1100
        elif "class='gl'" in html_text and "class='tpr" in html_text:
            css, w = BIG_GROUP_CSS, 1100
        else:
            css, w, floor_k = _dense_css(html_text), _card_width(html_text), _dense_floor(html_text)
            html_text = _dense_html(html_text)
        floor = round(w / INLINE_W * floor_k, 1)             # safety net: nothing under ~9.5px (dense: 9) on screen
        hmax = int(w * INLINE_H / INLINE_W)                   # fixed shape: Discord shows it at its full width
        dense = css.startswith(DENSE_CSS)
        html_text = re.sub(r"body,\.card\{width:\d+px(!important)?\}", "", html_text)
        fit = (DENSE_FIT_CSS if dense else FIT_CSS) % hmax
        css = f"body,.card{{width:{w}px!important}}" + css + fit
        return (html_text.replace("<style>", "<style>" + f"body,.card{{width:{w}px!important}}", 1)
                .replace("</style>", css + "</style>", 1)
                .replace("</head>", (_MIN_TEXT_JS % floor if floor_k else "") + _FIT_JS + "</head>", 1))
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"inline sizing skipped: {ex}")
        return html_text




# ------------------------------------------------ league matchups in standings order + readable win-chance meters
#   Anywhere the bot lists league matchups (pre-game "who needs what", Sunday updates, the race card) they go in
#   order of the two managers' average standings rank (1 = top of the table), closest win chance first on ties.
#   (The pinned weekly boards keep Sleeper's matchup order so people find their game in the same place.)

def _rank_sorted(pairs, ol=None, sport="nfl"):
    try:
        ppl = _wb_people(sport)
    except Exception:  # noqa: BLE001
        return list(pairs)

    def key(p):
        a, b = p
        avg = ((ppl.get(a) or {}).get("rank", 99) + (ppl.get(b) or {}).get("rank", 99)) / 2
        return avg, abs(float((ol or {}).get(a, {}).get("win", 50)) - 50)
    return sorted(pairs, key=key)


_matchup_rows_html_plain = matchup_rows_html


def matchup_rows_html(live, ol, *a, **k):
    return _matchup_rows_html_plain(dict(live, pairs=_rank_sorted(live["pairs"], ol)), ol, *a, **k)


_compact_matchups_html_plain = compact_matchups_html


def compact_matchups_html(live, ol, pairs):
    return _compact_matchups_html_plain(live, ol, _rank_sorted(pairs, ol))


METER_CSS = """
.mt2{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:10px;margin-top:8px}
.mt2 b{font-size:24px;font-weight:900;font-variant-numeric:tabular-nums}.mt2 b.dog{color:#7d879b}
.mt2 .tk{position:relative;height:16px;border-radius:999px;background:#3a4152;overflow:hidden}
.mt2 .tk i{position:absolute;top:0;bottom:0;background:linear-gradient(90deg,#4be38e,#1faa59)}
.mt2 .tk em{position:absolute;left:50%;top:0;bottom:0;width:2px;margin-left:-1px;background:rgba(255,255,255,.55)}
.mt2.sm b{font-size:17px}.mt2.sm .tk{height:10px}
"""


def meter_html(win_a, size=""):
    """Win-chance meter: big percentages, a solid green bar growing from the favourite's side."""
    wa = max(0.0, min(100.0, win_a))
    fav_left = wa >= 50
    w = wa if fav_left else 100 - wa
    side = "left:0" if fav_left else "right:0"
    return (f"<style>{METER_CSS}</style><div class='mt2 {size}'><b class='{'' if fav_left else 'dog'}'>{wa:.0f}%</b>"
            f"<div class='tk'><i style='{side};width:{w:.1f}%'></i><em></em></div>"
            f"<b class='{'dog' if fav_left else ''}'>{100 - wa:.0f}%</b></div>")


# ---- pre-game "who needs what": landscape tiles, standings order, readable without opening
#   Starters in gold; bench players in this game as muted outlined chips after them. Bench chips never add a line:
#   as many as fit, then "+N bench" (a small script in the card does the fitting).
BENCH_FIT_JS = ("<script>document.addEventListener('DOMContentLoaded',()=>{const go=()=>{"
                "document.querySelectorAll('.wch2').forEach(w=>{const bn=[...w.querySelectorAll('span.bn')];if(!bn.length)return;"
                "const st=[...w.querySelectorAll('span:not(.bn)')];bn.forEach(b=>b.style.display='none');"
                "const base=st.length?w.offsetHeight:(bn[0].style.display='',w.offsetHeight);bn[0].style.display='none';"
                "let n=bn.length,more=null;const put=()=>{if(more)more.remove();more=null;if(n<bn.length){more=document.createElement('span');"
                "more.className='bn';more.textContent='+'+(bn.length-n)+' bench';w.appendChild(more);}};"
                "let k=0;for(;k<bn.length;k++){bn[k].style.display='';n=k+1;put();if(w.offsetHeight>base+1){bn[k].style.display='none';n=k;put();"
                "if(w.offsetHeight>base+1&&more){more.textContent='+'+(bn.length-n);}break;}}"
                "bn.slice(n).forEach(b=>b.remove());});};"
                "(document.fonts&&document.fonts.ready?document.fonts.ready:Promise.resolve()).then(go);});</script>")
WHO_CSS = """
body,.card{width:1100px!important}
.top{font-size:20px!important;letter-spacing:1.5px!important}.top img{width:32px!important;height:32px!important}
.top .pill{font-size:20px!important;padding:5px 12px!important}
.hl{margin:10px 0 0!important;padding:10px 18px!important}.hl .h1{font-size:28px!important}.hl .h2{font-size:21px!important}
.wn{display:grid;grid-template-columns:1fr 1fr;gap:12px 14px;margin-top:12px}
.wnt{position:relative;background:rgba(255,255,255,.065);border-radius:14px;padding:4px 14px}
.wnt.cold{opacity:.62}
.wnr{display:grid;grid-template-columns:44px minmax(0,1fr) auto 92px;gap:10px;align-items:center;padding:7px 0;min-height:70px}
.wnr+.wnr{border-top:1px solid rgba(255,255,255,.08)}
.wnr .rk{font-size:22px;font-weight:900;color:#ffd166;text-align:center}
.wnr .wnm.s{font-size:19px;overflow:visible;text-overflow:clip}
.wnr .wnm{font-size:24px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;text-transform:none;letter-spacing:0}
.wnr .wch2{display:flex;flex-wrap:wrap;gap:5px;margin-top:3px}
.wnr .wch2 span{background:#f5c542;color:#1b1f2a;font-weight:900;font-size:17px;border-radius:7px;padding:1px 8px;white-space:nowrap}
.wnr .wch2 span small{font-size:14px;font-weight:800;opacity:.75;margin-left:5px}
.wnr .no{font-size:18px;color:#8b95a8;font-style:italic;margin-top:3px;white-space:nowrap}
.wnr .wch2 span.bn{background:transparent;color:#c3cad8;box-shadow:inset 0 0 0 1.5px #6b7385;font-weight:800}
.wnr .wch2 span.bn i{font-style:normal;font-size:12px;font-weight:900;letter-spacing:.8px;color:#8b95a8;margin-right:5px}
.wnr .wch2 span.bn small{opacity:.9}
.wnr .po{text-align:right;font-size:18px;font-weight:800;color:#cfd5e1;white-space:nowrap;line-height:1.25}
.wnr .po b{color:#fff;font-size:21px}.wnr .po .g{color:#7ee2a8}.wnr .po .r{color:#ff9a9e}.wnr .po .out{color:#7d879b;font-style:italic}
.wnr .wp{height:52px;border-radius:11px;display:flex;align-items:center;justify-content:center;font-size:30px;font-weight:900;
 background:rgba(255,255,255,.08);color:#8b95a8;font-variant-numeric:tabular-nums}
.wnr.fav .wp{background:linear-gradient(180deg,#4be38e,#1faa59);color:#062312}
.wnt .tg{position:absolute;top:-11px;right:14px;font-size:14px;font-weight:900;letter-spacing:1.2px;padding:2px 9px;border-radius:6px;
 background:#ffd166;color:#1b1305}
"""


def nfl_preview_fantasy_html(events, live, ol, title, show_teams=None):
    """Pre-game card (30 min before kick-off): every matchup as a tile, best-ranked matchups first; each side shows
    its standings rank, its players in this game (with projections), playoff odds with a win / a loss, win chance."""
    import html as H
    k = parse_time(events[0]["date"]).astimezone(UK)
    players = sleeper_players("nfl")
    proj = nfl_projections(live["week"])
    ppl = _wb_people("nfl")

    def mine(r):
        return [p for p in ol[r]["left"] if show_teams is None or (players.get(p) or {}).get("team") in show_teams]

    here = show_teams or {sl_team(t["team"]["abbreviation"]) for e in events for t in teams_of(e)}

    def bench(r):
        x = live["rosters"][r]
        on = set(x.get("starters") or [])
        out = [p for p in x.get("players") or [] if p not in on and ((players.get(p) or {}).get("team") or p) in here]
        return sorted(out, key=lambda p: -proj.get(str(p), 0))

    def chip_name(p):
        pl = players.get(p) or {}
        return short_name(pl.get("full_name") or "") or (pl.get("last_name", p) + " D/ST")

    tiles = []
    for a, b in _rank_sorted(live["pairs"], ol):
        rows, any_in = [], False
        for r, w in ((a, ol[a]["win"]), (b, 100 - ol[a]["win"])):
            pe, o = ppl.get(r) or {}, ol[r]
            ps = mine(r)
            bn = bench(r)
            any_in = any_in or bool(ps)
            chips = "".join(f"<span>{H.escape(chip_name(p))}<small>{proj.get(str(p), 0):.1f}</small></span>" for p in ps[:3])
            chips += "".join(f"<span class='bn'><i>BN</i>{H.escape(chip_name(p))}<small>{proj.get(str(p), 0):.1f}</small></span>"
                             for p in bn[:6])
            who = (f"<div class='wch2'>{chips}</div>" if ps or bn else "<div class='no'>nobody playing</div>")
            if o["po"] < 0.5 and (o["po_w"] or 0) < 0.5:
                po = "<div class='po'><span class='out'>out of the race</span></div>"
            else:
                po = (f"<div class='po'>playoffs <b>{o['po']:.0f}%</b>"
                      + (f"<br>win <span class='g'>{_pct(o['po_w'])}</span> · lose <span class='r'>{_pct(o['po_l'])}</span>"
                         if o["po_w"] is not None and o["po_l"] is not None and abs(o["po_w"] - o["po_l"]) >= 3 else "")
                      + "</div>")
            rows.append(f"<div class='wnr{' fav' if w >= 50 else ''}'><div class='rk'>#{pe.get('rank', '–')}</div>"
                        f"<div style='min-width:0'><div class='wnm{' s' if len(live['rosters'][r]['manager']) > 10 else ''}'>{H.escape(live['rosters'][r]['manager'])}</div>{who}</div>"
                        f"{po}<div class='wp'>{w:.0f}%</div></div>")
        tag = "<div class='tg'>🔥 COIN FLIP</div>" if 42 <= ol[a]["win"] <= 58 and any_in else ""
        tiles.append(f"<div class='wnt{'' if any_in else ' cold'}'>{tag}{''.join(rows)}</div>")
    pill = "KICK-OFF IMMINENT" if now_utc() > parse_time(events[0]["date"]) else f"KICK-OFF {k:%H:%M} UK"
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>NFL · WEEK {live['week']} · {H.escape(title.upper())}</div>"
            f"<div class='pill'>{pill}</div></div><div class='wn'>{''.join(tiles)}</div>")
    return _card("--a:#5b7bd5;--h:#d50a0a", body, extra_css=WHO_CSS, width=1100).replace("<head>", "<head>" + BENCH_FIT_JS, 1)


# ---- big games get hype: NFL 30+ "BIG GAME", 40+ "MONSTER GAME"; NBA (lock-in, defence-heavy) 55+ / 70+.
#   Config: <sport>.hype = [big, monster]. Works on the TD/FG scorer panels, game-card rows and grouped NBA rows,
#   stacks with the season-high ribbon (that one sits on the other side). Animated in GIFs, glowing in stills.
HYPE_DEFAULT = {"nfl": (30, 40), "nba": (55, 70)}

HYPE_CSS = """
.pp5.h3,.pr2.h3,.tpr.h3,.pp5.h4,.pr2.h4,.tpr.h4{position:relative;overflow:visible!important}
.pp5.h3,.pr2.h3,.tpr.h3{box-shadow:0 0 0 3px #ff8a00,0 0 26px rgba(255,120,0,.6)!important;
 background:linear-gradient(120deg,rgba(110,38,0,.9),rgba(24,12,8,.88))!important}
.pp5.h4,.pr2.h4,.tpr.h4{box-shadow:0 0 0 4px #ffd600,0 0 44px rgba(255,40,0,.9)!important;
 background:radial-gradient(circle at 15% 0%,rgba(255,90,0,.6),rgba(130,0,0,.8) 55%,rgba(30,4,4,.92))!important}
.hyr{position:absolute;top:-15px;right:14px;z-index:7;font-size:20px;font-weight:900;letter-spacing:1.2px;padding:3px 12px;
 border-radius:8px;white-space:nowrap;background:linear-gradient(90deg,#ff3d00,#ff9100,#ffd600,#ff9100,#ff3d00);
 background-size:240px 100%;color:#1a0800;box-shadow:0 0 16px rgba(255,120,0,.75)}
.hyr.h4{font-size:22px;color:#fff;text-shadow:0 1px 0 rgba(0,0,0,.6);
 background:linear-gradient(90deg,#9e0000,#ff2d00,#ffb000,#ff2d00,#9e0000);background-size:240px 100%;box-shadow:0 0 24px rgba(255,50,0,1)}
.pr2 .hyr{right:auto;left:70px}.pr2.h3,.pr2.h4{padding-top:16px!important}
.tpr .hyr{right:90px;top:-13px;font-size:16px}.tpr.h3,.tpr.h4{margin-top:10px}
.pp5.h3,.pp5.h4{padding-top:28px!important}
.pp5.h3 .gn span em{color:#ffb347!important}
.pp5.h4 .gn span em{color:#ffd600!important;text-shadow:0 0 12px rgba(255,60,0,.95)}
.pp5.h3 .gn,.pp5.h4 .gn{white-space:nowrap}
.pr2.h3 .fp,.tpr.h3 .fp5{background:linear-gradient(180deg,#ffc04d,#ff6a00)!important;color:#1a0800!important}
.pr2.h4 .fp,.tpr.h4 .fp5{background:linear-gradient(180deg,#fff066,#ff3d00)!important;color:#1a0000!important;
 box-shadow:0 0 18px rgba(255,60,0,.9)}
.pr2.h3 .fp{font-size:35px!important}.pr2.h4 .fp{font-size:38px!important}
"""

ANIM_CSS += """
@keyframes a_hype{0%,100%{box-shadow:0 0 0 3px #ff8a00,0 0 16px rgba(255,120,0,.45)}50%{box-shadow:0 0 0 3px #ffb000,0 0 44px rgba(255,140,0,1)}}
@keyframes a_mon{0%,100%{box-shadow:0 0 0 4px #ffd600,0 0 30px rgba(255,40,0,.8)}25%{box-shadow:0 0 0 4px #ff3d00,0 0 64px rgba(255,200,0,1)}
 50%{box-shadow:0 0 0 4px #ffd600,0 0 36px rgba(255,40,0,.9)}75%{box-shadow:0 0 0 4px #fff,0 0 70px rgba(255,90,0,1)}}
@keyframes a_shake{0%,100%{transform:translate(0,0) rotate(0)}20%{transform:translate(-2px,1px) rotate(-2deg)}
 40%{transform:translate(2px,-1px) rotate(2deg)}60%{transform:translate(-1px,-1px) rotate(-1deg)}80%{transform:translate(1px,1px) rotate(1deg)}}
@keyframes a_embers{from{background-position:0 0,23px 30px}to{background-position:0 -60px,23px -30px}}
.pp5.h3,.pr2.h3,.tpr.h3{animation:a_hype 1s ease-in-out infinite!important}
.pp5.h4,.pr2.h4,.tpr.h4{animation:a_mon .5s linear infinite!important}
.pp5.h4::after,.pr2.h4::after,.tpr.h4::after{content:'';position:absolute;inset:0;border-radius:inherit;pointer-events:none;
 background-image:radial-gradient(circle,rgba(255,214,0,.9) 0 2px,transparent 3px),radial-gradient(circle,rgba(255,110,0,.8) 0 3px,transparent 4px);
 background-size:46px 60px,70px 60px;background-position:0 0,23px 30px;opacity:.45;animation:a_embers .5s linear infinite}
.pp5.h4 .gn span em,.pr2.h4 .fp,.tpr.h4 .fp5{display:inline-block;animation:a_shake .5s linear infinite}
.hyr{animation:a_shine 1s linear infinite}
"""


def _hype_levels(html_text):
    sport = "nba" if "class='nbar'" in html_text else "nfl"
    lv = (CONFIG.get(sport) or {}).get("hype") or HYPE_DEFAULT[sport]
    return sport, float(lv[0]), float(lv[1])


def _hype_scan(html_text):
    """[(start, end, tier, points)] for every scorer panel / player row worth hype."""
    sport, lo, hi = _hype_levels(html_text)
    out = []
    for cls, num_re in (("pp5", r"now <em>([\d.]+)</em>"), ("pr2", r"class='fp[^']*'>([\d.]+)"),
                        ("tpr", r"class='fp5'>([\d.]+)")):
        for m in re.finditer(r"<div class='%s[^']*'>" % cls, html_text):
            e = _div_end(html_text, m.start())
            v = re.search(num_re, html_text[m.start():e])
            if v and float(v.group(1)) >= lo:
                out.append((m.start(), m.end(), 4 if float(v.group(1)) >= hi else 3, float(v.group(1))))
    return out


def hype_mark(html_text):
    """Add the BIG GAME / MONSTER GAME treatment to the panels and rows that earned it."""
    try:
        sport, lo, hi = _hype_levels(html_text)
        found = sorted(_hype_scan(html_text), reverse=True)
        if not found:
            return html_text
        for start, tag_end, tier, v in found:
            tag = html_text[start:tag_end]
            new = tag[:-2] + f" h{tier}'>"
            label = (f"💥 MONSTER GAME · {v:g} PTS" if tier == 4 else f"🔥 BIG GAME · {v:g} PTS")
            html_text = html_text[:start] + new + f"<span class='hyr h{tier}'>{label}</span>" + html_text[tag_end:]
        return html_text.replace("</style>", HYPE_CSS + "</style>", 1)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"hype skipped: {ex}")
        return html_text


_wants_animation_plain = wants_animation


def wants_animation(html_text):
    return _wants_animation_plain(html_text) or (
        (CONFIG.get("animations") or {}).get("enabled", True) and any(t == 4 for _, _, t, _ in _hype_scan(html_text)))


# ---- ribbons (season high, league record, big game) never leave their row and never cover text: they are merged into
#      one strip held inside the row's own width, and the row gets room above its name.
RIBBON_CSS = """
.rb{position:relative;overflow:visible!important}
.pr2.rb,.pp5.rb,.tpr.rb{padding-top:26px!important}
.pl2>.pr2.rb,.tp>.tpr.rb{margin-top:12px!important}.lsx .pp5.rb{margin-top:8px!important}
.rbs{position:absolute;top:-13px;left:10px;right:10px;display:flex;gap:6px;z-index:8;overflow:hidden;pointer-events:none;
 padding:0 2px 2px}
.rbs>*{position:static!important;flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
 font-size:16px!important;padding:3px 10px!important;letter-spacing:.8px!important;margin:0!important;box-shadow:none!important}
.rbs>* em{font-size:15px!important;margin-left:4px!important}
.pp5 .rbs{left:14px;right:14px}.pp5 .rbs>*{font-size:19px!important}.pp5 .rbs>* em{font-size:17px!important}
.tpr.rb{padding-top:20px!important}.tpr .rbs>*{font-size:14px!important}
"""


def ribbon_merge(html_text):
    try:
        found = False
        for cls in ("pp5", "pr2", "tpr"):
            i = 0
            while True:
                m = re.compile(r"<div class='%s([^']*)'>" % cls).search(html_text, i)
                if not m:
                    break
                e = _div_end(html_text, m.start())
                blk = html_text[m.end():e - len("</div>")]
                rib = []
                for rx in (r"<span class='recr'>.*?</em></span>", r"<span class='hyr[^']*'>[^<]*</span>",
                           r"<span class='shr'>.*?</em></span>"):
                    mm = re.search(rx, blk)
                    if mm:
                        rib.append(mm.group(0))
                        blk = blk.replace(mm.group(0), "", 1)
                if not rib:
                    i = e
                    continue
                short = []
                for r in rib:
                    r = re.sub(r"🏛️ NEW LEAGUE RECORD<em>best game in league history · old ([\d.]+) \((?:[^ ),]+ )*([^ ),]+), wk \d+\)</em>",
                               r"🏛️ LEAGUE RECORD<em>· old \1 (\2)</em>", r)
                    r = r.replace("<em>old best ", "<em>· old ")
                    if cls != "pp5" or len(rib) > 1:          # points are on the card already
                        r = re.sub(r"(GAME) · [\d.]+ PTS", r"\1", r)
                    if cls != "pp5" and len(rib) > 1:          # two ribbons in a row: keep both readable
                        r = re.sub(r" \([^()]*\)</em>", "</em>", r)
                    short.append(r)
                found = True
                new = (m.group(0)[:-2] + " rb'>" + "<span class='rbs'>" + "".join(short) + "</span>" + blk)
                html_text = html_text[:m.start()] + new + "</div>" + html_text[e:]
                i = m.start() + len(new)
        return html_text.replace("</style>", RIBBON_CSS + "</style>", 1) if found else html_text
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"ribbon layout skipped: {ex}")
        return html_text


# ---- positional rank badge on every player photo ("WR12"): the league-scoring season rank, like the Sleeper app.
#      Same size for everyone; only the style escalates (#1 shimmering gold, top 3 gold, top 5 silver, top 12 bronze).
_RANKS = {}

RANK_CSS = """
.pp5,.pr2,.as,.pp2,.pog{position:relative}
.phw{position:relative;display:block;width:fit-content;line-height:0;margin-bottom:15px}
.pr2>.phw{grid-column:1;grid-row:1/3}.pr2>.phw>img{width:58px!important;height:58px!important}
.prk{position:absolute;left:50%;bottom:-19px;transform:translateX(-50%);z-index:6;font-style:normal;font-weight:900;font-size:16px;
 line-height:22px;height:24px;width:auto;min-width:68px;padding:0 7px;text-align:center;letter-spacing:.3px;border-radius:999px;box-sizing:border-box;
 background:#1f2937;color:#f3f4f6;border:1.5px solid #6b7280;white-space:nowrap;pointer-events:none;font-variant-numeric:tabular-nums;
 text-shadow:none}
.as .prk,.pog .prk{min-width:58px;padding:0 6px;font-size:14px;height:22px;line-height:20px;bottom:-17px}
.prk.r12{background:linear-gradient(180deg,#f8fafc,#cbd5e1);color:#0f172a;border-color:#f8fafc}
.prk.r5{background:linear-gradient(135deg,#6d28d9,#a855f7 60%,#e879f9);color:#fff;border-color:#f0abfc;
 box-shadow:0 0 12px rgba(192,132,252,.85)}
.prk.r3{background:linear-gradient(180deg,#ffe08a,#f5b942);color:#1b1305;border-color:#fff1b8;box-shadow:0 0 12px rgba(255,200,80,.85)}
.prk.r1{background:linear-gradient(90deg,#f5b942,#fff6c2,#f5b942,#ffd166);background-size:136px 100%;color:#1b1305;
 border-color:#fffbe6;box-shadow:0 0 16px rgba(255,210,90,1),0 0 4px #fff}
.prk .sp{display:inline-block;margin-right:2px}
"""

ANIM_CSS += """
@keyframes a_rk{from{background-position:0 0}to{background-position:136px 0}}
@keyframes a_tw{0%,100%{opacity:.35;transform:scale(.8)}50%{opacity:1;transform:scale(1.2)}}
.prk.r1{animation:a_rk 1s linear infinite}
.prk.r5 .sp{animation:a_tw .5s ease-in-out infinite}
"""


def position_ranks(sport):
    """{sleeper pid: (position, rank)} from this season's stats scored with the league's settings (cached a day)."""
    hit = _RANKS.get(sport)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    out = {}
    try:
        season = fetch_url(f"{SLEEPER}/state/{sport}").get("season")
        stats = fetch_url(f"{SLEEPER}/stats/{sport}/regular/{season}?season_type=regular") or {}
        sc = league_scoring(sport)
        players = sleeper_players(sport)
        by_pos = {}
        for pid, st_ in stats.items():
            if not isinstance(st_, dict):
                continue
            p = players.get(pid) or {}
            pos = "DEF" if (sport == "nfl" and pid.isalpha() and pid.isupper()) else (p.get("position") or "")
            if not pos:
                continue
            pts = sum(float(v or 0) * float(sc[k]) for k, v in st_.items() if k in sc and isinstance(v, (int, float)))
            if pts > 0:
                by_pos.setdefault(pos, []).append((pts, pid))
        for pos, lst in by_pos.items():
            for i, (_, pid) in enumerate(sorted(lst, reverse=True), 1):
                out[pid] = (pos, i)
    except Exception as e:  # noqa: BLE001
        _log_plain(f"positional ranks unavailable: {e}")
        if hit:
            return hit[1]
    _RANKS[sport] = (time.time(), out)
    return out


def _rank_badge(pos, rk):
    tier = "r1" if rk == 1 else "r3" if rk <= 3 else "r5" if rk <= 5 else "r12" if rk <= 12 else ""
    mark = "<span class='sp'>👑</span>" if rk == 1 else ("<span class='sp'>✦</span>" if 4 <= rk <= 5 else "")
    return f"<em class='prk {tier}'>{mark}{pos}{rk}</em>"


def _badge_pid(sport, blk, img, name_re, ranks, by_espn, by_name):
    """Sleeper id of the player in one card block: data-pid, then the photo URL, then the name (+ team if shown)."""
    m = re.search(r"data-pid='(\w+)'", blk)
    if m:
        return m.group(1)
    if img:
        src = img.group(1)
        m = re.search(r"/players/thumb/(\w+)\.", src)
        if m:
            return m.group(1)
        m = re.search(r"/headshots/(?:nfl|nba)/players/full/(\d+)\.", src)
        if m and by_espn.get(m.group(1)):
            return by_espn[m.group(1)]
    nm = re.search(name_re, blk)
    if not nm:
        return None
    t = H_unescape(nm.group(1)).strip()
    if t.endswith(" D/ST") and sport == "nfl":
        tm = sl_team(t.split()[0])
        return tm if tm in ranks else None
    hits = by_name.get(name_key(t)) or []
    if len(hits) == 1:
        return hits[0]
    if hits:                                                # two players with one name: use the team on the card
        teams = set(re.findall(r"\b([A-Z]{2,4})\b", re.sub(r"<[^>]+>", " ", blk)))
        teams |= {sl_team(x) for x in teams}
        players = sleeper_players(sport)
        on = [pid for pid in hits if (players.get(pid) or {}).get("team") in teams]
        return on[0] if len(on) == 1 else None
    return None


def H_unescape(t):
    import html as H
    return H.unescape(t)


_BADGE_BLOCKS = (("pp5", r"class='nm(?: [^']*)?'>([^<]+)"), ("pr2", r"class='nm2(?: [^']*)?'>([^<]+)"),
                 ("pp2", r"class='nm(?: [^']*)?'>([^<]+)"), ("pog", r"<b>([^<]+)</b>"), ("as", r"<b>([^<]+)</b>"))


def rank_badges(html_text):
    try:
        sport = "nba" if ("class='nbar'" in html_text or "/nba/" in html_text) else "nfl"
        if "data-pid=" not in html_text and not any(f"<div class='{c}" in html_text for c, _ in _BADGE_BLOCKS):
            return html_text
        ranks = position_ranks(sport)
        if not ranks:
            return html_text
        players = sleeper_players(sport)
        by_espn = _RANKS.setdefault(f"{sport}-espn", {})
        if not by_espn:
            for pid, p in players.items():
                if p.get("espn_id"):
                    by_espn[str(p["espn_id"])] = pid
        by_name = _RANKS.get(f"{sport}-names")
        if not by_name or by_name[0] != id(ranks):
            names = {}
            for pid in ranks:
                p = players.get(pid) or {}
                if p.get("full_name"):
                    names.setdefault(name_key(p["full_name"]), []).append(pid)
            _RANKS[f"{sport}-names"] = by_name = (id(ranks), names)
        by_name = by_name[1]
        found = False
        for cls, name_re in _BADGE_BLOCKS:
            out = []
            for m in re.finditer(r"<div class='%s[ '][^>]*>" % cls, html_text):
                e = _div_end(html_text, m.start())
                blk = html_text[m.start():e]
                img = re.search(r"<img[^>]*src='([^']+)'[^>]*>", blk)
                if not img or "class='prk" in blk:
                    continue
                pid = _badge_pid(sport, blk, img, name_re, ranks, by_espn, by_name)
                if pid in ranks:
                    pos, rk = ranks[pid]
                    out.append((m.start() + img.start(), m.start() + img.end(), _rank_badge(pos, rk)))
            for a0, a1, badge in sorted(out, reverse=True):          # badge centred on the photo's bottom edge
                html_text = html_text[:a0] + "<span class='phw'>" + html_text[a0:a1] + badge + "</span>" + html_text[a1:]
                found = True
        return html_text.replace("</style>", RANK_CSS + "</style>", 1) if found else html_text
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"rank badges skipped: {ex}")
        return html_text


# ------------------------------------------------ recap / playoff cards, redesigned to read without opening (inline)
#   Same data as before, 1100 x 700 (Discord's full-width box), text >= ~22px in the card (~11px on screen):
#   results + awards as big tiles, a short lineup "verdict" band instead of the full table, a race table with
#   only what matters, the playoff picture and bracket as big tiles. Switch: config "recap_layout": "v2".
RD_CSS = """
body,.card{width:1100px!important}
.top{font-size:19px!important;letter-spacing:1.5px!important}.top img{width:32px!important;height:32px!important}
.top .pill{font-size:18px!important;padding:5px 12px!important}
.hl{margin:10px 0 0!important;padding:10px 18px!important}.hl .h1{font-size:30px!important;line-height:1.15}
.hl .h2{font-size:21px!important;margin-top:4px}
.rd2{display:grid;grid-template-columns:430px 1fr;gap:16px;margin-top:12px}
.sx{font-size:17px;font-weight:900;letter-spacing:1.6px;color:#c3cad8;margin:0 0 8px 2px}
.rsl{display:flex;flex-direction:column;gap:7px}
.rsr{display:grid;grid-template-columns:minmax(0,1fr) 84px;gap:1px 10px;align-items:center;background:rgba(255,255,255,.065);
 border-radius:11px;padding:6px 14px;font-variant-numeric:tabular-nums}
.rsr b{font-size:22px;font-weight:900;white-space:nowrap;line-height:1.15}
.rsr .s{font-size:24px;font-weight:900;text-align:right}.rsr .lo{color:#8b95a8}
.rsr b.lo{font-weight:800;font-size:19px}.rsr .s.lo{font-size:20px}
.rsr.blow{box-shadow:inset 4px 0 0 #ff8a3d}.rsr.close{box-shadow:inset 4px 0 0 #ffd166}
.awg{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.awt{position:relative;background:rgba(255,255,255,.065);border-radius:12px;padding:11px 14px;min-height:96px}
.awt .k{font-size:16px;font-weight:900;letter-spacing:1.2px;color:#ffd166}
.awt .v{font-size:25px;font-weight:900;margin-top:4px;line-height:1.1}
.awt .x{font-size:19px;color:#dbe0ea;margin-top:3px;line-height:1.25}
.awt .x b{color:#fff}
.awt.pow{background:linear-gradient(120deg,rgba(255,179,71,.22),rgba(255,255,255,.05))}
.awt .prk{position:static!important;transform:none!important;display:inline-block;vertical-align:middle;margin-left:10px}
.vb{display:flex;flex-direction:column;gap:6px;margin-top:12px;background:rgba(9,12,22,.55);border-radius:12px;padding:10px 16px}
.vb div{font-size:20px;color:#dbe0ea;line-height:1.3}.vb b{color:#fff}.vb .c{color:#ff9a9e;font-weight:900}
.st3{display:grid;grid-template-columns:minmax(0,1fr) 330px;gap:16px;margin-top:12px}
.trw{display:grid;grid-template-columns:34px minmax(0,1fr) 70px 92px 200px 84px;gap:10px;align-items:center;height:41px;
 font-size:22px;font-variant-numeric:tabular-nums;border-radius:8px;padding:0 8px}
.trw:nth-child(even){background:rgba(255,255,255,.04)}
.trw.h{height:28px;font-size:15px;font-weight:900;letter-spacing:1.2px;color:#9aa3b5;background:none!important}
.trw .k{color:#9aa3b5;font-weight:900}.trw .m{font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:clip}
.trw .pf{color:#aab3c5}.trw.cut{border-bottom:2px dashed rgba(245,197,66,.7)}.trw.out{opacity:.5}
.trw .po{display:grid;grid-template-columns:62px 1fr;gap:8px;align-items:center;font-weight:900}
.trw .po i{height:10px;border-radius:999px;background:rgba(255,255,255,.1);overflow:hidden}
.trw .po i em{display:block;height:10px;border-radius:999px;background:linear-gradient(90deg,#1faa59,#7ee2a8)}
.trw .ti{font-weight:900;color:#ffd166;text-align:right}
.trw .chip{font-size:14px;font-weight:900;letter-spacing:.6px;border-radius:6px;padding:4px 7px;text-align:center;white-space:nowrap}
.chip.in{background:#1faa59;color:#fff}.chip.bye{background:linear-gradient(90deg,#ffd166,#ffb347);color:#1b1305}
.chip.out{background:#3a1218;color:#ff9a9e}.chip.alive{background:rgba(255,255,255,.1);color:#dbe0ea}
.chip.mir{background:linear-gradient(90deg,#6d28d9,#a855f7);color:#fff}
.sdc{display:flex;flex-direction:column;gap:10px}
.sdt{background:rgba(255,255,255,.065);border-radius:12px;padding:11px 14px}
.sdt .k{font-size:16px;font-weight:900;letter-spacing:1.2px;color:#c3cad8}
.sdt .v{font-size:27px;font-weight:900;margin-top:3px;color:#ffd166;line-height:1.1}
.sdt .x{font-size:19px;color:#dbe0ea;margin-top:3px;line-height:1.3}.sdt .x b{color:#fff}
.mvl{display:flex;flex-direction:column;gap:3px;margin-top:4px;font-size:20px}
.mvl .u{color:#7ee2a8;font-weight:900}.mvl .dn{color:#ff9a9e;font-weight:900}
.stp2{display:grid;grid-template-columns:52px minmax(0,1fr);gap:12px;align-items:center;border-radius:12px;padding:10px 12px;
 background:rgba(255,255,255,.07)}
.stp2 img,.stp2 .av{width:52px;height:52px;border-radius:50%;object-fit:cover;background:#2a3246;display:flex;align-items:center;
 justify-content:center;font-weight:900;font-size:22px}
.stp2 b{font-size:24px;font-weight:900;display:block;line-height:1.1}.stp2 small{font-size:17px;font-weight:900;letter-spacing:1px}
.stp2.in,.stp2.bye{box-shadow:inset 0 0 0 2px #1faa59;background:linear-gradient(120deg,rgba(31,170,89,.25),rgba(255,255,255,.04))}
.stp2.in small,.stp2.bye small{color:#7ee2a8}
.stp2.out{box-shadow:inset 0 0 0 2px rgba(255,154,158,.6)}.stp2.out small{color:#ff9a9e}
.bk2{display:grid;grid-template-columns:1fr 1fr 0.9fr 270px;gap:14px;margin-top:12px;align-items:stretch}
.bkc2{display:flex;flex-direction:column;gap:10px}
.bkc2 .k{font-size:16px;font-weight:900;letter-spacing:1.3px;color:#c3cad8}
.bkg2{display:flex;flex-direction:column;justify-content:space-around;flex:1;gap:12px}
.bkm2{display:flex;flex-direction:column;gap:4px;background:rgba(255,255,255,.05);border-radius:12px;padding:6px}
.bkt{display:grid;grid-template-columns:30px minmax(0,1fr) auto;gap:8px;align-items:center;border-radius:9px;padding:7px 10px;
 background:rgba(255,255,255,.07);font-variant-numeric:tabular-nums}
.bkt .sd{font-size:18px;font-weight:900;color:#ffd166;text-align:center}
.bkt b{font-size:22px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:clip;line-height:1.1}
.bkt small{display:block;font-size:16px;color:#aab3c5;font-weight:700}
.bkt .o{font-size:20px;font-weight:900;color:#7ee2a8}.bkt .o.lo{color:#9aa3b5}
.bkt.w{box-shadow:inset 0 0 0 2px #ffd166}.bkt.l{opacity:.5}.bkt.tbd b{color:#8b95a8;font-weight:700}
.bkye{font-size:16px;font-weight:900;letter-spacing:1px;color:#ffd166;text-align:center;padding:2px 0}
.bko{background:rgba(9,12,22,.55);border-radius:12px;padding:12px 14px;display:flex;flex-direction:column;gap:7px}
.bko .k{font-size:16px;font-weight:900;letter-spacing:1.2px;color:#c3cad8}
.bko div.r{display:grid;grid-template-columns:minmax(0,1fr) 54px;gap:8px;align-items:center;font-size:20px}
.bko div.r span:first-child{font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:clip}
.bko div.r b{text-align:right;color:#ffd166}
.bko i{grid-column:1/-1;height:7px;border-radius:999px;background:rgba(255,255,255,.1);overflow:hidden;margin-top:-4px}
.bko i em{display:block;height:7px;background:linear-gradient(90deg,#f5b942,#ffe08a)}
"""


def _rd_card(sport, body, css_vars, label):
    body = re.sub(r"<span class='m'>([^<]{15,})</span>", r"<span class='m' style='font-size:16px;letter-spacing:-.2px'>\1</span>", body)
    body = re.sub(r"<span class='m'>([^<]{12,})</span>", r"<span class='m' style='font-size:18px'>\1</span>", body)
    body = re.sub(r"<div style='min-width:0'><b>([^<]{11,})</b>", r"<div style='min-width:0'><b style='font-size:18px'>\1</b>", body)
    html_text = _card(css_vars, body, None, HL_CSS + RD_CSS + RANK_CSS, width=1100)
    return _nba_theme(html_text, None, label) if sport == "nba" else html_text


def _rd_top(sport, title, pill):
    import html as H
    logo = NFL_LOGO if sport == "nfl" else NBA_LOGO
    return f"<div class='top'><div class='lg'><img src='{logo}'>{H.escape(title)}</div><div class='pill'>{H.escape(pill)}</div></div>"


def recap_v2_html(sport, week, live, settings):
    """Weekly recap as two inline-readable cards: (1) results + awards + lineup verdicts, (2) the title race."""
    import html as H
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
            row = {"pid": pid, "name": nm, "pts": float(x["pp"].get(pid) or 0), "manager": x["manager"]}
            (starters if pid in x["starters"] else bench).append(row)
    scores, rec = season_history(sport, week)
    odds, po_start, n_po = simulate_odds(sport, settings, scores, rec, week)
    table = sorted(rec.keys(), key=lambda r: (-(rec[r]["w"] + 0.5 * rec[r]["t"]), -rec[r]["pf"]))
    allr = [x for x in live["rosters"].values() if x.get("matchup_id")]
    allpts = sorted((x["points"] for x in allr), reverse=True)

    def allplay(p):
        return sum(1 for q in allpts if q < p), len(allpts) - 1
    top = max(allr, key=lambda x: x["points"])
    blow = max(games, key=lambda g: g[0]["points"] - g[1]["points"])
    close = min(games, key=lambda g: g[0]["points"] - g[1]["points"])
    lucky = min((w for w, _ in games), key=lambda x: x["points"])
    unlucky = max((l for _, l in games), key=lambda x: x["points"])
    pow_ = max(starters, key=lambda p: p["pts"], default=None)
    lw, ln_ = allplay(lucky["points"])
    uw, un = allplay(unlucky["points"])
    proj = {}
    try:
        proj = nfl_projections(week) if sport == "nfl" else {}
    except Exception:  # noqa: BLE001
        pass
    ranks = position_ranks(sport)
    pow_extra, badge = "", ""
    if pow_:
        pj = proj.get(pow_["pid"])
        pow_extra = f"<b>{pow_['pts']:.1f}</b> pts for {H.escape(pow_['manager'])}" + (f" · proj {pj:.1f}" if pj else "")
        if pow_["pid"] in ranks:
            pos, rk = ranks[pow_["pid"]]
            badge = _rank_badge(pos, rk)
    tiles = [
        ("⭐ PLAYER OF THE WEEK", pow_["name"] if pow_ else "—", pow_extra, " pow", badge),
        ("💥 BIGGEST WIN", blow[0]["manager"], f"by <b>{blow[0]['points'] - blow[1]['points']:.1f}</b> over {H.escape(blow[1]['manager'])}", "", ""),
        ("😬 CLOSEST FINISH", close[0]["manager"], f"by <b>{close[0]['points'] - close[1]['points']:.1f}</b> over {H.escape(close[1]['manager'])}", "", ""),
        ("🍀 LUCKIEST WIN", lucky["manager"], f"won with {lucky['points']:.1f} (beats {lw} of {ln_})", "", ""),
        ("🥶 UNLUCKIEST LOSS", unlucky["manager"], f"lost with {unlucky['points']:.1f} (beats {uw} of {un})", "", ""),
    ]
    lr = lineup_report(sport, week, live, settings)
    verdicts = []
    if lr:
        tiles.append(("🧠 MANAGER OF THE WEEK", lr[0]["manager"], f"{lr[0]['eff']:.0f}% of their best lineup", "", ""))
        perfect = [r["manager"] for r in lr if r["eff"] >= 99.5]
        if perfect:
            verdicts.append(f"🧠 <b>Perfect lineups:</b> {H.escape(', '.join(perfect[:4]))}" + (f" +{len(perfect) - 4}" if len(perfect) > 4 else ""))
        for r in [r for r in lr if r["cost"]][:2]:
            bp = players.get(r["top_bench"]) or {}
            bname = bp.get("full_name") or (f"{bp.get('last_name', '')} D/ST" if bp else "")
            verdicts.append(f"<span class='c'>💔 Bench cost {H.escape(r['manager'])} the win</span> · "
                            f"{H.escape(bname)} scored {r['top_bench_pts']:.1f} on the bench vs {H.escape(r['opp'])}")
        worst = max(lr, key=lambda r: r["left"])
        if worst["left"] >= 5 and not worst["cost"]:
            verdicts.append(f"🪑 <b>Most left on the bench:</b> {H.escape(worst['manager'])} −{worst['left']:.1f}")
    else:
        bb = max(bench, key=lambda p: p["pts"], default=None)
        low = min(allr, key=lambda x: x["points"])
        tiles.append(("📉 LOW SCORE", low["manager"], f"{low['points']:.1f} pts", "", ""))
        if bb:
            verdicts.append(f"🪑 <b>Bench star:</b> {H.escape(bb['name'])} {bb['pts']:.1f} on {H.escape(bb['manager'])}'s bench")
    res = "".join(
        f"<div class='rsr{' blow' if (w, l) == blow else ' close' if (w, l) == close else ''}'><b>{H.escape(w['manager'])}</b>"
        f"<span class='s'>{w['points']:.1f}</span><b class='lo'>{H.escape(l['manager'])}</b>"
        f"<span class='s lo'>{l['points']:.1f}</span></div>" for w, l in games)
    aw = "".join(f"<div class='awt{cls}'><div class='k'>{k}</div><div class='v'>{H.escape(v)}{bd}</div><div class='x'>{x}</div></div>"
                 for k, v, x, cls, bd in tiles)
    vb = f"<div class='vb'>{''.join(f'<div>{v}</div>' for v in verdicts[:3])}</div>" if verdicts else ""
    body_a = (_rd_top(sport, f"{league.upper()} · WEEK {week}", "WEEKLY RECAP · 1 OF 2")
              + f"<div class='hl'><div class='h1'>Week {week}: {H.escape(top['manager'])} top-scores with {top['points']:.1f}</div></div>"
              + f"<div class='rd2'><div><div class='sx'>RESULTS</div><div class='rsl'>{res}</div></div>"
              + f"<div><div class='sx'>AWARDS</div><div class='awg'>{aw}</div></div></div>{vb}")
    # card 2: the race
    rows = ["<div class='trw h'><span>#</span><span>MANAGER</span><span>W-L</span><span>PF</span><span>PLAYOFFS</span>"
            "<span style='text-align:right'>TITLE</span></div>"]
    for i, r in enumerate(table, 1):
        x = rec[r]
        po, ti = odds[r]
        rows.append(f"<div class='trw{' cut' if i == n_po else ''}{' out' if po < 0.5 else ''}'><span class='k'>{i}</span>"
                    f"<span class='m'>{H.escape(names.get(r, '?'))}</span><span>{x['w']}-{x['l']}{'-' + str(x['t']) if x['t'] else ''}</span>"
                    f"<span class='pf'>{x['pf']:.0f}</span><span class='po'><span>{po:.0f}%</span><i><em style='width:{max(po, 1.5):.0f}%'></em></i></span>"
                    f"<span class='ti'>{ti:.0f}%</span></div>")
    fav = max(odds, key=lambda r: odds[r][1])
    side = [f"<div class='sdt'><div class='k'>🏆 TITLE FAVOURITE</div><div class='v'>{H.escape(names[fav])}</div>"
            f"<div class='x'><b>{odds[fav][1]:.0f}%</b> to win it all</div></div>"]
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
            side.append(f"<div class='sdt'><div class='k'>👀 GAME TO WATCH NEXT WEEK</div><div class='x'><b>{H.escape(names[a])}</b> v "
                        f"<b>{H.escape(names[b])}</b><br>playoff odds {odds[a][0]:.0f}% and {odds[b][0]:.0f}%</div></div>")
    except Exception as e:  # noqa: BLE001
        _log_plain(f"recap next week unavailable: {e}")
    try:
        now_r, _, _ = power_rankings(sport, week)
        prev_r, _, _ = power_rankings(sport, week - 1) if week > 1 else ([], {}, {})
        mv = []
        for i, r in enumerate(now_r, 1):
            if r in prev_r:
                d = (prev_r.index(r) + 1) - i
                if d:
                    mv.append((abs(d), d, i, r))
        mv = sorted(mv, reverse=True)[:3]
        if now_r:
            items = "".join(f"<div><span class='{'u' if d > 0 else 'dn'}'>{'▲' if d > 0 else '▼'}{abs(d)}</span> "
                            f"{H.escape(names.get(r, '?'))} <span style='color:#9aa3b5'>now #{i}</span></div>" for _, d, i, r in mv)
            side.append(f"<div class='sdt'><div class='k'>⚡ POWER RANKINGS</div><div class='x'>#1 <b>{H.escape(names.get(now_r[0], '?'))}</b></div>"
                        f"<div class='mvl'>{items}</div></div>")
    except Exception as e:  # noqa: BLE001
        _log_plain(f"recap power rankings unavailable: {e}")
    left = max(0, po_start - week - 1)
    body_b = (_rd_top(sport, f"{league.upper()} · WEEK {week}", "TITLE RACE · 2 OF 2")
              + f"<div class='hl'><div class='h1'>The race: top {n_po} make it, {left} week{'s' if left != 1 else ''} to go</div></div>"
              + f"<div class='st3'><div>{''.join(rows)}</div><div class='sdc'>{''.join(side)}</div></div>")
    v = "--a:#1faa59;--h:#5b7bd5"
    return _rd_card(sport, body_a, v, "WEEKLY RECAP"), _rd_card(sport, body_b, v, "TITLE RACE")


def playoff_picture_v2_html(sport, settings, rec, status, changes, odds, week):
    import html as H
    people = _league_people(sport)
    s = settings.get("settings") or {}
    n, po = int(s.get("playoff_teams") or 6), int(s.get("playoff_week_start") or 15)
    byes = _byes(n)
    words = {"bye": "🔒 BYE CLINCHED", "in": "🔒 PLAYOFF SPOT CLINCHED", "out": "❌ ELIMINATED"}
    stamps = []
    for rid, old, new in changes[:4]:
        pe = people.get(rid, {"manager": "?"})
        r = rec[rid]
        stamps.append(f"<div class='stp2 {new}'>{_avatar(pe)}<div><b>{H.escape(pe['manager'])}</b>"
                      f"<small>{words[new]} · {r['w']}-{r['l']}</small></div></div>")
    rows = ["<div class='trw h' style='grid-template-columns:34px minmax(0,1fr) 70px 220px 84px'><span>#</span><span>MANAGER</span>"
            "<span>W-L</span><span>STATUS</span><span style='text-align:right'>ODDS</span></div>"]
    for rid in sorted(rec, key=lambda r: status[r]["seed"]):
        x, stt = rec[rid], status[rid]
        if stt["status"] == "alive" and odds.get(rid, (0, 0))[0] < 0.5:
            chip = "<span class='chip mir'>NEEDS A MIRACLE</span>"
        elif stt["status"] == "alive":
            chip = (f"<span class='chip alive'>{stt['magic']} WIN{'S' if stt['magic'] != 1 else ''} TO CLINCH</span>"
                    if stt["magic"] else "<span class='chip alive'>IN THE HUNT</span>")
        else:
            chip = f"<span class='chip {stt['status']}'>{ {'bye': 'BYE ✓', 'in': 'CLINCHED ✓', 'out': 'ELIMINATED'}[stt['status']] }</span>"
        rows.append(f"<div class='trw{' cut' if stt['seed'] == n else ''}{' out' if stt['status'] == 'out' else ''}' "
                    f"style='grid-template-columns:34px minmax(0,1fr) 70px 220px 84px'><span class='k'>{stt['seed']}</span>"
                    f"<span class='m'>{H.escape(people.get(rid, {}).get('manager', '?'))}</span><span>{x['w']}-{x['l']}</span>"
                    f"<span style='justify-self:start'>{chip}</span><span class='ti' style='color:#fff'>{odds.get(rid, (0, 0))[0]:.0f}%</span></div>")
    ins = [people[c[0]]["manager"] for c in changes if c[2] in ("in", "bye") and c[0] in people]
    outs = [people[c[0]]["manager"] for c in changes if c[2] == "out" and c[0] in people]
    head = " · ".join(x for x in ((" & ".join(ins) + (" clinch!" if len(ins) > 1 else " clinches!")) if ins else "",
                                  (" & ".join(outs) + (" are out" if len(outs) > 1 else " is out")) if outs else "") if x)
    left = max(po - 1 - week, 0)
    body = (_rd_top(sport, f"{(settings.get('name') or '').upper()} · PLAYOFF PICTURE", f"{left} WEEK{'S' if left != 1 else ''} LEFT · TOP {n}")
            + f"<div class='hl'><div class='h1'>{H.escape(head)}</div><div class='h2'>Gold line = the playoff cut · top {byes} get a bye</div></div>"
            + f"<div class='st3' style='grid-template-columns:minmax(0,1fr) 340px'><div>{''.join(rows)}</div>"
            + f"<div class='sdc'>{''.join(stamps)}</div></div>")
    return _rd_card(sport, body, "--a:#1faa59;--h:#d4af37", "PLAYOFF PICTURE")


def bracket_v2_html(sport, settings, br, rec, odds, stage):
    import html as H
    people = _league_people(sport)
    seed_of = {r: s for s, r in br["seed"].items()}

    def team(rid, g=None, side=None):
        if rid is None:
            return "<div class='bkt tbd'><span class='sd'>?</span><b>TBD</b><span></span></div>"
        pe = people.get(rid, {"manager": "?"})
        x = rec.get(rid, {"w": 0, "l": 0})
        cls, right = "", (f"<span class='o'>{odds.get(rid, 0):.0f}%</span>" if odds else "<span></span>")
        if g and g.get("win"):
            cls = " w" if g["win"] == rid else " l"
            right = f"<span class='o{'' if g['win'] == rid else ' lo'}'>{(g['pa'] if side == 'a' else g['pb']):.1f}</span>"
        return (f"<div class='bkt{cls}'><span class='sd'>{seed_of.get(rid, '')}</span><div style='min-width:0'><b>{H.escape(pe['manager'])}</b>"
                f"<small>{x['w']}-{x['l']}</small></div>{right}</div>")

    def game(g):
        return f"<div class='bkm2'>{team(g['a'], g, 'a')}{team(g['b'], g, 'b')}</div>"
    po = br["po"]
    r1 = (f"<div class='bkc2'><div class='k'>ROUND 1 · WK {po}</div><div class='bkg2'>"
          f"<div class='bkm2'>{team(br['seed'].get(1))}<div class='bkye'>BYE</div></div>{game(br['r1'][0])}{game(br['r1'][1])}"
          f"<div class='bkm2'>{team(br['seed'].get(2))}<div class='bkye'>BYE</div></div></div></div>")
    r2 = f"<div class='bkc2'><div class='k'>SEMI-FINALS · WK {po + 1}</div><div class='bkg2'>{game(br['r2'][0])}{game(br['r2'][1])}</div></div>"
    fn = f"<div class='bkc2'><div class='k'>🏆 FINAL · WK {po + 2}</div><div class='bkg2'>{game(br['final'])}</div></div>"
    if br["champ"]:
        pe = people.get(br["champ"], {"manager": "?"})
        side = f"<div class='bko'><div class='k'>🏆 CHAMPION</div><div class='r'><span>{H.escape(pe['manager'])}</span><b>👑</b></div></div>"
    else:
        rows = "".join(f"<div class='r'><span>{H.escape(people.get(r, {}).get('manager', '?'))}</span><b>{p:.0f}%</b>"
                       f"<i><em style='width:{max(p, 2):.0f}%'></em></i></div>" for r, p in sorted(odds.items(), key=lambda kv: -kv[1]))
        side = f"<div class='bko'><div class='k'>TITLE ODDS</div>{rows}</div>"
    titles = {0: "THE PLAYOFFS ARE SET", 1: "ROUND 1 RESULTS", 2: "THE FINAL IS SET", 3: "CHAMPION CROWNED"}
    head = {0: "Six teams, one trophy: the bracket is set", 1: "Round 1 is done: the semi-finals are set",
            2: "Two left standing: it's the final",
            3: f"🏆 {people.get(br['champ'], {}).get('manager', '?')} is the champion!"}.get(stage, "")
    body = (_rd_top(sport, f"{(settings.get('name') or '').upper()} · PLAYOFFS {settings.get('season', '')}", titles.get(stage, "PLAYOFFS"))
            + f"<div class='hl'><div class='h1'>{H.escape(head)}</div></div><div class='bk2'>{r1}{r2}{fn}{side}</div>")
    html_text = _rd_card(sport, body, "--a:#d4af37;--h:#5b7bd5", "PLAYOFFS")
    if br["champ"]:
        html_text = html_text.replace("<div class='card'", "<div class='card shc'", 1).replace("</style>", SH_CSS + "</style>", 1)
    return html_text


# ---- live: the recap / playoff posts use the redesigned cards; the old ones are only a fallback if a new one fails
#      (config "recap_layout": "classic" goes back to the old cards)
_recap_wide_classic = recap_wide_html
_playoff_picture_classic = playoff_picture_html
_bracket_classic = bracket_html


def _v2_layout():
    return CONFIG.get("recap_layout", "v2") == "v2"


def recap_wide_html(sport, week, live, settings):
    if _v2_layout():
        try:
            return recap_v2_html(sport, week, live, settings)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"new recap layout failed, using the classic one: {ex}")
    return _recap_wide_classic(sport, week, live, settings)


def playoff_picture_html(sport, settings, rec, status, changes, odds, week):
    if _v2_layout():
        try:
            return playoff_picture_v2_html(sport, settings, rec, status, changes, odds, week)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"new playoff picture layout failed, using the classic one: {ex}")
    return _playoff_picture_classic(sport, settings, rec, status, changes, odds, week)


def bracket_html(sport, settings, br, rec, odds, stage):
    if _v2_layout():
        try:
            return bracket_v2_html(sport, settings, br, rec, odds, stage)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"new bracket layout failed, using the classic one: {ex}")
    return _bracket_classic(sport, settings, br, rec, odds, stage)


_render_card_small = render_card
_render_anim_small = render_anim


def _inline_ready(html_text):
    return rank_badges(ribbon_merge(hype_mark(readable_inline(_shape_more(_moment_landscape(html_text))))))


def render_card(html_text, width=None):
    return _render_card_small(_inline_ready(html_text), width)


def render_anim(html_text, width=None):
    return _render_anim_small(_inline_ready(html_text), width)



# ------------------------------------------------ daily race: remember the message, and re-do a posted one in place

_maybe_daily_race_keepid = maybe_daily_race


def _race_embed(ctx, prev):
    ol, live = ctx["ol"], ctx["live"]
    mover = max(ol, key=lambda r: abs(ol[r]["po"] - prev.get(str(r), ol[r]["po"])))
    d = ol[mover]["po"] - prev.get(str(mover), ol[mover]["po"])
    fav = max(ol, key=lambda r: ol[r]["title"])
    return {"author": AU_NFL(), "color": 0xD97757,
            "title": (f"Biggest mover: {live['rosters'][mover]['manager']} {'▲' if d >= 0 else '▼'}{abs(d):.0f} "
                      f"to {ol[mover]['po']:.0f}%") if abs(d) >= 1 else "Playoff race update",
            "description": f"🏆 Title favourite: **{live['rosters'][fav]['manager']}** {ol[fav]['title']:.0f}%"}


def redo_race_post(st, message_id):
    """Rebuild today's playoff race card and swap it into an already-posted message (no new post)."""
    events = fetch("football/nfl/scoreboard").get("events", [])
    ctx = nfl_ctx(st, events)
    if not ctx:
        return False
    prev = st.get("race_prev_snap") or st.get("race_snap") or {}
    html_text = playoff_race_html(ctx, prev, datetime.now(UK).strftime("%a %-d %b"))
    png = render_card(_inject_headline(html_text, _race_embed(ctx, prev)))
    if not png:
        return False
    return discord_edit(NFL_HOOK, message_id, png, "race.png")


def maybe_daily_race(st):
    before = st.get("race_day")
    snap_before = st.get("race_snap")
    _maybe_daily_race_keepid(st)
    if st.get("race_day") != before and _LAST_MSG.get("id"):          # a new race post just went out
        st["race_msg"] = {"id": _LAST_MSG["id"], "day": st["race_day"]}
        st["race_prev_snap"] = snap_before or {}
    want = str(((CONFIG["nfl"].get("daily_race") or {}).get("redo_message_id")) or "")
    if want and st.get("race_redo_done") != want and NFL_HOOK:
        tries = st.setdefault("race_redo_tries", {})
        if tries.get(want, 0) < 3:
            tries[want] = tries.get(want, 0) + 1
            ok = redo_race_post(st, want.split(":")[0])
            if ok or ok is None:                                         # None = message gone: nothing to fix
                st["race_redo_done"] = want
                log(f"daily race message {want} re-done in place" if ok else f"race message {want} not found")



# ------------------------------------------------ outside watchdog: check in with healthchecks.io every few minutes
# If the check-ins stop (bot or whole server down), healthchecks.io posts DOWN in #rick-only. URL lives in the
# HEALTHCHECK_URL environment variable on the server (not in this public repo).

_HC = {"t": 0.0}
_save_state_hc = save_state


def _hc_ping(url):
    try:
        urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "sports-bot"}), timeout=15).read()
    except Exception as e:  # noqa: BLE001
        _log_plain(f"watchdog check-in failed: {e}")


def save_state(st):
    r = _save_state_hc(st)
    url = os.environ.get("HEALTHCHECK_URL", "").strip()
    if url and time.time() - _HC["t"] >= 240:
        _HC["t"] = time.time()
        import threading
        threading.Thread(target=_hc_ping, args=(url,), daemon=True).start()     # never holds up a post
    return r


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
        if time.time() - last_nba >= (20 if _NBA_LIVE["on"] else NBA_EVERY) or _NBA_LIVE.get("wake"):
            try:
                run_nba(st)
            except Exception as ex:  # noqa: BLE001
                log(f"NBA error: {ex}")
            last_nba = time.time()
            _NBA_LIVE["wake"] = False
        active = active or _NBA_LIVE["on"]
        save_state(st)
        now_saved = json.dumps(st, sort_keys=True)
        if keepalive and now_saved != saved and time.time() - last_push >= 120:
            push_state()                            # so a crash mid-run can't cause repeat posts
            saved, last_push = now_saved, time.time()
        elapsed = time.time() - start
        if elapsed > MAX_RUNTIME or (not active and not keepalive):
            break
        # hand over to the next run in a quiet moment (no game on, nothing about to post) instead of mid-game
        on_actions = os.environ.get("GITHUB_ACTIONS") == "true"
        if keepalive and not on_actions and _LIVE_CFG.get("new_code") and not active and not _NBA_LIVE["on"] \
                and _NEXT_WAKE["t"] is None and quiet_now():
            _log_plain("restarting onto the new code from GitHub (quiet moment)")
            break                                   # systemd restarts us; the start step pulls the new files
        if keepalive and on_actions and elapsed > HANDOFF_AFTER_MIN * 60 and not active and not _NBA_LIVE["on"] \
                and _NEXT_WAKE["t"] is None and quiet_now():
            log_handoff = f"handing over to the next run in a quiet window after {elapsed / 3600:.1f}h"
            _log_plain(log_handoff)
            break
        wait = min(POLL_SECONDS, int(CONFIG.get("live_poll_seconds", 15))) if active else idle
        if _NEXT_WAKE["t"]:                          # a post is due to be built soon: wake up exactly then
            wait = min(wait, max(1, _NEXT_WAKE["t"] - time.time()))
            _NEXT_WAKE["t"] = None
            _NBA_LIVE["wake"] = True
        time.sleep(max(1, min(wait, MAX_RUNTIME - elapsed + 1)))
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



# ------------------------------------------------ faster in-game posts: the still card goes out at once (~2-3s to
#   build), the looping animation is built in the background and swapped into the same message when it's ready.
#   One animation is built at a time (queued; Chrome runs at low priority so the next still card isn't slowed).
#   Every edit of a message bumps its version; a swap is only made if nothing newer was put in the message since
#   the animation was asked for (try edits, overturned stamps, corrections always win). Pending swaps live in memory
#   only: after a restart the message simply keeps its still card. Switch: config animations.still_first (default on).
import queue as _queue
import threading as _threading

_GIF_Q = _queue.Queue()
_GIF_VER = {}                         # message id -> version (bumped by every edit that isn't a swap)
_GIF_LOCK = _threading.Lock()         # an edit and a swap never interleave
_GIF_WORKER = {"t": None}
_GIF_LAST = {}                        # html + file name of the last still-first card (for the try-edit rebuilds)
_GIF_LOG = []                         # (built secs, swapped secs after the still) for the report / tests


def _still_first():
    a = CONFIG.get("animations") or {}
    return a.get("enabled", True) and a.get("still_first", True)


def _gif_nice():
    """Low CPU priority for the Chrome that builds animations (the bot has one core)."""
    import shutil
    if _threading.current_thread().name == "gif-swap" and shutil.which("nice"):
        return ["nice", "-n", "15"]
    return []


def _gif_bump(message_id):
    if message_id:
        _GIF_VER[str(message_id)] = _GIF_VER.get(str(message_id), 0) + 1


def _gif_queue(hook, message_id, html_text, name, t0=None, posted=None):
    """Ask for the animated version of a posted still card (latest version of that message only)."""
    if not (hook and message_id and html_text) or CAPTURE is not None:
        return
    job = {"hook": hook, "msg": str(message_id), "html": html_text, "name": name.rsplit(".", 1)[0] + ".gif",
           "ver": _GIF_VER.get(str(message_id), 0), "t0": t0, "posted": posted or time.time()}
    _GIF_Q.put(job)
    if _GIF_WORKER["t"] is None or not _GIF_WORKER["t"].is_alive():
        _GIF_WORKER["t"] = _threading.Thread(target=_gif_worker, name="gif-swap", daemon=True)
        _GIF_WORKER["t"].start()


def _gif_worker():
    while True:
        job = _GIF_Q.get()
        try:
            _gif_swap(job)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"animation swap failed: {ex}")
        finally:
            _GIF_Q.task_done()


def _gif_swap(job):
    msg = job["msg"]
    if _GIF_VER.get(msg, 0) != job["ver"]:
        return                                            # the message changed since: a newer job (or none) applies
    t = time.time()
    gif = render_anim(job["html"])
    built = time.time() - t
    if not gif:
        _log_plain("animation not built: the post keeps its still card")
        return
    with _GIF_LOCK:
        if _GIF_VER.get(msg, 0) != job["ver"]:
            _log_plain("animation dropped: the post was edited while it was being built")
            return
        ok = _discord_edit_raw(job["hook"], msg, gif, job["name"])
    if ok:
        after = time.time() - job["posted"]
        _GIF_LOG.append((round(built, 1), round(after, 1)))
        del _GIF_LOG[:-40]
        mom = f", {time.time() - job['t0']:.1f}s after the moment" if job.get("t0") else ""
        _log_plain(f"gif swapped {after:.1f}s later (built in {built:.1f}s{mom})")
        st = _NFL_ST.get("st") or {}
        for r in (st.get("td_posts") or {}).values():   # later text-only edits must name the new file
            if str(r.get("msg")) == msg:
                r["file"] = job["name"]


_discord_edit_raw = discord_edit


def discord_edit(hook, message_id, png=None, name="live.png", content=None):
    with _GIF_LOCK:
        _gif_bump(message_id)
        return _discord_edit_raw(hook, message_id, png, name, content)


_post_card_animfirst = _post_card


def _post_card(hook, html_text, content, embed, name):
    """Animated moments: post the still card now and swap the animation in when it's built."""
    if not ((CONFIG.get("bot") or {}).get("image_only", True) and wants_animation(html_text) and _still_first()):
        return _post_card_animfirst(hook, html_text, content, embed, name)
    for nm in list(_SH_HEAD):                             # same SEASON HIGH headline as the other paths
        for k in ("img_title", "title"):
            t = embed.get(k)
            if t and nm in t and "SEASON HIGH" not in t:
                embed[k] = f"🔥 SEASON HIGH! {t}"
    _SH_HEAD.clear()
    full = _inject_headline(html_text, embed)
    png = render_card(full)
    if not png:
        return _post_card_animfirst(hook, html_text, content, embed, name)
    still = name.rsplit(".", 1)[0] + ".png"
    _GIF_LAST.clear()
    _GIF_LAST.update(html=full, name=still)
    t0 = _SEND_AT.get("t0")
    if _SEND_AT.get("label") and not _SEND_AT["label"].startswith("still "):
        _SEND_AT["label"] = "still " + _SEND_AT["label"]
    _LAST_MSG.clear()
    ok = discord(hook, [], name, files={still: png})
    if ok is True and _LAST_MSG.get("id"):
        _gif_queue(hook, _LAST_MSG["id"], full, still, t0)
    return ok


_rebuild_td_image_still = _rebuild_td_image


def _rebuild_td_image(event, play, summary, ctx, tag):
    _GIF_LAST.clear()
    return _rebuild_td_image_still(event, play, summary, ctx, tag)


def _gif_after_edit(hook, message_id):
    """After a try / give-up edit made with a rebuilt still card: queue its animated version."""
    if _GIF_LAST.get("html"):
        _gif_queue(hook, message_id, _GIF_LAST["html"], _GIF_LAST["name"])



# ------------------------------------------------ where the seconds go: play -> seen in ESPN's feed -> card built -> sent
_STAGE = {}
_TICKS = []
_td_ready_stage = td_ready


def td_ready(summary, play, first_seen):
    ok = _td_ready_stage(summary, play, first_seen)
    if ok:
        _STAGE.clear()
        _STAGE.update(seen=first_seen, start=time.time())
    return ok


_post_card_stage = _post_card


def _post_card(hook, html_text, content, embed, name):
    _STAGE.setdefault("start", time.time())
    _STAGE["built0"] = time.time()
    return _post_card_stage(hook, html_text, content, embed, name)


_render_card_stage = render_card


def render_card(html_text, width=None):
    png = _render_card_stage(html_text, width)
    if "built0" in _STAGE and "built" not in _STAGE:
        _STAGE["built"] = time.time()
    return png


_record_send_stage = _record_send


def _record_send(ok):
    t0, lab = _SEND_AT.get("t0"), _SEND_AT.get("label") or "post"
    if ok and t0 and _STAGE.get("seen"):
        now = time.time()
        seen = _STAGE["seen"]
        built = _STAGE.get("built") or now
        gaps = [b - a for a, b in zip(_TICKS[-6:], _TICKS[-5:])]
        every = f" · ESPN checked every {sum(gaps) / len(gaps):.0f}s" if gaps else ""
        _log_plain(f"timing {lab}: play -> seen in ESPN feed {seen - t0:.1f}s · seen -> card built {built - seen:.1f}s · "
                   f"built -> sent {now - built:.1f}s{every}")
    _STAGE.clear()
    return _record_send_stage(ok)


_nfl_prime_slot_tick = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    if any(e["status"]["type"]["state"] == "in" for e in slot):
        _TICKS.append(time.time())
        del _TICKS[:-20]
    return _nfl_prime_slot_tick(slot, st, getctx, force)



# ------------------------------------------------ defensive / special-teams touchdowns: who really scored, and the D/ST
#   ESPN's scoring-play line for these is odd ("Bryan Anger Punt Blk'd Kick Blocked by Ty Robinson"); the drive chart has
#   the real story ("... BLOCKED by T.Robinson ... RECOVERED by PHI-K.Moore at DAL 3. K.Moore for 3 yards, TOUCHDOWN").
#   Every NFL summary we fetch is indexed so td_parse can use it. The card: returner (blocker credited), the scoring
#   team's D/ST owner in the fantasy impact (TD + block points, live D/ST total, win swing), MONSTER-style hype and the
#   ESPN win-chance swing.
_PLAYTXT = {}
_RET_KINDS = (("blocked punt", "blocked_punt", "blocked"), ("blocked field goal", "blocked_fg", "blocked"),
              ("blocked fg", "blocked_fg", "blocked"), ("punt return", "punt", "punt"),
              ("kickoff return", "kick", "kick"), ("interception return", "int", "int"),
              ("fumble return", "fumble", "fumble"), ("fumble recovery (opponent)", "fumble", "fumble"),
              ("missed field goal return", "kick", "kick"))
_DST_SUB = {"blocked_punt": ("BLOCKED PUNT TD", "⚡"), "blocked_fg": ("BLOCKED FG TD", "⚡"), "punt": ("PUNT RETURN TD", "⚡"),
            "kick": ("KICK RETURN TD", "⚡"), "int": ("PICK-SIX", "🛡️"), "fumble": ("SCOOP AND SCORE", "🛡️")}
_fetch_index_plain = fetch


def _index_summary(d):
    names = []
    for t in (d.get("boxscore") or {}).get("players") or []:
        ab = (t.get("team") or {}).get("abbreviation", "")
        for cat in t.get("statistics") or []:
            for a in cat.get("athletes") or []:
                nm = (a.get("athlete") or {}).get("displayName")
                if nm:
                    names.append((nm, ab))
    dr = d.get("drives") or {}
    for dv in list(dr.get("previous") or []) + ([dr["current"]] if dr.get("current") else []):
        for p in dv.get("plays") or []:
            if "touchdown" in ((p.get("type") or {}).get("text") or "").lower():
                _PLAYTXT[str(p.get("id"))] = (p.get("text") or "", names)
    if len(_PLAYTXT) > 4000:
        for k in list(_PLAYTXT)[:2000]:
            _PLAYTXT.pop(k, None)


def fetch(path, tries=3):
    d = _fetch_index_plain(path, tries)
    if path.startswith("football/nfl/summary"):
        try:
            _index_summary(d)
        except Exception:  # noqa: BLE001
            pass
    return d


_SFX = r"(?:\s(?:Jr|Sr|II|III|IV)\.?)?"
_ABBR = r"[A-Z][A-Za-z]?\.\s?[A-Z][\w'\-]+" + _SFX


def _full_name(short, names, team):
    """'K.Moore' -> 'Kenny Moore II' using the game's box score (scoring team first)."""
    m = re.match(r"([A-Z][A-Za-z]?)\.\s?([\w'\-]+)", short or "")
    if not m:
        return short
    ini, last = m.group(1).lower(), m.group(2).lower()
    best = []
    for nm, ab in names:
        parts = [w for w in nm.split() if w.lower().rstrip(".") not in ("jr", "sr", "ii", "iii", "iv")]
        if len(parts) >= 2 and parts[-1].lower() == last and parts[0].lower().startswith(ini):
            best.append((ab != team, nm))
    best = sorted(set(best))
    return best[0][1] if best else f"{m.group(1)}. {m.group(2)}"


_td_parse_plain = td_parse


def td_parse(play):
    td = _td_parse_plain(play)
    try:
        tt = ((play.get("type") or {}).get("text") or "").lower()
        hit = next(((sub, k) for key, sub, k in _RET_KINDS if key in tt), None)
        if not hit:
            return td
        sub, kind = hit
        info = _PLAYTXT.get(str(play.get("id")))
        team = (play.get("team") or {}).get("abbreviation", "")
        td.update(kind=kind, sub=sub)
        raw = td.get("raw") or ""
        b = re.search(r"Blocked by ([A-Z][\w'\-]+(?: [A-Z][\w'\-.]+)+?)(?:\s*\(|$)", raw)
        if b:
            td["blocker"] = b.group(1).strip()
        if not info:
            if td.get("scorer") and len(td["scorer"].split()) > 4:  # the odd ESPN line: never show it as a name
                td["scorer"] = td.get("blocker") or "Special teams"
            return td
        text, names = info
        m = re.search(rf"({_ABBR})(?: to [A-Z]{{2,3}} \d+)? for (-?\d+|no gain)(?: yards?)?,? TOUCHDOWN", text)
        who = m.group(1) if m else None
        if m:
            td["yds"] = 0 if m.group(2) == "no gain" else int(m.group(2))
        if not who:
            r = re.search(rf"RECOVERED by [A-Z]{{2,3}}-({_ABBR})", text) or re.search(rf"INTERCEPTED by ({_ABBR})", text)
            who = r.group(1) if r else None
        if who:
            td["scorer"] = _full_name(who, names, team)
        bb = re.search(rf"BLOCKED by ({_ABBR})", text)
        if bb and not td.get("blocker"):
            td["blocker"] = _full_name(bb.group(1), names, team)
        if td.get("blocker") == td.get("scorer"):
            td.pop("blocker", None)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"return TD parse skipped: {ex}")
    return td


_td_headline_plain = _td_headline


def _td_headline(td, nick):
    sub = td.get("sub")
    if not sub:
        return _td_headline_plain(td, nick)
    y = td.get("yds")
    far = f" {y} yards" if y else ""
    who, blk = td["scorer"], td.get("blocker")
    if sub == "blocked_punt":
        return (f"BLOCKED PUNT! {blk} blocks it, {who} takes it to the house — {nick}!" if blk
                else f"BLOCKED PUNT! {who} takes it to the house — {nick}!")
    if sub == "blocked_fg":
        return (f"BLOCKED FIELD GOAL! {blk} gets a hand on it, {who} returns it{far} — {nick}!" if blk
                else f"BLOCKED FIELD GOAL! {who} returns it{far} for the TD — {nick}!")
    if sub == "punt":
        return f"PUNT RETURN TD! {who} takes it{far} to the house — {nick}!"
    if sub == "kick":
        return f"KICK RETURN TD! {who} goes{far} all the way — {nick}!"
    if sub == "int":
        return f"PICK-SIX! {who} picks it off and takes it{far} to the house — {nick}!"
    return f"SCOOP AND SCORE! {who} returns the fumble{far} for the TD — {nick}!"


_td_yards_label_plain = _td_yards_label


def _td_yards_label(td):
    return _DST_SUB[td["sub"]][0] if td.get("sub") in _DST_SUB else _td_yards_label_plain(td)


def _win_swing(summary, play_id, home_side):
    """(win chance before, after) for the scoring side from ESPN's win probability, or None."""
    wp = {str(w.get("playId")): float(w.get("homeWinPercentage") or 0) * 100 for w in summary.get("winprobability") or []}
    prev = None
    for p in _all_plays(summary):
        w = wp.get(str(p.get("id")))
        if str(p.get("id")) == str(play_id) and w is not None and prev is not None:
            return (prev, w) if home_side else (100 - prev, 100 - w)
        if w is not None:
            prev = w
    return None


DST_CSS = """
.dstw{margin-top:10px;display:flex;align-items:center;gap:14px;white-space:nowrap;padding:10px 16px;border-radius:14px;
 background:linear-gradient(100deg,rgba(255,214,0,.28),rgba(255,61,61,.22));box-shadow:0 0 0 3px #ffd600,0 0 34px rgba(255,140,0,.75);
 font-size:20px;font-weight:800;color:#fff}
.dstw b{font-size:24px;font-weight:900;color:#ffd600}.dstw .gc{font-size:16px;font-weight:900;letter-spacing:1.6px;background:#ff3d3d;
 color:#fff;border-radius:7px;padding:3px 9px;flex:none}
.pp5 .blk{font-size:20px;color:#ffe08a;font-weight:800;margin-top:3px}.pp5 .blk b{color:#fff}
"""
ANIM_CSS += ".dstw{animation:a_pulse 1s ease-in-out infinite}"
_nfl_td_html_dst = nfl_td_fantasy_html


def nfl_td_fantasy_html(event, play, summary, live, ol, base, *a, **k):
    st = _NFL_ST.get("st") or {}
    prev_win = dict(st.get("nfl_td_win") or {})
    h = _nfl_td_html_dst(event, play, summary, live, ol, base, *a, **k)
    td = td_parse(play)
    if not td.get("sub"):
        return h
    try:
        import html as H
        away, home = teams_of(event)
        abbr = (play.get("team") or {}).get("abbreviation", "")
        team = away if away["team"]["abbreviation"] == abbr else home
        nick = team["team"].get("name") or abbr
        label, emo = _DST_SUB[td["sub"]]
        # D/ST owner instead of "unrostered — available on waivers"
        sc = league_scoring("nfl")
        st_td = td["kind"] in ("punt", "kick", "blocked")
        pts_td = float(sc.get("def_st_td" if st_td else "def_td") or sc.get("def_td") or 6)
        extra = {"blocked": ("block", float(sc.get("blk_kick") or 0)), "int": ("INT", float(sc.get("int") or 0)),
                 "fumble": ("fumble rec", float(sc.get("fum_rec") or 0))}.get(td["kind"])
        gain = pts_td + (extra[1] if extra else 0)
        bits = f"TD +{pts_td:g}" + (f" · {extra[0]} +{extra[1]:g}" if extra and extra[1] else "")
        o = league_owner(f"def:{sl_team(abbr)}", "")
        dn = next((x for x in nfl_defenses(summary, event) if x["team"] == abbr), None)
        row = ""
        if o:
            rid = next((r for r, x in live["rosters"].items() if x["manager"] == o["manager"]), None)
            win = ""
            if rid is not None and rid in ol:
                pw = prev_win.get(str(rid))
                d = ol[rid]["win"] - pw if pw is not None else 0
                win = (f"win <b>{ol[rid]['win']:.0f}%</b>"
                       + (f" <span class='{'u' if d > 0 else 'dn'}'>{'▲' if d > 0 else '▼'}{abs(d):.0f}</span>" if abs(d) >= 1 else ""))
            row = (f"<div class='im'><span class='ar u'>▲</span><b class='who5'>{H.escape(o['manager'])}</b>"
                   f"<span>{H.escape(nick)} D/ST <b class='u'>+{gain:g}</b> ({bits})"
                   + (f" → now <b>{fmt_pts(dn['pts'])}</b> pts" if dn else "") + f"</span>"
                   f"<span>{win or ('starting' if o['starting'] else 'bench')}</span><span></span></div>")
        h = re.sub(r"<div class='im'><span class='ar'>🆓</span>.*?</span><span></span><span></span></div>", row, h, count=1,
                   flags=re.S) if "🆓" in h else h.replace("<div class='ih'>FANTASY IMPACT</div>",
                                                         "<div class='ih'>FANTASY IMPACT</div>" + row, 1)
        # scorer panel: MONSTER-style hype, blocker credited
        i = h.find("<div class='pp5")
        if i >= 0:
            j = h.find(">", i)
            tag = h[i:j + 1]
            h = h[:i] + tag.replace("class='pp5", "class='pp5 h4", 1) + \
                f"<span class='hyr h4'>{emo} {label} · {H.escape(nick.upper())}</span>" + h[j + 1:]
            g0 = h.find("<div class='gn'>", i)                # the returner scores for the team's D/ST
            if g0 > 0:
                g1 = _div_end(h, g0)
                h = h[:g0] + (f"<div class='gn'><b>+{gain:g}</b><span>for the <em>{H.escape(nick)} D/ST</em></span></div>") + h[g1:]
            w0 = h.find("<div class='ow3'>", i)
            if w0 > 0:
                w1 = _div_end(h, w0)
                own = (f"<b>{H.escape(o['manager'])}</b> {'⭐ starting' if o['starting'] else '· bench'}" if o
                       else f"<b>{H.escape(nick)} D/ST</b> unrostered")
                h = h[:w0] + f"<div class='ow3'>{own}</div>" + h[w1:]
            if td.get("blocker"):
                k2 = h.find("<div class='gn'>", i)
                if k2 > 0:
                    h = h[:k2] + f"<div class='blk'>🖐️ blocked by <b>{H.escape(td['blocker'])}</b></div>" + h[k2:]
        # game-changer strip: ESPN win chance swing
        sw = _win_swing(summary, play.get("id"), team is home)
        strip = f"<div class='dstw'><span class='gc'>GAME-CHANGER</span>"
        if sw and abs(sw[1] - sw[0]) >= 1:
            strip += f"{H.escape(nick)} win chance <b>{sw[0]:.0f}% → {sw[1]:.0f}%</b>"
        else:
            strip += f"{emo} <b>{label}</b> · {H.escape(nick)} special teams strike"
        strip += "</div>"
        k3 = h.find("<div class='imp'>")
        h = (h[:k3] + strip + h[k3:]) if k3 >= 0 else h.replace("</div></div></body>", strip + "</div></div></body>", 1)
        h = h.replace("</style>", HYPE_CSS + DST_CSS + "</style>", 1)
        _last_td_html["h"] = h
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"D/ST touchdown extras skipped: {ex}")
    return h


# ---- one-off: rebuild posted touchdowns in place (config nfl.redo_td_posts: [play ids]), once each, with the try state
_nfl_prime_slot_redo = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    active = _nfl_prime_slot_redo(slot, st, getctx, force)
    want = [str(x) for x in (CONFIG.get("nfl") or {}).get("redo_td_posts") or []]
    done = st.setdefault("td_redone", [])
    posts = st.get("td_posts") or {}
    for pid in want:
        r = posts.get(pid)
        if pid in done or not r or r.get("done"):
            continue
        e = next((x for x in slot if x["id"] == r.get("game")), None)
        if e is None:
            continue
        try:
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            p = next((q for q in summary.get("scoringPlays") or [] if str(q.get("id")) == pid), None)
            ctx = getctx()
            if not p or not ctx:
                continue
            res = td_try(p, summary)
            pl = p if res is not None else td_only_play(p, summary)
            tag = res if res is not None else ({"label": "⏳ CONVERSION TO COME", "cls": "wait"}
                                               if e["status"]["type"].get("state") == "in" else None)
            img, name, html_text = _rebuild_td_image(e, pl, summary, ctx, tag)
            if img and discord_edit(NFL_HOOK, r["msg"], img, name):
                _gif_after_edit(NFL_HOOK, r["msg"])
                if html_text:
                    _TD_HTML[pid] = html_text
                if res is not None:
                    r["try"], r["try_result"] = "done", res["label"]
                r["file"] = name
                done.append(pid)
                _log_plain(f"touchdown {pid}: post rebuilt in place")
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"rebuild of touchdown {pid} skipped: {ex}")
    return active



# ------------------------------------------------ faster detection: ESPN's core plays feed
#   Measured live: a scoring play shows up in the core feed ~22-47s after ESPN's wallclock, the scoreboard ~4-10s
#   later and the summary (what the bot reads) ~13-17s after core. So every live NFL summary we fetch is topped up
#   with the scoring plays the core feed already has: the scoring line (core "shortText" = the summary's own format),
#   the score, the try and a drive-chart entry with the full text. Everything downstream (touchdowns, field goals,
#   try edits) sees the play early. The core feed is polled at most every 4s per game (last 10-20 plays only); any
#   failure backs off for a minute and the plain summary is used. Plays keep ESPN's ids in both feeds, so nothing is
#   posted twice (and a posted touchdown is also matched by team + quarter + clock in case the id changes).
#   A touchdown posted from the core feed is rebuilt once the summary has it (fresh box score), unless its try edit
#   is still to come (that edit rebuilds it anyway).
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{e}/competitions/{e}/plays"
_CORE = {}


def _core_latest(ev):
    """The last ~10-20 plays of a game from the core feed (cached 4s), or None."""
    c = _CORE.setdefault(ev, {"t": 0.0, "count": 0, "items": None, "off": 0.0})
    now = time.time()
    if now < c["off"]:
        return None
    if now - c["t"] < 4 and c["items"] is not None:
        return c["items"]
    try:
        base = CORE.format(e=ev)
        if not c["count"]:
            c["count"] = int(_fetch_index_plain_url(f"{base}?limit=1", 1).get("count") or 0)
        page = max(1, math.ceil(c["count"] / 10))
        d = _fetch_index_plain_url(f"{base}?limit=10&page={page}", 1)
        if int(d.get("pageCount") or page) > page:               # new plays since: read the new last page
            page = int(d["pageCount"])
            d = _fetch_index_plain_url(f"{base}?limit=10&page={page}", 1)
        items = list(d.get("items") or [])
        if len(items) < 5 and page > 1:                          # nearly empty last page: the one before too
            items = list(_fetch_index_plain_url(f"{base}?limit=10&page={page - 1}", 1).get("items") or []) + items
        c.update(t=now, count=int(d.get("count") or c["count"]), items=items)
        return items
    except Exception as ex:  # noqa: BLE001
        c["off"] = now + 60
        _log_plain(f"core plays feed unavailable, using the summary only: {ex}")
        return None


def _fetch_index_plain_url(url, tries):
    return fetch_url(url, tries)


import math  # noqa: E402


def _core_merge(d, ev):
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    if ((comp.get("status") or {}).get("type") or {}).get("state") != "in":
        return d
    items = _core_latest(ev)
    if not items:
        return d
    teams = {str((c.get("team") or {}).get("id")): c["team"] for c in comp.get("competitors") or [] if c.get("team")}
    sps = d.setdefault("scoringPlays", [])
    have = {str(p.get("id")): p for p in sps}
    dr = d.setdefault("drives", {})
    pool = {str(p.get("id")): p for dv in list(dr.get("previous") or []) + ([dr["current"]] if dr.get("current") else [])
            for p in dv.get("plays") or []}
    injected = d.setdefault("_core_ids", [])
    for q in items:
        if not q.get("scoringPlay") or not q.get("shortText"):
            continue
        pid = str(q.get("id"))
        tid = re.search(r"/teams/(\d+)", ((q.get("team") or {}).get("$ref") or ""))
        tm = teams.get(tid.group(1)) if tid else None
        if not tm:
            continue
        sp = have.get(pid)
        if sp is None:
            logo = next((x.get("href") for x in tm.get("logos") or [] if x.get("href")), "")
            sp = {"id": pid, "type": q.get("type") or {}, "text": q["shortText"], "awayScore": q.get("awayScore"),
                  "homeScore": q.get("homeScore"), "period": q.get("period") or {}, "clock": q.get("clock") or {},
                  "team": {"id": tm.get("id"), "abbreviation": tm.get("abbreviation"), "displayName": tm.get("displayName"),
                           "logo": logo},
                  "scoringType": q.get("scoringType") or {}, "_core": True}
            sps.append(sp)
            have[pid] = sp
            injected.append(pid)
        elif (int(num(q.get("awayScore"))) + int(num(q.get("homeScore"))) >
              int(num(sp.get("awayScore"))) + int(num(sp.get("homeScore")))):
            sp.update(text=q["shortText"], awayScore=q.get("awayScore"), homeScore=q.get("homeScore"))   # try is in
        dp = pool.get(pid)
        if dp is None:
            dp = {"id": pid, "type": q.get("type") or {}, "text": q.get("text") or "", "wallclock": q.get("wallclock"),
                  "scoringPlay": True, "start": {}, "end": {}}
            if q.get("pointAfterAttempt"):
                dp["pointAfterAttempt"] = q["pointAfterAttempt"]
            cur = dr.get("current") or {"plays": []}
            cur.setdefault("plays", []).append(dp)
            dr["current"] = cur
            pool[pid] = dp
            if "touchdown" in ((q.get("type") or {}).get("text") or "").lower():
                _PLAYTXT[pid] = (dp["text"], _PLAYTXT.get(pid, ("", []))[1] or [])
        elif q.get("pointAfterAttempt") and not ((dp.get("pointAfterAttempt") or {}).get("id")):
            dp["pointAfterAttempt"] = q["pointAfterAttempt"]
    return d


_fetch_core_plain = fetch


def fetch(path, tries=3):
    d = _fetch_core_plain(path, tries)
    m = re.fullmatch(r"football/nfl/summary\?event=(\d+)", path)
    if m and (CONFIG.get("nfl") or {}).get("core_feed", True):
        try:
            d = _core_merge(d, m.group(1))
            if d.get("_core_ids"):
                _index_summary(d)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"core feed merge skipped: {ex}")
    return d


_post_td_core = post_td_fantasy


def post_td_fantasy(event, play, summary, ctx):
    st = _NFL_ST.get("st")
    key = f"{event['id']}|{(play.get('team') or {}).get('abbreviation')}|{(play.get('period') or {}).get('number')}|" \
          f"{(play.get('clock') or {}).get('displayValue')}"
    keys = st.setdefault("td_keys", {}) if st is not None else {}
    if key in keys and keys[key] != str(play.get("id")):
        _log_plain(f"touchdown {play.get('id')} is {keys[key]} under another id: not posted again")
        return True
    ok = _post_td_core(event, play, summary, ctx)
    if ok and st is not None:
        keys[key] = str(play.get("id"))
        for k in list(keys)[:-200]:
            keys.pop(k, None)
        r = (st.get("td_posts") or {}).get(str(play.get("id")))
        if r is not None and play.get("_core"):
            r["core"] = True
    return ok


def _td_rebuild_post(e, pid, r, getctx, why):
    """Rebuild a posted touchdown card from the current feed (with its try state) and edit it in place."""
    summary = fetch(f"football/nfl/summary?event={e['id']}")
    p = next((q for q in summary.get("scoringPlays") or [] if str(q.get("id")) == pid), None)
    ctx = getctx()
    if not p or not ctx:
        return False
    res = td_try(p, summary)
    pl = p if res is not None else td_only_play(p, summary)
    tag = res if res is not None else ({"label": "⏳ CONVERSION TO COME", "cls": "wait"}
                                       if e["status"]["type"].get("state") == "in" else None)
    img, name, html_text = _rebuild_td_image(e, pl, summary, ctx, tag)
    if not img or r.get("done") or not discord_edit(NFL_HOOK, r["msg"], img, name):
        return False
    _gif_after_edit(NFL_HOOK, r["msg"])
    if html_text:
        _TD_HTML[pid] = html_text
    if res is not None:
        r["try"], r["try_result"] = "done", res["label"]
    r["file"] = name
    _log_plain(f"touchdown {pid}: post rebuilt in place ({why})")
    return True


_nfl_prime_slot_core = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    active = _nfl_prime_slot_core(slot, st, getctx, force)
    for pid, r in list((st.get("td_posts") or {}).items()):
        if not r.get("core") or r.get("done") or r.get("try") == "pending":
            continue
        e = next((x for x in slot if x["id"] == r.get("game")), None)
        if e is None:
            continue
        try:
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            if pid in (summary.get("_core_ids") or []):
                continue                                    # the summary hasn't caught up yet
            r["core"] = False
            _td_rebuild_post(e, pid, r, getctx, "full details")
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"details refresh of touchdown {pid} skipped: {ex}")
    return active



# ------------------------------------------------ touchdown cards: useful extras in the spare space (right column)
#   In order of usefulness (waiver watch first for an unrostered scorer; the middle two rotate for variety): win chance
#   + scoring drive + game state, the scorer's line today, league players in this game, waiver watch. All are added;
#   a small script in the card removes the lowest ones until nothing overflows (so the card never shrinks for them).
FILL_CSS = """
.lsx>.rcol{min-width:0}.lsx .rcol:has(.fx)>.imp{flex:0 0 auto!important}
.lsx .rcol>.fx:last-child{flex:1}
.fx{min-width:0;overflow:hidden;background:rgba(9,12,22,.62);border-radius:14px;padding:10px 16px 11px;box-shadow:inset 4px 0 0 var(--a)}
.fx .fh{font-size:15px;font-weight:900;letter-spacing:1.6px;color:#c3cad8;margin-bottom:5px}
.fx .fr{display:flex;flex-wrap:wrap;justify-content:space-between;column-gap:14px;min-width:0;font-size:20px;line-height:1.25;
 color:#e3e7ef;white-space:nowrap}
.fx .fr span:last-child{margin-left:auto}
.fx .fr+.fr{margin-top:3px}
.fx .fr b{color:#fff;font-weight:900}.fx .fr .u{color:#7ee2a8}.fx .fr .dn{color:#ff9a9e}.fx .fr .g{color:#ffd166}
.fx .fr span:last-child{text-align:right;color:#c3cad8}
.fx .fr span:first-child{overflow:hidden;text-overflow:clip}
.sbx .sc.hot{text-shadow:0 0 22px var(--hc,#ffd166),0 0 44px var(--hc,#ffd166)!important}
"""
FILL_JS = ("<script>document.addEventListener('DOMContentLoaded',()=>{const go=()=>{const c=document.querySelector('.card');"
           "if(!c)return;const fx=[...document.querySelectorAll('.fx')];"
           "while(fx.length&&(c.scrollHeight>c.clientHeight+1||[...document.querySelectorAll('.fx .fr')].some(r=>r.scrollWidth>r.clientWidth+1)))"
           "{fx.pop().remove();}};(document.fonts&&document.fonts.ready?document.fonts.ready:Promise.resolve()).then(go);});</script>")
_NEXTWK = {}


def _fx(head, rows):
    import html as H
    rows = [r for r in rows if r]
    if not rows:
        return ""
    return f"<div class='fx'><div class='fh'>{H.escape(head)}</div>" + "".join(
        f"<div class='fr'><span>{a}</span><span>{b}</span></div>" for a, b in rows) + "</div>"


def _stat_line(summary, name):
    """'15/20 · 247 yds · 2 TD' passing, then rushing / receiving, from the box score."""
    want = {"passing": ("completions/passingAttempts", "passingYards", "passingTouchdowns", "interceptions"),
            "rushing": ("rushingAttempts", "rushingYards", "rushingTouchdowns"),
            "receiving": ("receptions", "receivingYards", "receivingTouchdowns")}
    out = []
    for team in (summary.get("boxscore") or {}).get("players") or []:
        for cat in team.get("statistics") or []:
            if cat.get("name") not in want:
                continue
            keys = cat.get("keys") or []
            for a in cat.get("athletes") or []:
                if (a.get("athlete") or {}).get("displayName") != name:
                    continue
                v = dict(zip(keys, a.get("stats") or []))
                if cat["name"] == "passing":
                    t = f"{v.get('completions/passingAttempts', '')} · {v.get('passingYards', 0)} pass yds"
                    t += f" · {v.get('passingTouchdowns', 0)} TD" if v.get("passingTouchdowns") not in (None, "0") else ""
                    t += f" · {v.get('interceptions')} INT" if v.get("interceptions") not in (None, "0") else ""
                elif cat["name"] == "rushing":
                    t = f"{v.get('rushingAttempts', 0)} car · {v.get('rushingYards', 0)} yds"
                    t += f" · {v.get('rushingTouchdowns')} TD" if v.get("rushingTouchdowns") not in (None, "0") else ""
                else:
                    t = f"{v.get('receptions', 0)} rec · {v.get('receivingYards', 0)} yds"
                    t += f" · {v.get('receivingTouchdowns')} TD" if v.get("receivingTouchdowns") not in (None, "0") else ""
                out.append((cat["name"], t))
    return out


def _fill_sections(event, play, summary, live, td, owned):
    import html as H
    away, home = teams_of(event)
    abbr = (play.get("team") or {}).get("abbreviation", "")
    team = away if away["team"]["abbreviation"] == abbr else home
    nick = team["team"].get("name") or abbr
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    secs = {}
    # 1) win chance + drive + game state
    rows = []
    wp = summary.get("winprobability") or []
    sw = _win_swing(summary, play.get("id"), team is home)
    if sw:
        d = sw[1] - sw[0]
        rows.append((f"{H.escape(nick)} win", f"<b>{sw[1]:.0f}%</b> <span class='{'u' if d >= 0 else 'dn'}'>"
                     f"{'▲' if d >= 0 else '▼'}{abs(d):.0f}</span>"))
    elif wp:
        w = float(wp[-1].get("homeWinPercentage") or 0) * 100
        rows.append((f"{H.escape(nick)} win", f"<b>{(w if team is home else 100 - w):.0f}%</b>"))
    dr = summary.get("drives") or {}
    for dv in list(dr.get("previous") or []) + ([dr["current"]] if dr.get("current") else []):
        if any(str(p.get("id")) == str(play.get("id")) for p in dv.get("plays") or []) and dv.get("description"):
            m = re.match(r"(\d+) plays?, (-?\d+) yards?, ([\d:]+)", dv["description"])
            if m:
                rows.append(("Drive", f"<b>{m.group(1)}</b> plays · <b>{m.group(2)}</b> yds · {m.group(3)}"))
            break
    q = (play.get("period") or {}).get("number", 0)
    clk = (play.get("clock") or {}).get("displayValue", "")
    diff = abs(a_s - h_s)
    state = ("tied" if diff == 0 else "one-score game" if diff <= 8 else "two-score game" if diff <= 16 else f"{diff}-point game")
    when = f"{clk} " + (f"Q{q}" if q <= 4 else "OT")
    rows.append(("Game", f"<b class='g'>{state}</b> · {when}"))
    secs["game"] = _fx("WIN CHANCE & DRIVE", rows)
    # 2) scorer's line today
    names = [td.get("scorer")] + ([td["qb"]] if td.get("qb") else [])
    rows = []
    for nm in names:
        for cat, t in _stat_line(summary, nm or "")[:2]:
            rows.append((f"<b>{H.escape(short_name(nm) if len(names) > 1 else nm)}</b>", H.escape(t)))
    secs["line"] = _fx("TODAY'S LINE", rows[:3])
    # 3) league players in this game
    players = sleeper_players("nfl")
    here = {sl_team(t["team"]["abbreviation"]) for t in teams_of(event)}
    skip = {str(_sl_pid(n, abbr) or "") for n in names if n}
    lst = []
    for rid, x in live["rosters"].items():
        for pid in x.get("starters") or []:
            pl = players.get(pid) or {}
            if (pl.get("team") or pid) in here and str(pid) not in skip:
                nm = f"{pid} D/ST" if str(pid).isalpha() and str(pid).isupper() else (pl.get("full_name") or pid)
                lst.append((float(x["pp"].get(pid) or 0), nm, x["manager"]))
    lst.sort(reverse=True)
    secs["league"] = _fx("LEAGUE PLAYERS IN THIS GAME",
                         [(f"<b>{H.escape(short_name(nm) if ' ' in nm and 'D/ST' not in nm else nm)}</b> {pts:.1f}",
                           H.escape(mg)) for pts, nm, mg in lst[:3]])
    # 4) waiver watch (unrostered scorer)
    if not owned and td.get("scorer") and not td.get("sub"):
        pid = _sl_pid(td["scorer"], abbr)
        rows = [("Unrostered", "<span class='g'>free agent</span>")]
        rk = position_ranks("nfl").get(str(pid)) if pid else None
        if rk:
            pos, n = rk
            note = ("worth a pick-up" if n <= (12 if pos in ("QB", "TE") else 24) else
                    "deep-league flier" if n <= 40 else "watch, don't chase")
            rows.append((f"<b>{pos}{n}</b> this season", f"<span class='g'>{note}</span>"))
        try:
            hit = _wb_cached(("trend",), 1800, lambda: fetch_url(f"{SLEEPER}/players/nfl/trending/add?lookback_hours=24&limit=200"))
            adds = next((x["count"] for x in hit or [] if str(x.get("player_id")) == str(pid)), None)
            if adds:
                rows.append(("Sleeper adds (24h)", f"<b>{adds:,}</b>"))
        except Exception:  # noqa: BLE001
            pass
        try:
            wk = int(live.get("week") or 0) + 1
            evs = _wb_cached(("nextwk", wk), 6 * 3600, lambda: fetch(f"football/nfl/scoreboard?seasontype=2&week={wk}").get("events", []))
            for e2 in evs:
                ts = teams_of(e2)
                if abbr in [t["team"]["abbreviation"] for t in ts]:
                    other = next(t for t in ts if t["team"]["abbreviation"] != abbr)
                    at = "vs" if other.get("homeAway") == "away" else "at"
                    rows.append(("Next week", f"{at} <b>{other['team']['abbreviation']}</b> · "
                                 f"{parse_time(e2['date']).astimezone(UK):%a %H:%M}"))
                    break
        except Exception:  # noqa: BLE001
            pass
        secs["waiver"] = _fx(f"WAIVER WATCH · {td['scorer'].upper()}", rows)
    seed = int(re.sub(r"\D", "", str(play.get("id", "0")))[-3:] or 0)
    mid = ["line", "league"] if seed % 2 == 0 else ["league", "line"]
    order = ["game"] + (["waiver"] if "waiver" in secs else []) + mid
    return "".join(secs.get(k) or "" for k in order)


_nfl_td_html_fill = nfl_td_fantasy_html


def nfl_td_fantasy_html(event, play, summary, live, ol, base, *a, **k):
    h = _nfl_td_html_fill(event, play, summary, live, ol, base, *a, **k)
    if not (CONFIG.get("nfl") or {}).get("td_fill", True):
        return h
    try:
        td = td_parse(play)
        owned = league_owner(boxscore_id(summary, td.get("scorer") or ""), td.get("scorer") or "")
        extra = _fill_sections(event, play, summary, live, td, owned)
        if not extra or "<div class='imp'>" not in h:
            return h
        i = h.find("<div class='imp'>")
        j = _div_end(h, i)
        imp = h[i:j]
        if "WAIVER WATCH" in extra and imp.count("<div class='im'>") == 1 and "🆓" in imp:
            h = h[:i] + extra + h[j:]                          # the waiver section says it (and more)
        else:
            h = h[:j] + extra + h[j:]
        if "<div class='rcol'>" not in h:                  # older layout: keep the extras next to the impact panel
            pass
        h = h.replace("</style>", FILL_CSS + "</style>", 1).replace("<head>", "<head>" + FILL_JS, 1)
        _last_td_html["h"] = h
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"touchdown extras skipped: {ex}")
    return h



# ------------------------------------------------ MISSED / BLOCKED kicks in crunch time (one-score game, last 5:00 of
#   Q4 or any time in OT): missed or blocked field goals and extra points, and a failed 2-pt try that would have tied it
#   or taken the lead. Dramatic red card (flash + shake when animated), still first then GIF like every big moment.
MISS_CSS = """
.bg{opacity:1!important;background:radial-gradient(circle at 50% 30%,rgba(255,40,40,.42),rgba(60,0,8,.78) 55%,rgba(8,4,8,.96) 100%)!important}
.hl{border-left-color:#ff3d3d!important}
.sbx .mid .yd{color:#ff5a5a!important;text-shadow:0 0 18px rgba(255,40,40,.9),0 0 40px rgba(255,0,0,.6)}
.sbx .mid .st{background:#ff3d3d!important;color:#fff!important}
.mkx{margin-top:10px;display:flex;align-items:center;gap:14px;white-space:nowrap;padding:10px 16px;border-radius:14px;
 background:linear-gradient(100deg,rgba(255,61,61,.32),rgba(120,0,0,.4));box-shadow:0 0 0 3px #ff3d3d,0 0 34px rgba(255,40,40,.7);
 font-size:20px;font-weight:800;color:#fff}
.mkx b{font-size:24px;font-weight:900;color:#ffd166}.mkx .gc{font-size:16px;font-weight:900;letter-spacing:1.6px;background:#ff3d3d;
 color:#fff;border-radius:7px;padding:3px 9px;flex:none}
.pp5.mk{box-shadow:0 0 0 3px #ff3d3d,0 0 30px rgba(255,40,40,.65)!important;background:linear-gradient(120deg,rgba(70,6,12,.9),rgba(9,12,22,.75))!important}
.pp5.mk .gn b{color:#ff6b6b!important}
"""
ANIM_CSS += """
@keyframes a_mkshake{0%,100%{transform:translateX(0)}20%{transform:translateX(-6px)}40%{transform:translateX(6px)}60%{transform:translateX(-4px)}80%{transform:translateX(4px)}}
@keyframes a_mkflash{0%,100%{opacity:1}50%{opacity:.55}}
.mkc .sbx .mid .yd{animation:a_mkshake .5s ease-in-out infinite}
.mkx{animation:a_pulse 1s ease-in-out infinite}
.mkc .bg{animation:a_mkflash 1s ease-in-out infinite}
"""
_wants_animation_mk = wants_animation


def wants_animation(html_text):
    return _wants_animation_mk(html_text) or ("class='mkx'" in html_text and (CONFIG.get("animations") or {}).get("enabled", True))


def _miss_drive_team(summary, pid):
    dr = summary.get("drives") or {}
    for dv in list(dr.get("previous") or []) + ([dr["current"]] if dr.get("current") else []):
        if any(str(p.get("id")) == str(pid) for p in dv.get("plays") or []):
            return (dv.get("team") or {}).get("abbreviation")
    return None


def missed_kicks(summary, event):
    """[(key, play, info)] for crunch-time missed / blocked kicks (and decisive failed 2-pt tries) in this game."""
    out = []
    away, home = teams_of(event)
    names = []
    for t in (summary.get("boxscore") or {}).get("players") or []:
        ab = (t.get("team") or {}).get("abbreviation", "")
        for cat in t.get("statistics") or []:
            for a in cat.get("athletes") or []:
                if (a.get("athlete") or {}).get("displayName"):
                    names.append((a["athlete"]["displayName"], ab))
    for p in _all_plays(summary):
        q = (p.get("period") or {}).get("number", 0)
        ck = p.get("clock") or {}
        mm = re.match(r"(\d+):(\d+)", ck.get("displayValue") or "")
        secs = float(ck["value"]) if ck.get("value") is not None else (int(mm.group(1)) * 60 + int(mm.group(2)) if mm else 9999)
        if not (q >= 5 or (q == 4 and secs <= 300)):
            continue
        tt = ((p.get("type") or {}).get("text") or "").lower()
        text = p.get("text") or ""
        a1, h1 = int(num(p.get("awayScore"))), int(num(p.get("homeScore")))
        team = _miss_drive_team(summary, p.get("id")) or ""
        info = None
        if "field goal" in tt and ("missed" in tt or "blocked" in tt or "no good" in text.lower()):
            m = re.search(rf"({_ABBR}) (\d+) yard field goal", text)
            blk = re.search(rf"BLOCKED by ({_ABBR})", text)
            why = re.search(r"No Good, ([A-Za-z ]+?)(?:,|\.|$)", text)
            info = {"kind": "fg", "yds": int(m.group(2)) if m else None, "kicker": m.group(1) if m else "",
                    "blocked": bool(blk) or "blocked" in tt, "blocker": blk.group(1) if blk else None,
                    "why": why.group(1).strip().lower() if why else "", "team": team, "before": (a1, h1)}
            key = str(p.get("id"))
        elif "touchdown" in tt:
            pa = (p.get("pointAfterAttempt") or {})
            pat = (pa.get("text") or "").lower()
            if "extra point" in pat and ("miss" in pat or "block" in pat or "no good" in pat):
                m = re.search(rf"({_ABBR}) extra point is (?:No Good|BLOCKED)", text)
                blk = re.search(rf"extra point is BLOCKED[^.]*?by ({_ABBR})", text) or re.search(rf"BLOCKED by ({_ABBR})", text)
                why = re.search(r"extra point is No Good, ([A-Za-z ]+?)(?:,|\.|$)", text)
                info = {"kind": "xp", "yds": None, "kicker": m.group(1) if m else "", "blocked": "block" in pat,
                        "blocker": blk.group(1) if blk else None, "why": why.group(1).strip().lower() if why else "",
                        "team": team, "before": (a1, h1)}
            elif "two point" in pat and not pa.get("value"):
                mine, theirs = (a1, h1) if team == away["team"]["abbreviation"] else (h1, a1)
                if -2 <= mine - theirs <= 0:                       # the try would have tied it or taken the lead
                    info = {"kind": "2pt", "yds": None, "kicker": "", "blocked": False, "blocker": None, "why": "",
                            "team": team, "before": (a1, h1)}
            key = f"{p.get('id')}-pat"
        if not info:
            continue
        if abs(a1 - h1) > 8:
            continue
        for k in ("kicker", "blocker"):
            if info.get(k):
                info[k] = _full_name(info[k], names, team if k == "kicker" else "")
        out.append((key, p, info))
    return out


def nfl_miss_html(event, play, summary, live, ol, info):
    import html as H
    away, home = teams_of(event)
    abbr = info["team"]
    kt = away if away["team"]["abbreviation"] == abbr else home
    other = home if kt is away else away
    nick = kt["team"].get("name") or abbr
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    q = (play.get("period") or {}).get("number", 0)
    clk = (play.get("clock") or {}).get("displayValue", "")
    when = ("OT" if q > 4 else "Q4") + f" · {clk} LEFT"
    diff = abs(a_s - h_s)
    state = "STILL TIED" if diff == 0 else "STILL A ONE-SCORE GAME"
    word = "BLOCKED" if info["blocked"] else ("NO GOOD" if info["kind"] == "2pt" else "MISSED")
    icon = "🚫" if info["blocked"] else "❌"
    big = {"fg": f"{icon} {word} {info['yds']}-YD FG" if info.get("yds") else f"{icon} {word} FG",
           "xp": f"{icon} {word} EXTRA POINT", "2pt": "❌ 2-PT TRY FAILS"}[info["kind"]]
    sc = league_scoring("nfl")
    proj = nfl_projections(live["week"])
    rows = []
    panel = ""
    if info["kind"] in ("fg", "xp") and info["kicker"]:
        kicker = info["kicker"]
        k = next((x for x in nfl_kickers(summary) if name_key(x["name"]) == name_key(kicker)), None)
        kid = k["id"] if k else boxscore_id(summary, kicker)
        o = league_owner(kid, kicker)
        lost = kicker_fpts(info["yds"] or 30) if info["kind"] == "fg" else float(sc.get("xpm") or 1)
        pen = float(sc.get("fgmiss" if info["kind"] == "fg" else "xpmiss") or 0)
        now = k["pts"] if k else None
        own = (f"<b>{H.escape(o['manager'])}</b> {'⭐ starting' if o['starting'] else '· bench'}" if o
               else "<b>Unrostered</b> — free agent")
        why = f" · {H.escape(info['why'])}" if info.get("why") else ""
        panel = (f"<div class='pair one'><div class='pp5 mk'><img src='{H.escape(nfl_headshot(kid) or kt['team'].get('logo', ''))}'"
                 f"{pid_attr(_sl_pid(kicker, abbr))}><div><div class='role'>KICKER · {abbr}</div><div class='nm'>{H.escape(kicker)}</div>"
                 f"<div class='gn'><b>{(f'+{pen:g}' if pen > 0 else f'{pen:g}')}</b><span>no +{lost:g}{why}</span></div>"
                 + (f"<div class='pjn'>now <b>{fmt_pts(now)}</b> pts</div>" if now is not None else "")
                 + f"<div class='ow3'>{own}</div></div></div></div>")
        if o:
            rid = next((r for r, x in live["rosters"].items() if x["manager"] == o["manager"]), None)
            if rid in ol:
                rows.append(f"<div class='im'><span class='ar dn'>▼</span><b class='who5'>{H.escape(o['manager'])}</b>"
                            f"<span>misses out on <b class='dn'>+{lost:g}</b> from {H.escape(short_name(kicker))}</span>"
                            f"<span>win <b>{ol[rid]['win']:.0f}%</b></span><span></span></div>")
    if info["blocked"]:
        od = league_owner(f"def:{sl_team(other['team']['abbreviation'])}", "")
        b = float(sc.get("blk_kick") or 0)
        if od and b:
            rows.append(f"<div class='im'><span class='ar u'>▲</span><b class='who5'>{H.escape(od['manager'])}</b>"
                        f"<span>{H.escape(other['team'].get('name') or '')} D/ST <b class='u'>+{b:g}</b> for the block"
                        + (f" ({H.escape(info['blocker'])})" if info.get("blocker") else "") + "</span><span></span><span></span></div>")
    sw = _win_swing(summary, play.get("id"), kt is home)
    strip = "<div class='mkx'><span class='gc'>CRUNCH TIME</span>"
    if sw and abs(sw[1] - sw[0]) >= 1:
        strip += f"{H.escape(nick)} win chance <b>{sw[0]:.0f}% → {sw[1]:.0f}%</b>"
    else:
        strip += f"<b>{H.escape(clk)}</b> left · {diff}-point game"
    strip += "</div>"
    if not rows:
        rows.append("<div class='im'><span class='ar'>·</span><b class='who5'>No league points</b>"
                    "<span>change hands on this one</span><span></span><span></span></div>")
    imp = f"<div class='imp'><div class='ih'>FANTASY IMPACT</div>{''.join(rows)}</div>"
    try:                                                  # spare space: game state + league players (dropped if no room)
        imp += _fill_sections(event, play, summary, live, {"scorer": info.get("kicker") or "", "sub": "kick"}, True)
    except Exception:  # noqa: BLE001
        pass
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>{word} KICK · {'OT' if q > 4 else 'Q' + str(q)} {clk} · "
            f"{away['team']['abbreviation']} {a_s}–{h_s} {home['team']['abbreviation']}</div><div class='pill'>{state}</div></div>"
            + score_hero(event, a_s, h_s, when, state, hot=None, plus=None, big=big)
            + (panel or "") + strip + imp)
    return _card("--a:#ff3d3d;--h:#ffd166", body, extra_css=V4_CSS + TD5_CSS + FG_CSS + SB_CSS + MISS_CSS + FILL_CSS).replace(
        "<div class='card'", "<div class='card mkc'", 1).replace("<head>", "<head>" + FILL_JS, 1)


def post_missed_kick(event, play, summary, ctx, info):
    away, home = teams_of(event)
    kt = away if away["team"]["abbreviation"] == info["team"] else home
    nick = kt["team"].get("name") or info["team"]
    clk = (play.get("clock") or {}).get("displayValue", "")
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    diff = abs(a_s - h_s)
    k = info.get("kicker") or nick
    yd = f" {info['yds']}-yarder" if info.get("yds") else ""
    if info["kind"] == "2pt":
        title = f"❌ NO GOOD! The {nick} two-point try fails — still {'tied' if not diff else f'a {diff}-point game'}!"
    elif info["blocked"]:
        title = f"🚫 BLOCKED! {k}'s{yd or ' extra point'} is blocked" + (f" by {info['blocker']}" if info.get("blocker") else "") + "!"
    else:
        title = f"❌ MISSED! {k}'s{yd or ' extra point'} is no good" + (f" — {info['why']}!" if info.get("why") else "!")
    desc = (f"Still tied" if not diff else f"Still a one-score game") + f" · {clk} left" + (" in OT" if (play.get("period") or {}).get("number", 0) > 4 else "")
    embed = {"author": AU_NFL(), "color": 0xFF3D3D, "img_title": title, "img_desc": desc, "title": title, "description": desc}
    html_text = nfl_miss_html(event, play, summary, ctx["live"], ctx["ol"], info)
    return _post_card(NFL_HOOK, html_text, f"❌ **{title}**", embed, "miss.png")


_nfl_prime_slot_miss = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    active = _nfl_prime_slot_miss(slot, st, getctx, force)
    if not (CONFIG.get("nfl") or {}).get("missed_kicks", True):
        return active
    for e in slot:
        if e["status"]["type"]["state"] != "in":
            continue
        g = st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})
        try:
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            for key, p, info in missed_kicks(summary, e):
                if key in g.setdefault("mk", []):
                    continue
                wc = p.get("wallclock")
                if wc and time.time() - _wallclock_ts(wc) > 600:
                    g["mk"].append(key)                     # old (restart): don't post late
                    continue
                t0 = _wallclock_ts(wc) if wc else g.setdefault("mk_seen", {}).setdefault(key, time.time())
                if not due(t0, "missed kick"):
                    continue
                ctx = getctx()
                if ctx and post_missed_kick(e, p, summary, ctx, info):
                    g["mk"].append(key)
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"missed-kick check skipped: {ex}")
    return active



# ---- one-off preview: config nfl.test_missed_kick = "<play id>" posts that missed kick once to #rick-only through the
#      real path (still first, then the GIF swap), ignoring the age limit. Done once (state "test_mk_done"), then ignored.
_run_nfl_testmk = run_nfl


def run_nfl(st, date=None, force=False):
    out = _run_nfl_testmk(st, date, force)
    try:
        _test_missed_kick(st)
    except Exception as ex:  # noqa: BLE001
        _log_plain(f"test missed kick skipped: {ex}")
    return out


def _test_missed_kick(st):
    global NFL_HOOK
    pid = str((CONFIG.get("nfl") or {}).get("test_missed_kick") or "").strip()
    done = st.setdefault("test_mk_done", [])
    if not pid or pid in done or not ALERT_HOOK or CAPTURE is not None or DRY_RUN:
        return
    done.append(pid)                                    # once, whatever happens
    week = int(fetch_url(f"{SLEEPER}/state/nfl").get("week") or 0)
    events = fetch(f"football/nfl/scoreboard?seasontype=2&week={week}").get("events", [])
    e = next((x for x in events if pid.startswith(str(x["id"]))), None)
    if e is None:
        _log_plain(f"test missed kick {pid}: game not found this week")
        return
    summary = fetch(f"football/nfl/summary?event={e['id']}")
    hit = next(((k, p, i) for k, p, i in missed_kicks(summary, e) if k in (pid, pid + "-pat")), None)
    ctx = nfl_ctx(st, events)
    if not hit or not ctx:
        _log_plain(f"test missed kick {pid}: not a crunch-time missed kick (or no league data)")
        return
    keep = NFL_HOOK
    NFL_HOOK = ALERT_HOOK
    try:
        ok = post_missed_kick(e, hit[1], summary, ctx, hit[2])
    finally:
        NFL_HOOK = keep
    _log_plain(f"test missed kick {pid}: posted to #rick-only ({ok})")



# ------------------------------------------------ crunch-time TURNOVERS: on downs, interceptions, lost fumbles in a
#   one-score game with <= 2:00 left in Q4 (or in OT). Two moods, by who gets the ball:
#   the team behind / level -> COMEBACK ON (bright, gold, pulsing); the team ahead -> GAME OVER-ish (dark, muted).
#   Plays the game-changer post already covered are skipped (and these are marked so it won't post them later).
_TO_SKIP = ("timeout", "end period", "end of", "two-minute", "penalty")
TOD_CSS = """
.sbx .mid .yd{font-size:42px!important;white-space:nowrap}
.tox{margin-top:10px;display:flex;align-items:center;gap:14px;white-space:nowrap;padding:10px 16px;border-radius:14px;
 font-size:20px;font-weight:800;color:#fff}
.tox b{font-size:24px;font-weight:900}.tox .gc{font-size:16px;font-weight:900;letter-spacing:1.6px;border-radius:7px;padding:3px 9px;flex:none}
.pp5.to .gn b{font-size:40px!important}
"""
TOD_ON_CSS = """
.bg{opacity:1!important;background:radial-gradient(circle at 50% 28%,rgba(255,214,0,.42),rgba(255,120,0,.22) 40%,rgba(10,14,26,.92) 80%)!important}
.hl{border-left-color:#ffd600!important}
.sbx .mid .yd{color:#ffd600!important;text-shadow:0 0 18px rgba(255,190,0,.95),0 0 44px rgba(255,120,0,.7)}
.sbx .mid .st{background:#ffd600!important;color:#1b1305!important}
.tox{background:linear-gradient(100deg,rgba(255,214,0,.3),rgba(255,120,0,.22));box-shadow:0 0 0 3px #ffd600,0 0 34px rgba(255,170,0,.75)}
.tox b{color:#ffd600}.tox .gc{background:#ffd600;color:#1b1305}
.pp5.to{box-shadow:0 0 0 3px #ffd600,0 0 30px rgba(255,190,0,.6)!important}
"""
TOD_OVER_CSS = """
.bg{opacity:1!important;background:linear-gradient(180deg,rgba(20,22,30,.9),rgba(4,5,9,.98))!important;filter:saturate(.5)}
.hl{border-left-color:#6b7280!important}
.sbx .mid .yd{color:#e5e7eb!important;text-shadow:0 0 14px rgba(0,0,0,.9)}
.sbx .mid .st{background:#374151!important;color:#e5e7eb!important}
.tox{background:rgba(31,41,55,.85);box-shadow:0 0 0 2px #4b5563}
.tox b{color:#e5e7eb}.tox .gc{background:#111827;color:#e5e7eb;box-shadow:inset 0 0 0 1.5px #6b7280}
.pp5.to{box-shadow:0 0 0 2px #4b5563!important}.pp5.to .gn b{color:#e5e7eb!important}
"""
ANIM_CSS += """
@keyframes a_tofade{0%,100%{opacity:1}50%{opacity:.7}}
.toc.on .sbx .mid .yd{animation:a_mkshake .6s ease-in-out infinite}
.toc.on .tox{animation:a_pulse 1s ease-in-out infinite}
.toc.over .sbx .mid .yd{animation:a_tofade 2s ease-in-out infinite}
"""
_wants_animation_to = wants_animation


def wants_animation(html_text):
    return _wants_animation_to(html_text) or ("class='tox'" in html_text and (CONFIG.get("animations") or {}).get("enabled", True))


def _secs_left(p):
    ck = p.get("clock") or {}
    if ck.get("value") is not None:
        return float(ck["value"])
    m = re.match(r"(\d+):(\d+)", ck.get("displayValue") or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else 9999


def crunch_turnovers(summary, event):
    """[(play id, play, info)]: turnovers on downs / interceptions / lost fumbles, <= 2:00 left in Q4 or in OT,
    one-score game. info: kind, offense (lost it), defense (got it), defender, passer."""
    away, home = teams_of(event)
    abbrs = [away["team"]["abbreviation"], home["team"]["abbreviation"]]
    names = []
    for t in (summary.get("boxscore") or {}).get("players") or []:
        ab = (t.get("team") or {}).get("abbreviation", "")
        for cat in t.get("statistics") or []:
            for a in cat.get("athletes") or []:
                if (a.get("athlete") or {}).get("displayName"):
                    names.append((a["athlete"]["displayName"], ab))
    dr = summary.get("drives") or {}
    seen, out = set(), []
    for dv in list(dr.get("previous") or []) + ([dr["current"]] if dr.get("current") else []):
        if id(dv) in seen:
            continue
        seen.add(id(dv))
        off = (dv.get("team") or {}).get("abbreviation", "")
        if off not in abbrs:
            continue
        de = abbrs[1] if off == abbrs[0] else abbrs[0]
        res = (dv.get("result") or dv.get("shortDisplayResult") or "").upper()
        plays = [p for p in dv.get("plays") or [] if not any(k in ((p.get("type") or {}).get("text") or "").lower() for k in _TO_SKIP)]
        hit = None
        if res == "DOWNS" and plays:
            hit = (plays[-1], "downs")
        else:
            for p in plays:
                tt = (p.get("type") or {}).get("text") or ""
                if "touchdown" in tt.lower():
                    continue
                if "Interception" in tt:
                    hit = (p, "int")
                elif "Fumble Recovery (Opponent)" in tt or ("fumble" in tt.lower() and "opponent" in tt.lower()):
                    hit = (p, "fumble")
        if not hit:
            continue
        p, kind = hit
        q = (p.get("period") or {}).get("number", 0)
        if not (q >= 5 or (q == 4 and _secs_left(p) <= 120)):
            continue
        a1, h1 = int(num(p.get("awayScore"))), int(num(p.get("homeScore")))
        if abs(a1 - h1) > 8:
            continue
        text = p.get("text") or ""
        who = None
        if kind == "int":
            m = re.search(rf"INTERCEPTED by ({_ABBR})", text)
            who = m.group(1) if m else None
        elif kind == "fumble":
            m = re.search(rf"RECOVERED by [A-Z]{{2,3}}-({_ABBR})", text)
            who = m.group(1) if m else None
        else:
            m = re.search(rf"sacked [^(]*\(({_ABBR})", text) or re.search(rf"\(({_ABBR})(?:[;,)])", text)
            who = m.group(1) if m else None
        qb = re.search(rf"({_ABBR}) (?:pass|sacked|scrambles)", text)
        info = {"kind": kind, "off": off, "def": de, "sack": " sacked" in text,
                "defender": _full_name(who, names, de) if who else None,
                "passer": _full_name(qb.group(1), names, off) if qb else None}
        out.append((str(p.get("id")), p, info))
    return out


def nfl_turnover_html(event, play, summary, live, ol, info):
    import html as H
    away, home = teams_of(event)
    dt = away if away["team"]["abbreviation"] == info["def"] else home        # got the ball
    ot = home if dt is away else away
    dnick, onick = dt["team"].get("name") or info["def"], ot["team"].get("name") or info["off"]
    a_s, h_s = int(num(play.get("awayScore"))), int(num(play.get("homeScore")))
    mine, theirs = (a_s, h_s) if dt is away else (h_s, a_s)
    q = (play.get("period") or {}).get("number", 0)
    clk = (play.get("clock") or {}).get("displayValue", "")
    sw = _win_swing(summary, play.get("id"), dt is home)
    behind = theirs - mine
    on = behind >= 0
    need = ("needs a score to win" if behind == 0 else "needs a FG to win" if behind <= 2 else "needs a FG to tie"
            if behind == 3 else "needs a TD to win" if behind <= 6 else "needs a TD to tie" if behind == 7 else "needs a TD + 2")
    over = (not on) and (sw is None or sw[1] >= 97)
    big = {"downs": "STOPPED ON 4TH!" if on else ("TURNED OVER" if over else "STOPPED ON 4TH!"),
           "int": "INTERCEPTED!", "fumble": "FUMBLE! TURNOVER!"}[info["kind"]]
    big = ("🔥 " if on else ("💀 " if over else "🛑 ")) + big
    when = ("OT" if q > 4 else "Q4") + f" · {clk} LEFT"
    sub = (f"{info['def']} BALL · {need.upper()}" if on else
           ("THAT'S THE BALLGAME" if over else f"{info['def']} BALL · CAN RUN IT DOWN"))
    sc = league_scoring("nfl")
    rows = []
    # D/ST that made the stop
    od = league_owner(f"def:{sl_team(info['def'])}", "")
    pts = (float(sc.get("int") or 0) if info["kind"] == "int" else float(sc.get("fum_rec") or 0) if info["kind"] == "fumble" else 0.0)
    pts += float(sc.get("sack") or 0) if info.get("sack") else 0.0
    bits = " · ".join(x for x in (("INT" if info["kind"] == "int" else "fumble rec" if info["kind"] == "fumble" else ""),
                                   ("sack" if info.get("sack") else "")) if x)
    if od:
        rows.append(f"<div class='im'><span class='ar u'>▲</span><b class='who5'>{H.escape(od['manager'])}</b>"
                    f"<span>{H.escape(dnick)} D/ST " + (f"<b class='u'>+{pts:g}</b> ({bits})" if pts else "gets the stop")
                    + f"</span><span>{'⭐' if od['starting'] else ''}</span><span></span></div>")
    if info.get("passer") and info["kind"] in ("int", "fumble"):
        o = league_owner(boxscore_id(summary, info["passer"]), info["passer"])
        lose = float(sc.get("pass_int" if info["kind"] == "int" else "fum_lost") or 0)
        if o and lose:
            rows.append(f"<div class='im'><span class='ar dn'>▼</span><b class='who5'>{H.escape(o['manager'])}</b>"
                        f"<span>{H.escape(short_name(info['passer']))} <b class='dn'>{lose:g}</b> "
                        f"({'INT' if info['kind'] == 'int' else 'fumble lost'})</span><span></span><span></span></div>")
    if not rows:
        rows.append("<div class='im'><span class='ar'>·</span><b class='who5'>No league points</b>"
                    "<span>change hands on this one</span><span></span><span></span></div>")
    imp = f"<div class='imp'><div class='ih'>FANTASY IMPACT</div>{''.join(rows)}</div>"
    try:
        imp += _fill_sections(event, dict(play, team={"abbreviation": info["def"]}), summary, live,
                              {"scorer": info.get("defender") or "", "sub": "to"}, True)
    except Exception:  # noqa: BLE001
        pass
    panel = ""
    if info.get("defender"):
        did = boxscore_id(summary, info["defender"])
        verb = {"int": "picks it off", "fumble": "recovers the fumble"}.get(info["kind"], "makes the stop on 4th down")
        panel = (f"<div class='pair one'><div class='pp5 to'><img src='{H.escape(nfl_headshot(did) or dt['team'].get('logo', ''))}'>"
                 f"<div><div class='role'>DEFENCE · {info['def']}</div><div class='nm'>{H.escape(info['defender'])}</div>"
                 + (f"<div class='gn'><b>+{pts:g}</b><span>{H.escape(verb)}</span></div>" if pts else
                    f"<div class='gn'><b>STOP</b><span>{H.escape(verb.replace('makes the stop ', ''))}</span></div>")
                 + f"<div class='ow3'>for the <b>{H.escape(dnick)} D/ST</b></div></div></div></div>")
    strip = f"<div class='tox'><span class='gc'>{'COMEBACK ON' if on else ('GAME OVER' if over else 'BIG STOP')}</span>"
    if sw and abs(sw[1] - sw[0]) >= 1:
        strip += f"{H.escape(dnick)} win chance <b>{sw[0]:.0f}% → {sw[1]:.0f}%</b>"
    else:
        strip += f"<b>{H.escape(clk)}</b> left · {abs(behind)}-point game"
    strip += "</div>"
    body = (f"<div class='top'><div class='lg'><img src='{NFL_LOGO}'>TURNOVER · {'OT' if q > 4 else 'Q' + str(q)} {clk} · "
            f"{away['team']['abbreviation']} {a_s}–{h_s} {home['team']['abbreviation']}</div>"
            f"<div class='pill'>{'COMEBACK ON' if on else ('GAME OVER' if over else 'BIG STOP')}</div></div>"
            + score_hero(event, a_s, h_s, when, sub, hot=None, plus=None, big=big) + panel + strip + imp)
    mood = "on" if on else "over"
    css = V4_CSS + TD5_CSS + FG_CSS + SB_CSS + TOD_CSS + (TOD_ON_CSS if on else TOD_OVER_CSS) + FILL_CSS
    html_text = _card(f"--a:{'#ffd600' if on else '#6b7280'};--h:#ffd166", body, extra_css=css)
    return (html_text.replace("<div class='card'", f"<div class='card toc {mood}'", 1).replace("<head>", "<head>" + FILL_JS, 1),
            on, over, need, dnick, onick, sw)


def post_turnover(event, play, summary, ctx, info):
    html_text, on, over, need, dnick, onick, sw = nfl_turnover_html(event, play, summary, ctx["live"], ctx["ol"], info)
    clk = (play.get("clock") or {}).get("displayValue", "")
    what = {"downs": "STOPPED ON 4TH", "int": "INTERCEPTED", "fumble": "FUMBLE"}[info["kind"]]
    if on:
        title = f"🔥 {what}! {dnick} get the ball back · {clk} left · {need}"
    elif over:
        title = (f"💀 {'Turned over on downs' if info['kind'] == 'downs' else what.title()} — that's the ballgame for the {onick}"
                 if info["kind"] == "downs" else f"💀 {what.title()}! That's the ballgame for the {onick}")
    else:
        title = f"🛑 {what}! {dnick} take over · {clk} left — the {onick} need a stop"
    desc = f"{dnick} win chance {sw[0]:.0f}% → {sw[1]:.0f}%" if sw else ""
    embed = {"author": AU_NFL(), "color": 0xFFD600 if on else 0x374151, "img_title": title, "img_desc": desc,
             "title": title, "description": desc}
    return _post_card(NFL_HOOK, html_text, f"**{title}**", embed, "turnover.png")


_nfl_prime_slot_to = nfl_prime_slot


def nfl_prime_slot(slot, st, getctx, force=False):
    active = _nfl_prime_slot_to(slot, st, getctx, force)
    if not (CONFIG.get("nfl") or {}).get("turnover_alerts", True):
        return active
    for e in slot:
        if e["status"]["type"]["state"] != "in":
            continue
        g = st["nfl"].setdefault(e["id"], {"date": e["date"][:10], "tds": [], "pending": {}})
        try:
            summary = fetch(f"football/nfl/summary?event={e['id']}")
            for key, p, info in crunch_turnovers(summary, e):
                if key in g.setdefault("tod", []) or key in g.setdefault("gc", []):
                    continue
                wc = p.get("wallclock")
                if wc and time.time() - _wallclock_ts(wc) > 600:
                    g["tod"].append(key)
                    continue
                t0 = _wallclock_ts(wc) if wc else g.setdefault("tod_seen", {}).setdefault(key, time.time())
                if not due(t0, "turnover"):
                    continue
                ctx = getctx()
                if ctx and post_turnover(e, p, summary, ctx, info):
                    g["tod"].append(key)
                    g["gc"].append(key)                     # the game-changer post won't do it again
        except Exception as ex:  # noqa: BLE001
            _log_plain(f"turnover check skipped: {ex}")
    return active


if __name__ == "__main__":
    main()
