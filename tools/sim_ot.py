#!/usr/bin/env python3
"""
Play out the rest of tonight's GB @ LAR from the real half-time score into OVERTIME, through the bot's real
post code: half-time, touchdowns, late field goals, the overtime post, overtime scores, the walk-off,
full-time, then the next day's 14:00 playoff race. Real league data; the game script after 10-13 is made up.

  python3 tools/sim_ot.py out.json
"""
import copy
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

EVENT_ID = "401873079"   # GB @ LAR
bot.CAPTURE = []
bot.NFL_HOOK = "sim"

sb = bot.fetch("football/nfl/scoreboard")
event0 = next(e for e in sb["events"] if e["id"] == EVENT_ID)
OTHERS = [e for e in sb["events"] if e["id"] != EVENT_ID]
comp = {c["homeAway"]: c for c in event0["competitions"][0]["competitors"]}
TEAM = {"GB": {"id": comp["away"]["team"]["id"], "abbreviation": "GB"},
        "LAR": {"id": comp["home"]["team"]["id"], "abbreviation": "LAR"}}
ids = {}
for t in TEAM.values():
    for g in bot.fetch(f"football/nfl/teams/{t['id']}/roster")["athletes"]:
        for a in g["items"]:
            ids[a["displayName"]] = str(a["id"])

# end-of-game stat lines (touchdowns come from the scoring plays)
FINAL = {
    "Jordan Love":      ("GB",  {"pass": (26, 38, 318, 0), "rush": (3, 15)}),
    "Christian Watson": ("GB",  {"rec": (7, 118)}),
    "Matthew Golden":   ("GB",  {"rec": (4, 71)}),
    "Tucker Kraft":     ("GB",  {"rec": (6, 58)}),
    "Kaleb Johnson":    ("GB",  {"rush": (11, 31), "rec": (2, 25)}),
    "Matthew Stafford": ("LAR", {"pass": (25, 35, 251, 1)}),
    "Kyren Williams":   ("LAR", {"rush": (10, 44), "rec": (7, 41)}),
    "Blake Corum":      ("LAR", {"rush": (13, 52)}),
    "Konata Mumpfield": ("LAR", {"rec": (5, 81)}),
    "Colby Parkinson":  ("LAR", {"rec": (2, 15)}),
}
KICKER = {"GB": "Trey Smack", "LAR": "Brandon McManus"}

# the real first half (already posted live tonight)
REAL = [
    ("LAR", "FG", 1, "9:55", "Brandon McManus 43 Yd Field Goal"),
    ("LAR", "FG", 1, "3:12", "Brandon McManus 41 Yd Field Goal"),
    ("GB", "FG", 1, "1:28", "Trey Smack 54 Yd Field Goal"),
    ("LAR", "TD", 2, "11:29", "Kyren Williams 7 Yd Rush (Brandon McManus Kick)"),
    ("GB", "TD", 2, "8:06", "Christian Watson 6 Yd pass from Jordan Love (Trey Smack Kick)"),
]
# (label time UK, team, kind, quarter, clock, scoring text, play-by-play, down, share of game, what to post)
SCRIPT = [
    ("02:47", None, "HALF", 2, "0:00", None, None, None, 0.45, "half"),
    ("03:08", "LAR", "TD", 3, "9:12", "Konata Mumpfield 22 Yd pass from Matthew Stafford (Brandon McManus Kick)",
     "(Shotgun) M.Stafford pass short right to K.Mumpfield for 22 yards, TOUCHDOWN.", "3rd & 8 at GB 22", 0.55, "td"),
    ("03:21", "GB", "FG", 3, "4:01", "Trey Smack 38 Yd Field Goal", "", "", 0.64, None),
    ("03:31", "GB", "GC", 3, "2:10", None,
     "(Shotgun) M.Stafford pass short middle intended for K.Mumpfield INTERCEPTED by X.McKinney at GB 9. "
     "X.McKinney to GB 31 for 22 yards (K.Mumpfield).", "2nd & Goal at GB 9", 0.70, "gc"),
    ("03:44", "GB", "TD", 4, "6:44", "Tucker Kraft 11 Yd pass from Jordan Love (Trey Smack Kick)",
     "J.Love pass short middle to T.Kraft for 11 yards, TOUCHDOWN.", "1st & 10 at LAR 11", 0.80, "td"),
    ("04:01", "LAR", "FG", 4, "0:41", "Brandon McManus 47 Yd Field Goal", "", "", 0.90, "fg"),
    ("04:03", "GB", "FG", 4, "0:02", "Trey Smack 52 Yd Field Goal", "", "", 0.92, "fg"),
    ("04:06", None, "OT", 5, "10:00", None, None, None, 0.92, "ot"),
    ("04:14", "LAR", "FG", 5, "6:20", "Brandon McManus 41 Yd Field Goal", "", "", 0.96, "fg"),
    ("04:21", "GB", "TD", 5, "1:55", "Christian Watson 18 Yd pass from Jordan Love",
     "(Shotgun) J.Love pass deep left to C.Watson for 18 yards, TOUCHDOWN. Packers win!", "2nd & 6 at LAR 18", 1.0, "walkoff"),
]


