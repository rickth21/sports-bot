#!/usr/bin/env python3
"""
Play out the rest of tonight's GB @ LAR with a made-up script through the bot's real post code:
pre-game preview, every touchdown post, half-time, full-time and the next day's 14:00 playoff race.
Real league data (owners, matchups, odds, projections); the live Sleeper points are replaced with the
simulated ones so the matchup scores and odds move with the game.

  python3 tools/sim_rest.py out.json
"""
import copy
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

EVENT_ID = "401873079"   # GB @ LAR
bot.CAPTURE = []
bot.NFL_HOOK = "sim"

sb = bot.fetch("football/nfl/scoreboard")
event0 = next(e for e in sb["events"] if e["id"] == EVENT_ID)
OTHERS = [e for e in sb["events"] if e["id"] != EVENT_ID]   # rest of the week, still to play
comp = {c["homeAway"]: c for c in event0["competitions"][0]["competitors"]}
TEAM = {"GB": {"id": comp["away"]["team"]["id"], "abbreviation": "GB"},
        "LAR": {"id": comp["home"]["team"]["id"], "abbreviation": "LAR"}}

ids = {}
for t in TEAM.values():
    for g in bot.fetch(f"football/nfl/teams/{t['id']}/roster")["athletes"]:
        for a in g["items"]:
            ids[a["displayName"]] = str(a["id"])

FINAL = {
    "Jordan Love":       ("GB",  {"pass": (21, 32, 268, 1), "rush": (3, 14)}),
    "Matthew Golden":    ("GB",  {"rec": (5, 88)}),
    "Tucker Kraft":      ("GB",  {"rec": (6, 61)}),
    "Christian Watson":  ("GB",  {"rec": (4, 79)}),
    "Kaleb Johnson":     ("GB",  {"rush": (14, 58), "rec": (2, 15)}),
    "Matthew Stafford":  ("LAR", {"pass": (25, 37, 281, 1)}),
    "Kyren Williams":    ("LAR", {"rush": (18, 71), "rec": (3, 24)}),
    "Blake Corum":       ("LAR", {"rush": (7, 31)}),
    "Konata Mumpfield":  ("LAR", {"rec": (6, 84)}),
    "Colby Parkinson":   ("LAR", {"rec": (3, 30)}),
}
KICKER = {"GB": "Trey Smack", "LAR": "Brandon McManus"}

# (team, kind, quarter, clock, scoring text, play-by-play, down text, share of game played)
STEPS = [
    ("LAR", "FG", 1, "6:12", "Brandon McManus 43 Yd Field Goal", "", "", 0.12),
    ("GB", "TD", 2, "11:02", "Matthew Golden 27 Yd pass from Jordan Love (Trey Smack Kick)",
     "(Shotgun) J.Love pass deep left to M.Golden for 27 yards, TOUCHDOWN.", "3rd & 4 at LAR 27", 0.32),
    ("LAR", "TD", 2, "5:40", "Kyren Williams 3 Yd Rush (Brandon McManus Kick)",
     "K.Williams left guard for 3 yards, TOUCHDOWN.", "2nd & Goal at GB 3", 0.42),
    ("GB", "FG", 2, "0:04", "Trey Smack 48 Yd Field Goal", "", "", 0.50),
    ("HALF",),
    ("LAR", "TD", 3, "9:12", "Konata Mumpfield 22 Yd pass from Matthew Stafford (Brandon McManus Kick)",
     "(Shotgun) M.Stafford pass short right to K.Mumpfield for 22 yards, TOUCHDOWN.", "3rd & 8 at GB 22", 0.60),
    ("GB", "TD", 3, "2:30", "Tucker Kraft 11 Yd pass from Jordan Love (Trey Smack Kick)",
     "J.Love pass short middle to T.Kraft for 11 yards, TOUCHDOWN.", "1st & 10 at LAR 11", 0.73),
    ("LAR", "FG", 4, "6:15", "Brandon McManus 51 Yd Field Goal", "", "", 0.85),
    ("GB", "TD", 4, "1:48", "Christian Watson 34 Yd pass from Jordan Love (Trey Smack Kick)",
     "(Shotgun) J.Love pass deep right to C.Watson for 34 yards, TOUCHDOWN.", "3rd & 6 at LAR 34", 0.96),
]


