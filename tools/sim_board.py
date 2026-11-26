#!/usr/bin/env python3
"""
Weekly matchups board: mock-ups with real league data, and a dry run of the whole week's lifecycle.

  python3 tools/sim_board.py cards OUT_DIR       # PNGs: NFL fresh / live now / Sunday evening / final, NBA fresh / mid-week
  python3 tools/sim_board.py lifecycle           # fake clock + fake Discord: post, edits, pin, notice delete, unpin,
                                                 # restarts, NBA Monday swap. Nothing is sent anywhere.
"""
import copy
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bot  # noqa: E402


def _event_at(e, state, period=0, clock=0.0):
    e = copy.deepcopy(e)
    e["status"]["type"]["state"] = state
    e["status"]["type"]["completed"] = state == "post"
    e["status"]["period"] = period
    e["status"]["clock"] = clock
    return e


def nfl_week_at(week, cut_uk, live_share=0.62):
    """Week `week` as it stood at `cut_uk`: earlier slots final, the slot that started last in progress, rest to come.
    Points come from Sleeper's final per-player points for that week (scaled for the game in progress)."""
    evs = bot.fetch(f"football/nfl/scoreboard?seasontype=2&week={week}").get("events", [])
    live = bot.league_live("nfl", week)
    out, frac_of_team = [], {}
    for e in evs:
        k = bot.parse_time(e["date"])
        played = (cut_uk - k.astimezone(bot.UK)).total_seconds() / (3.3 * 3600)
        if played < 0:
            e2, f = _event_at(e, "pre"), 0.0
        elif played < 1:
            live_share = played
            sec = live_share * 3600
            per = int(sec // 900) + 1
            e2, f = _event_at(e, "in", per, 900 - (sec - (per - 1) * 900)), live_share
        else:
            e2, f = _event_at(e, "post", 4, 0), 1.0
        out.append(e2)
        for t in bot.teams_of(e):
            frac_of_team[bot.sl_team(t["team"]["abbreviation"])] = f
    players = bot.sleeper_players("nfl")
    for x in live["rosters"].values():
        tot = 0.0
        for p in x["starters"]:
            f = frac_of_team.get((players.get(p) or {}).get("team"), 1.0)
            tot += float(x["pp"].get(p) or 0) * f
        x["points"] = round(tot, 2)
    return live, out


def save(png, path):
    Path(path).write_bytes(png)
    print("wrote", path)


def cards(out_dir):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sw = int(bot.fetch_url(f"{bot.SLEEPER}/state/nfl").get("week"))
    # 1) NFL as it will look at 00:00 Friday: next week, nothing played
    nxt = sw + 1
    evs = [_event_at(e, "pre") for e in bot.fetch(f"football/nfl/scoreboard?seasontype=2&week={nxt}").get("events", [])]
    live = bot.league_live("nfl", nxt)
    for x in live["rosters"].values():
        x["points"] = 0.0
    d = bot.wb_nfl_data(nxt, evs, live=live)
    save(bot.render_card(bot.wb_html(d, "00:00")), out / "nfl_board_fresh.png")
    # 2) NFL right now (real live data, this week)
    evs = bot.fetch(f"football/nfl/scoreboard?seasontype=2&week={sw}").get("events", [])
    d = bot.wb_nfl_data(sw, evs)
    save(bot.render_card(bot.wb_html(d)), out / "nfl_board_now.png")
    # 3) NFL last week, Sunday 22:40 UK: 18:00 games final, the late games in progress, night games to come
    lw = sw - 1
    evs0 = bot.fetch(f"football/nfl/scoreboard?seasontype=2&week={lw}").get("events", [])
    sun = max(bot.parse_time(e["date"]).astimezone(bot.UK) for e in evs0
              if bot.parse_time(e["date"]).astimezone(bot.UK).weekday() == 6 and
              bot.parse_time(e["date"]).astimezone(bot.UK).hour == 18)
    live, evs = nfl_week_at(lw, sun.replace(hour=22, minute=40))
    d = bot.wb_nfl_data(lw, evs, live=live, proj=bot._nfl_proj_fetch(lw))
    save(bot.render_card(bot.wb_html(d, "22:40")), out / "nfl_board_sunday.png")
    # 4) NFL last week, all done
    live = bot.league_live("nfl", lw)
    evs = [_event_at(e, "post", 4, 0) for e in evs0]
    d = bot.wb_nfl_data(lw, evs, live=live, proj=bot._nfl_proj_fetch(lw))
    save(bot.render_card(bot.wb_html(d, "04:30")), out / "nfl_board_final.png")
    # 5) NBA right now (real: this week, days played so far)
    nw = int(bot.fetch_url(f"{bot.SLEEPER}/state/nba").get("week"))
    d = bot.wb_nba_data(nw)
    save(bot.render_card(bot.wb_html(d)), out / "nba_board_now.png")
    # 6) NBA at Monday 12:00 UK: same week, nothing played yet, Monday's games still to come
    rows = bot._nba_rows_fetch(nw)
    mon = min(dd for _, dd, _ in rows)
    live = bot.league_live("nba", nw)
    for x in live["rosters"].values():
        x["points"] = 0.0
    d = bot.wb_nba_data(nw, events_today=[], live=live, rows=rows, day0=mon)
    save(bot.render_card(bot.wb_html(d, "12:00")), out / "nba_board_fresh.png")


# ------------------------------------------------------------------ lifecycle dry run

class Clock:
    def __init__(self, t):
        self.t = t

    def time(self):
        return self.t


def lifecycle():
    """Walk a fake clock from Thursday to the next Tuesday with fake Discord + fake data and check every step."""
    real_time = time.time
    UK = bot.UK
    clock = Clock(datetime(2026, 12, 3, 23, 0, tzinfo=UK).timestamp())   # Thursday 3 Dec 23:00 UK (week 13)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock.t, tz or timezone.utc)

    bot.datetime = FakeDT
    bot.time.time = clock.time
    bot.time.sleep = lambda s: setattr(clock, "t", clock.t + s)
    log = []
    discord_msgs = {}          # id -> {"pinned": bool}
    notices = []               # system "pinned a message" lines in the channel
    nxt = {"id": 1000}

    def fake_post(hook, embed, label="", content=None, files=None):
        nxt["id"] += 1
        bot._LAST_MSG.clear()
        bot._LAST_MSG.update(id=str(nxt["id"]), channel_id="42")
        discord_msgs[str(nxt["id"])] = {"pinned": False, "edits": 0, "hook": hook}
        log.append((clock.t, "post", str(nxt["id"]), hook))
        return True

    def fake_edit(hook, mid, png=None, name="live.png", content=None):
        if mid not in discord_msgs:
            return None
        discord_msgs[mid]["edits"] += 1
        log.append((clock.t, "edit", mid))
        return True

    def fake_api(method, path, body=None):
        parts = path.split("?")[0].strip("/").split("/")
        if method == "GET" and parts[-1] == "pins":
            if "messages" in parts:
                return 200, {"items": [{"message": {"id": k}} for k, v in discord_msgs.items() if v["pinned"]]}
            return 200, [{"id": k} for k, v in discord_msgs.items() if v["pinned"]]
        if method == "PUT" and "pins" in parts:
            mid = parts[-1]
            if not discord_msgs[mid]["pinned"]:
                discord_msgs[mid]["pinned"] = True
                notices.append({"id": f"n{mid}", "type": 6, "message_reference": {"message_id": mid}})
            log.append((clock.t, "pin", mid))
            return 204, None
        if method == "DELETE" and "pins" in parts:
            discord_msgs[parts[-1]]["pinned"] = False
            log.append((clock.t, "unpin", parts[-1]))
            return 204, None
        if method == "GET" and parts[-1] == "messages":
            return 200, list(reversed(notices))
        if method == "DELETE" and parts[-2] == "messages":
            notices[:] = [n for n in notices if n["id"] != parts[-1]]
            log.append((clock.t, "notice-deleted", parts[-1]))
            return 204, None
        return 404, None

    # fake data: NFL week 13 Thu 01:15 UK? no: first game Friday 01:15 UK, last Tuesday 01:15 UK
    def nfl_events(week):
        base = datetime(2026, 12, 4, 1, 15, tzinfo=UK) if week == 13 else datetime(2026, 12, 11, 1, 15, tzinfo=UK)
        kicks = [base, base + timedelta(days=2, hours=17), base + timedelta(days=4)]
        out = []
        for k in kicks:
            st = "pre" if clock.t < k.timestamp() else ("in" if clock.t < k.timestamp() + 3.3 * 3600 else "post")
            out.append({"id": str(k.timestamp()), "date": k.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
                        "status": {"type": {"state": st, "completed": st == "post"}, "period": 2, "clock": 300},
                        "competitions": [{"competitors": []}]})
        return out

    def nfl_data(week, events, **k):
        live = sum(e["status"]["type"]["state"] == "in" for e in events)
        done = sum(e["status"]["type"]["state"] == "post" for e in events)
        left = 3 - done
        sides = {r: {"points": 10.0 * done + r, "proj": 100.0 + r, "var": 50.0 * left, "left": left, "on": live}
                 for r in range(1, 5)}
        return {"sport": "nfl", "week": week, "pairs": [[1, 2], [3, 4]], "sides": sides, "rosters": {},
                "people": {}, "games": 3, "done": done, "live": live, "next_kick": None}

    def nba_data(week, **k):
        sides = {r: {"points": 5.0 * r, "proj": 900.0 + r, "var": 900.0, "left": 20, "on": 0} for r in range(1, 3)}
        return {"sport": "nba", "week": week, "pairs": [[1, 2]], "sides": sides, "rosters": {}, "people": {},
                "live": 0, "days_left": 5, "games": 40}

    bot.discord = fake_post
    bot.discord_edit = fake_edit
    bot.discord_api = fake_api
    bot.render_card = lambda h, w=None: b"png"
    bot.wb_html = lambda d, stamp=None: "<html>"
    bot._wb_nfl_events = nfl_events
    bot.wb_nfl_data = nfl_data
    bot.wb_nba_data = nba_data
    bot.wb_nba_week = lambda monday: 7 if monday.day >= 30 or monday.month == 12 else 6
    bot._wb_sleeper_week = lambda sport: 13 if sport == "nfl" else 7
    bot._wb_nba_today = lambda: ("x", [])
    bot.alert = lambda *a, **k: log.append((clock.t, "alert", a[0]))
    bot.BOT_TOKEN = ""                                 # first: no bot token on the server
    bot.NFL_HOOK, bot.NBA_HOOK = "nfl-hook", "nba-hook"
    bot._log_plain = lambda *a: None
    bot._hook_message = lambda hook, mid: {"id": mid, "channel_id": "42"} if mid in discord_msgs else None
    # the old game-night scoreboard still running when the new code starts: it becomes this week's NBA board
    discord_msgs["900"] = {"pinned": False, "edits": 0, "hook": "nba-hook"}
    st = {"live_boards": {"nba-2026-12-03": {"msg": "900", "t": clock.t - 1800, "final": False}}}

    def run_until(t_end, step=60):
        while clock.t < t_end:
            bot._NEXT_WAKE["t"] = None
            bot.maybe_live_board(st)
            w = bot._NEXT_WAKE["t"]
            clock.t += min(step, max(1, w - clock.t)) if w else step     # same as the main loop

    def at(day, h, m=0):
        return datetime(2026, 12, day, h, m, tzinfo=UK).timestamp()

    def fmt(t):
        return datetime.fromtimestamp(t, UK).strftime("%a %d %H:%M:%S")

    problems = []
    run_until(at(4, 0, 0) + 600, step=290)            # past Friday 00:00 (NFL), NBA already open (Mon 30 Nov)
    posts = [x for x in log if x[1] == "post"]
    nfl_post = next((x for x in posts if x[3] == "nfl-hook"), None)
    nba_post = next((x for x in posts if x[3] == "nba-hook"), None)
    print("NFL board posted:", fmt(nfl_post[0]) if nfl_post else None)
    nba_key = next(k for k in st["weekly_boards"] if k.startswith("nba"))
    print("NBA board:", "took over message", st["weekly_boards"][nba_key]["msg"],
          "edits", discord_msgs["900"]["edits"], "| new NBA posts:", 1 if nba_post else 0)
    if nba_post or st["weekly_boards"][nba_key]["msg"] != "900" or not discord_msgs["900"]["edits"]:
        problems.append("old game-night board not taken over")
    if not nfl_post or not (at(4, 0) <= nfl_post[0] <= at(4, 0) + 5):
        problems.append("NFL board not posted at Friday 00:00")
    if [x for x in log if x[1] == "pin"]:
        problems.append("pinned without a token")
    if not [x for x in log if x[1] == "alert" and "DISCORD_BOT_TOKEN" in x[2]]:
        problems.append("no alert about the missing token")
    bot.BOT_TOKEN = "fake"                             # token added (restart): pins catch up straight away
    run_until(clock.t + 60)
    pins = [x for x in log if x[1] == "pin"]
    print("pins:", [(fmt(x[0]), x[2]) for x in pins])
    if not nfl_post or not any(x[2] == nfl_post[2] for x in pins):
        problems.append("NFL board not pinned")
    if notices:
        problems.append(f"pin notices left in the channel: {notices}")
    # restart: lose memory (pending snapshots, caches) but keep state; nothing may be pinned/posted twice
    saved = json.loads(json.dumps(st))
    bot._WB_PENDING.clear()
    st.clear()
    st.update(saved)
    n_posts, n_pins = len([x for x in log if x[1] == "post"]), len([x for x in log if x[1] == "pin"])
    run_until(at(6, 20, 0), step=290)                 # through Sunday: live edits
    if len([x for x in log if x[1] == "post"]) != n_posts:
        problems.append("restart caused a second post")
    if len([x for x in log if x[1] == "pin"]) != n_pins:
        problems.append("restart caused a second pin")
    edits = [x for x in log if x[1] == "edit" and nfl_post and x[2] == nfl_post[2]]
    print("NFL board edits so far:", len(edits))
    # crash right after pinning, before state was saved: simulate by forgetting the pin state of the NBA board
    st["weekly_boards"][nba_key]["pin"] = {"state": "none"}
    run_until(at(6, 20, 10))
    if len([x for x in log if x[1] == "pin" and x[2] == st["weekly_boards"][nba_key]["msg"]]) != 1:
        problems.append("lost pin state caused a double pin")
    # NBA Monday swap + NFL Tuesday unpin
    run_until(at(8, 13, 0), step=290)
    unpins = [(fmt(x[0]), x[2]) for x in log if x[1] == "unpin"]
    print("unpins:", unpins)
    nfl_unpin = [x[0] for x in log if x[1] == "unpin" and nfl_post and x[2] == nfl_post[2]]
    if not nfl_unpin or not (at(8, 12) <= nfl_unpin[0] <= at(8, 12) + 400):
        problems.append("NFL board not unpinned Tuesday 12:00")
    nba_posts = [x for x in log if x[1] == "post" and x[3] == "nba-hook"]
    print("NBA posts:", [(fmt(x[0]), x[2]) for x in nba_posts])
    if len(nba_posts) != 1 or not (at(7, 12) <= nba_posts[0][0] <= at(7, 12) + 5):
        problems.append("NBA board not replaced Monday 12:00")
    else:
        old, new = "900", nba_posts[0][2]
        if discord_msgs[old]["pinned"] or not discord_msgs[new]["pinned"]:
            problems.append("NBA Monday swap left the wrong board pinned")
    pinned_now = [k for k, v in discord_msgs.items() if v["pinned"]]
    print("pinned at the end:", pinned_now, "| notices left:", len(notices))
    if notices:
        problems.append("pin notices left in the channel")
    bot.time.time = real_time
    if problems:
        print("\nwhat happened:")
        for x in log:
            print("  ", fmt(x[0]), *x[1:3])
    print("\nLIFECYCLE OK" if not problems else "\nPROBLEMS:\n  " + "\n  ".join(problems))
    return not problems


if __name__ == "__main__":
    if sys.argv[1] == "cards":
        cards(sys.argv[2])
    else:
        sys.exit(0 if lifecycle() else 1)