def box(share, plays):
    keys = {"passing": ["completions/passingAttempts", "passingYards", "passingTouchdowns", "interceptions"],
            "rushing": ["rushingAttempts", "rushingYards", "rushingTouchdowns"],
            "receiving": ["receptions", "receivingYards", "receivingTouchdowns"],
            "kicking": ["fieldGoalsMade/fieldGoalAttempts", "fieldGoalPct", "longFieldGoalMade",
                        "extraPointsMade/extraPointAttempts", "totalKickingPoints"],
            "defensive": ["totalTackles", "soloTackles", "sacks", "tacklesForLoss", "passesDefended", "QBHits",
                          "defensiveTouchdowns"]}
    ptd, rtd, rectd, fg, xp = {}, {}, {}, {}, {}
    for p in plays:
        ab = p["team"]["abbreviation"]
        if p["type"]["abbreviation"] == "TD":
            td = bot.td_parse(p)
            if td["kind"] == "pass":
                ptd[td["qb"]] = ptd.get(td["qb"], 0) + 1
                rectd[td["scorer"]] = rectd.get(td["scorer"], 0) + 1
            else:
                rtd[td["scorer"]] = rtd.get(td["scorer"], 0) + 1
            if "Kick)" in p["text"]:
                xp[ab] = xp.get(ab, 0) + 1
        else:
            fg.setdefault(ab, []).append(int(p["text"].split()[2]))
    teams = {t: {k: [] for k in keys} for t in TEAM}
    sc = lambda x: max(round(x * share), 0)  # noqa: E731
    for name, (team, s) in FINAL.items():
        ath = {"id": ids.get(name, name), "displayName": name}
        if "pass" in s:
            c, a, y, i = s["pass"]
            teams[team]["passing"].append({"athlete": ath, "stats": [f"{sc(c)}/{sc(a)}", str(sc(y)), str(ptd.get(name, 0)),
                                                                      str(i if share >= 0.6 else 0)]})
        if "rush" in s:
            n, y = s["rush"]
            teams[team]["rushing"].append({"athlete": ath, "stats": [str(max(sc(n), 1)), str(sc(y)), str(rtd.get(name, 0))]})
        if "rec" in s:
            n, y = s["rec"]
            k = rectd.get(name, 0)
            teams[team]["receiving"].append({"athlete": ath, "stats": [str(max(sc(n), k)), str(sc(y)), str(k)]})
    for t, k in KICKER.items():
        f = fg.get(t, [])
        teams[t]["kicking"].append({"athlete": {"id": ids.get(k, k), "displayName": k}, "stats": [
            f"{len(f)}/{len(f)}", "100.0", str(max(f) if f else 0), f"{xp.get(t, 0)}/{xp.get(t, 0)}",
            str(3 * len(f) + xp.get(t, 0))]})
    for t, n in (("GB", 3), ("LAR", 2)):
        teams[t]["defensive"].append({"athlete": {"id": f"sk{t}", "displayName": f"{t} pass rush"},
                                      "stats": ["0", "0", str(round(n * share)), "0", "0", "0", "0"]})
    return {"players": [{"team": TEAM[t], "statistics": [{"name": c, "keys": keys[c], "athletes": v}
                                                          for c, v in cats.items() if v]} for t, cats in teams.items()]}


def hdr(a, h):
    return {"competitions": [{"competitors": [{"team": {"abbreviation": "GB"}, "score": str(a)},
                                              {"team": {"abbreviation": "LAR"}, "score": str(h)}]}]}


REAL_LIVE = bot.league_live("nfl")
_SIM = {"summary": None}


def sim_live(sport="nfl", week=None):
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
bot.espn_topup = lambda live, events: live          # the simulated box is the truth here


def at(state, period, clock, score, name=None):
    e = copy.deepcopy(event0)
    for c in e["competitions"][0]["competitors"]:
        c["score"] = str(score[0] if c["homeAway"] == "away" else score[1])
    e["status"]["period"], e["status"]["clock"] = period, clock
    e["status"]["type"].update({"state": state, "completed": state == "post",
                                "name": name or {"in": "STATUS_IN_PROGRESS", "post": "STATUS_FINAL"}[state],
                                "shortDetail": "Final/OT" if state == "post" and period > 4 else "",
                                "detail": "Final/OT" if state == "post" and period > 4 else ""})
    return e


def secs(c):
    m, s = c.split(":")
    return int(m) * 60 + int(s)