def box(share, plays):
    """Box score at `share` of the game, with TDs and kicks from the scoring plays so far."""
    keys = {"passing": ["completions/passingAttempts", "passingYards", "passingTouchdowns", "interceptions"],
            "rushing": ["rushingAttempts", "rushingYards", "rushingTouchdowns"],
            "receiving": ["receptions", "receivingYards", "receivingTouchdowns"],
            "kicking": ["fieldGoalsMade/fieldGoalAttempts", "fieldGoalPct", "longFieldGoalMade",
                        "extraPointsMade/extraPointAttempts", "totalKickingPoints"]}
    ptd, rtd, rectd = {}, {}, {}
    fg, xp = {}, {}
    for p in plays:
        bot_td = bot.td_parse(p) if p["type"]["abbreviation"] == "TD" else None
        if bot_td:
            if bot_td["kind"] == "pass":
                ptd[bot_td["qb"]] = ptd.get(bot_td["qb"], 0) + 1
                rectd[bot_td["scorer"]] = rectd.get(bot_td["scorer"], 0) + 1
            else:
                rtd[bot_td["scorer"]] = rtd.get(bot_td["scorer"], 0) + 1
            xp[p["team"]["abbreviation"]] = xp.get(p["team"]["abbreviation"], 0) + 1
        else:
            fg.setdefault(p["team"]["abbreviation"], []).append(int(p["text"].split()[2]))
    teams = {t: {k: [] for k in keys} for t in TEAM}
    for name, (team, s) in FINAL.items():
        ath = {"id": ids.get(name, name), "displayName": name}
        sc = lambda x: max(round(x * share), 0)  # noqa: E731
        if "pass" in s:
            c, a, y, i = s["pass"]
            teams[team]["passing"].append({"athlete": ath, "stats": [f"{sc(c)}/{sc(a)}", str(sc(y)), str(ptd.get(name, 0)),
                                                                      str(i if share >= 0.8 else 0)]})
        if "rush" in s:
            n, y = s["rush"]
            teams[team]["rushing"].append({"athlete": ath, "stats": [str(max(sc(n), 1)), str(sc(y)), str(rtd.get(name, 0))]})
        if "rec" in s:
            n, y = s["rec"]
            k = rectd.get(name, 0)
            teams[team]["receiving"].append({"athlete": ath, "stats": [str(max(sc(n), k)), str(sc(y)), str(k)]})
    for t, k in KICKER.items():
        f = fg.get(t, [])
        if f or xp.get(t):
            teams[t]["kicking"].append({"athlete": {"id": ids.get(k, k), "displayName": k}, "stats": [
                f"{len(f)}/{len(f)}", "100.0", str(max(f) if f else 0), f"{xp.get(t, 0)}/{xp.get(t, 0)}",
                str(3 * len(f) + xp.get(t, 0))]})
    for t, n in (("GB", 3), ("LAR", 2)):     # team sacks
        teams[t]["defensive"] = [{"athlete": {"id": f"sk{t}", "displayName": f"{t} pass rush"},
                                  "stats": ["0", "0", str(round(n * share)), "0", "0", "0", "0"]}]
    keys["defensive"] = ["totalTackles", "soloTackles", "sacks", "tacklesForLoss", "passesDefended", "QBHits",
                         "defensiveTouchdowns"]
    return {"players": [{"team": TEAM[t], "statistics": [{"name": c, "keys": keys[c], "athletes": v}
                                                          for c, v in cats.items() if v]} for t, cats in teams.items()]}


REAL_LIVE = bot.league_live("nfl")
_SIM = {"summary": None}


def sim_live(sport="nfl", week=None):
    """Real league, but this game's players score what the simulation says."""
    live = copy.deepcopy(REAL_LIVE)
    pts = {}
    if _SIM["summary"] is not None:
        npid = bot.nfl_name_pid()
        for p in bot.nfl_fantasy(_SIM["summary"]):
            pid = (p["id"][4:] if str(p.get("id", "")).startswith("def:")
                   else npid.get((bot.name_key(p["name"]), bot.sl_team(p["team"]))))
            if pid:
                pts[pid] = p["pts"]
    players = bot.sleeper_players("nfl")
    for x in live["rosters"].values():
        for pid in x["players"]:
            if (players.get(pid) or {}).get("team") in ("GB", "LAR") or pid in ("GB", "LAR"):
                new = pts.get(pid, 0.0)
                if pid in x["starters"]:
                    x["points"] += new - float(x["pp"].get(pid) or 0)
                x["pp"][pid] = new
    return live


bot.league_live = sim_live


def at(state, period=1, clock=900, score=(0, 0), name=None):
    e = copy.deepcopy(event0)
    for c in e["competitions"][0]["competitors"]:
        c["score"] = str(score[0] if c["homeAway"] == "away" else score[1])
    e["status"]["period"], e["status"]["clock"] = period, clock
    e["status"]["type"].update({"state": state, "completed": state == "post",
                                "name": name or {"pre": "STATUS_SCHEDULED", "in": "STATUS_IN_PROGRESS",
                                                 "post": "STATUS_FINAL"}[state]})
    for c in e["competitions"][0]["competitors"]:
        c["team"]["score"] = c["score"]
    e["competitors"] = e["competitions"][0]["competitors"]
    return e


def hdr(a_s, h_s):
    return {"competitions": [{"competitors": [{"team": {"abbreviation": "GB"}, "score": str(a_s)},
                                              {"team": {"abbreviation": "LAR"}, "score": str(h_s)}]}]}


def clock_secs(c):
    m, s = c.split(":")
    return int(m) * 60 + int(s)


def main(out):
    st = json.loads(bot.STATE_FILE.read_text()) if bot.STATE_FILE.exists() else {}
    st.setdefault("nfl", {})
    labels = []

    pre = at("pre")
    ctx = bot.nfl_ctx(st, [pre] + OTHERS)
    prev_race = {str(r): v["po"] for r, v in ctx["ol"].items()}
    bot.post_prime_preview([pre], st, ctx)
    labels.append("00:50 · pre-game preview (updated: position + projection on each player)")

    plays, a_s, h_s = [], 0, 0
    drives = []
    for n, step in enumerate(STEPS, 1):
        if step[0] == "HALF":
            _SIM["summary"] = {"boxscore": box(0.5, plays), "scoringPlays": plays, "header": hdr(a_s, h_s)}
            e = at("in", 2, 0, (a_s, h_s), "STATUS_HALFTIME")
            ctx = bot.nfl_ctx(st, [e] + OTHERS)
            bot.post_prime_half(e, _SIM["summary"], ctx)
            labels.append(f"half-time · GB {a_s}–{h_s} LAR")
            continue
        team, kind, q, clock, text, detail, down, share = step
        pts = 3 if kind == "FG" else 7
        if team == "GB":
            a_s += pts
        else:
            h_s += pts
        pid = f"{EVENT_ID}{n:04d}"
        play = {"id": pid, "type": {"abbreviation": kind, "text": "Touchdown" if kind == "TD" else "Field Goal"},
                "text": text, "awayScore": a_s, "homeScore": h_s, "period": {"number": q},
                "clock": {"displayValue": clock}, "team": {"abbreviation": team}}
        plays.append(play)
        drives.append({"description": "", "team": {"abbreviation": team},
                       "plays": [{"id": pid, "text": detail, "start": {"downDistanceText": down}}]})
        if kind != "TD":
            continue
        summary = {"boxscore": box(share, plays), "scoringPlays": plays, "drives": {"previous": drives},
                   "header": hdr(a_s, h_s)}
        _SIM["summary"] = summary
        e = at("in", q, clock_secs(clock), (a_s, h_s))
        ctx = bot.nfl_ctx(st, [e] + OTHERS)
        bot.post_td_fantasy(e, play, summary, ctx)
        labels.append(f"Q{q} {clock} · touchdown · GB {a_s}–{h_s} LAR")

    final = at("post", 4, 0, (a_s, h_s))
    summary = {"boxscore": box(1.0, plays), "scoringPlays": plays, "header": hdr(a_s, h_s)}
    _SIM["summary"] = summary
    ctx = bot.nfl_ctx(st, [final] + OTHERS)
    bot.post_prime_final(final, summary, ctx)
    labels.append(f"full-time · GB {a_s}–{h_s} LAR")

    # next day 14:00 UK: playoff race (compared with before this game)
    fake_now = datetime(2026, 11, 26, 14, 0, tzinfo=bot.UK)
    html_text = bot.playoff_race_html(ctx, prev_race, fake_now.strftime("%a %-d %b"))
    ol, live = ctx["ol"], ctx["live"]
    mover = max(ol, key=lambda r: abs(ol[r]["po"] - prev_race.get(str(r), ol[r]["po"])))
    d = ol[mover]["po"] - prev_race[str(mover)]
    fav = max(ol, key=lambda r: ol[r]["title"])
    embed = {"title": (f"Biggest mover: {live['rosters'][mover]['manager']} {'▲' if d >= 0 else '▼'}{abs(d):.0f} "
                       f"to {ol[mover]['po']:.0f}%") if abs(d) >= 1 else "Playoff race update",
             "description": f"🏆 Title favourite: **{live['rosters'][fav]['manager']}** {ol[fav]['title']:.0f}%"}
    bot._post_card("sim", html_text, "", embed, "race.png")
    labels.append("14:00 · daily playoff race")

    Path(out).write_text(json.dumps(bot.CAPTURE, ensure_ascii=False))
    Path(out).with_suffix(".labels.json").write_text(json.dumps(labels))
    print(f"{len(bot.CAPTURE)} posts -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sim_posts.json")