def main(out):
    st = json.loads(bot.STATE_FILE.read_text()) if bot.STATE_FILE.exists() else {}
    st.setdefault("nfl", {})
    bot._NFL_ST["st"] = st
    labels = []
    plays, drives, a_s, h_s = [], [], 0, 0

    def add(team, kind, q, clock, text, detail="", down=""):
        nonlocal a_s, h_s
        pts = 3 if kind == "FG" else 6 + (1 if "Kick)" in text else 0)
        if team == "GB":
            a_s += pts
        else:
            h_s += pts
        pid = f"{EVENT_ID}9{len(plays):03d}"
        p = {"id": pid, "type": {"abbreviation": kind, "text": "Touchdown" if kind == "TD" else "Field Goal"},
             "text": text, "awayScore": a_s, "homeScore": h_s, "period": {"number": q},
             "clock": {"displayValue": clock}, "team": {"abbreviation": team}}
        plays.append(p)
        drives.append({"description": "", "team": {"abbreviation": team},
                       "plays": [{"id": pid, "text": detail, "start": {"downDistanceText": down}}]})
        return p

    for team, kind, q, clock, text in REAL:
        add(team, kind, q, clock, text)
    ctx0 = None
    for when, team, kind, q, clock, text, detail, down, share, post in SCRIPT:
        if post == "gc":
            gid = f"{EVENT_ID}8{len(drives):03d}"
            prev_id = gid + "0"
            drives.append({"description": "", "team": {"abbreviation": "LAR"}, "plays": [
                {"id": prev_id, "text": "K.Williams up the middle to GB 9 for 6 yards.", "type": {"text": "Rush"},
                 "period": {"number": q}, "clock": {"displayValue": "2:41"}},
                {"id": gid, "text": detail, "type": {"text": "Pass Interception Return"},
                 "period": {"number": q}, "clock": {"displayValue": clock}, "start": {"downDistanceText": down}}]})
            summary = {"boxscore": box(share, plays), "scoringPlays": plays, "drives": {"previous": drives},
                       "header": hdr(a_s, h_s),
                       "winprobability": [{"playId": prev_id, "homeWinPercentage": 0.83},
                                          {"playId": gid, "homeWinPercentage": 0.58}]}
            _SIM["summary"] = summary
            e = at("in", q, secs(clock), (a_s, h_s))
            p, b, a = bot.game_changers(summary, 12)[0]
            bot.post_game_changer(e, p, summary, bot.nfl_ctx(st, [e] + OTHERS), b, a)
            labels.append(f"{when} · game-changer · GB {a_s}–{h_s} LAR")
            continue
        p = add(team, kind, q, clock, text, detail, down) if team else None
        summary = {"boxscore": box(share, plays), "scoringPlays": plays, "drives": {"previous": drives},
                   "header": hdr(a_s, h_s)}
        _SIM["summary"] = summary
        if post == "half":
            e = at("in", 2, 0, (a_s, h_s), "STATUS_HALFTIME")
            ctx = bot.nfl_ctx(st, [e] + OTHERS)
            ctx0 = ctx0 or {str(r): v["po"] for r, v in ctx["ol"].items()}
            bot.post_prime_half(e, summary, ctx)
            labels.append(f"{when} · half-time · GB {a_s}–{h_s} LAR")
        elif post == "td":
            e = at("in", q, secs(clock), (a_s, h_s))
            bot.post_td_fantasy(e, p, summary, bot.nfl_ctx(st, [e] + OTHERS))
            labels.append(f"{when} · touchdown · GB {a_s}–{h_s} LAR")
        elif post == "fg":
            e = at("in", q, secs(clock), (a_s, h_s))
            bot.post_fg_clutch(e, p, summary, bot.nfl_ctx(st, [e] + OTHERS))
            labels.append(f"{when} · {'overtime ' if q > 4 else 'late '}field goal · GB {a_s}–{h_s} LAR")
        elif post == "ot":
            e = at("in", 5, 600, (a_s, h_s))
            bot.post_overtime(e, summary, bot.nfl_ctx(st, [e] + OTHERS))
            labels.append(f"{when} · overtime · GB {a_s}–{h_s} LAR")
        elif post == "walkoff":
            e = at("post", 5, 0, (a_s, h_s))
            ctx = bot.nfl_ctx(st, [e] + OTHERS)
            bot.post_td_fantasy(e, p, summary, ctx)
            labels.append(f"{when} · walk-off touchdown · GB {a_s}–{h_s} LAR")
            bot.post_prime_final(e, summary, ctx)
            labels.append(f"{when} · full-time (OT) · GB {a_s}–{h_s} LAR")
            final_ctx = ctx

    fake_now = datetime(2026, 11, 26, 14, 0, tzinfo=bot.UK)
    ol, live = final_ctx["ol"], final_ctx["live"]
    html_text = bot.playoff_race_html(final_ctx, ctx0, fake_now.strftime("%a %-d %b"))
    mover = max(ol, key=lambda r: abs(ol[r]["po"] - ctx0.get(str(r), ol[r]["po"])))
    d = ol[mover]["po"] - ctx0[str(mover)]
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
