#!/usr/bin/env python3
"""
Touchdown + try (extra point / 2-pt) cards with real data: the card as first posted (try not in yet) and the
edited card for each kind of try. Uses a real touchdown (default: Goff -> LaPorta 11 yd, CHI @ DET, 26 Nov 2026).

  python3 tools/sim_try.py OUT_DIR [EVENT_ID PLAY_ID]
  python3 tools/sim_try.py flow        # fake clock + fake Discord: post, try edit, restart, overturn, give-up
Writes td-try-0-posted.png, td-try-1-xp-good.png, -2-xp-missed.png, -3-2pt-good.png, -4-2pt-failed.png
(still images; on Discord they are the same cards, animated).
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

EVENT_ID, PLAY_ID = "401873081", "4018730812318"


def main(out, event_id=EVENT_ID, play_id=PLAY_ID):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    bot.CONFIG["animations"] = {"enabled": False}          # stills for review
    bot.NFL_HOOK = "sim"
    summary = bot.fetch(f"football/nfl/summary?event={event_id}")
    week = bot.fetch_url(f"{bot.SLEEPER}/state/nfl").get("week")
    events = bot.fetch(f"football/nfl/scoreboard?seasontype=2&week={week}").get("events", [])
    event = copy.deepcopy(next(e for e in events if e["id"] == event_id))
    event["status"]["type"].update(state="in", completed=False)
    play = next(p for p in summary["scoringPlays"] if str(p["id"]) == play_id)
    st = {"nfl": {}, "nba": {}}
    bot._NFL_ST["st"] = st
    ctx = bot.nfl_ctx(st, events)
    a0, h0 = bot._td_prev_score(play, summary)
    away = bot.teams_of_play_side(play, summary)
    kicker = "Jake Bates" if not away else "Cairo Santos"
    qb = bot.td_parse(play).get("qb") or "Jared Goff"
    variants = [
        ("1-xp-good", f"({kicker} Kick)", 1, 0, {"id": 61, "text": "Extra Point Good", "value": 1}),
        ("2-xp-missed", f"({kicker} PAT Failed)", 0, 0, {"id": 62, "text": "Extra Point Missed", "value": 0}),
        ("3-2pt-good", f"({qb} Pass to Amon-Ra St. Brown for Two-Point Conversion)", 2, 0,
         {"id": 15, "text": "Two Point Pass", "value": 2}),
        ("4-2pt-failed", "(Two-Point Pass Conversion Failed)", 0, 0, {"id": 15, "text": "Two Point Pass", "value": 0}),
    ]
    first = bot.td_only_play(play, summary)
    tag = {"label": "⏳ CONVERSION TO COME", "cls": "wait"}
    img, name, _ = bot._rebuild_td_image(event, first, summary, ctx, tag)
    (out / "td-try-0-posted.png").write_bytes(img)
    print("wrote", out / "td-try-0-posted.png", "score", first["awayScore"], first["homeScore"])
    for slug, pat, mine, theirs, pa in variants:
        s2 = copy.deepcopy(summary)
        p = next(x for x in s2["scoringPlays"] if str(x["id"]) == play_id)
        p["text"] = bot.td_only_play(play, summary)["text"] + " " + pat
        if away:
            p["awayScore"], p["homeScore"] = a0 + 6 + mine, h0 + theirs
        else:
            p["awayScore"], p["homeScore"] = a0 + theirs, h0 + 6 + mine
        dp, _ = bot._td_drive_play(s2, play_id)
        if dp is not None:
            dp["pointAfterAttempt"] = pa
        res = bot.td_try(p, s2)
        img, name, _ = bot._rebuild_td_image(event, p, s2, ctx, res)
        f = out / f"td-try-{slug}.png"
        f.write_bytes(img)
        print("wrote", f, "score", p["awayScore"], p["homeScore"], res and res["label"])


def flow():
    """The real touchdown replayed on a fake clock through the bot's own prime-time loop. Nothing is sent."""
    import json
    from datetime import datetime, timezone
    summary0 = bot.fetch(f"football/nfl/summary?event={EVENT_ID}")
    week = bot.fetch_url(f"{bot.SLEEPER}/state/nfl").get("week")
    events = bot.fetch(f"football/nfl/scoreboard?seasontype=2&week={week}").get("events", [])
    event = copy.deepcopy(next(e for e in events if e["id"] == EVENT_ID))
    event["status"]["type"].update(state="in", completed=False)
    st0 = {"nfl": {}, "nba": {}}
    ctx = bot.nfl_ctx(st0, events)
    play = next(p for p in summary0["scoringPlays"] if str(p["id"]) == PLAY_ID)
    dp0, _ = bot._td_drive_play(summary0, PLAY_ID)
    t_td = bot._wallclock_ts(dp0["wallclock"])
    i = [str(p["id"]) for p in summary0["scoringPlays"]].index(PLAY_ID)

    def make_summary(with_try, next_wc=None):
        s = copy.deepcopy(summary0)
        s["scoringPlays"] = s["scoringPlays"][:i + 1]
        if not with_try:
            s["scoringPlays"][i] = bot.td_only_play(play, summary0)
        drives = s["drives"]
        for d in list(drives.get("previous") or []) + ([drives["current"]] if drives.get("current") else []):
            keep = []
            for q in d.get("plays") or []:
                if str(q["id"]) == PLAY_ID:
                    if not with_try:
                        q["pointAfterAttempt"] = {"id": 0, "text": "Not Available", "value": 0}
                    keep.append(q)
                    if next_wc:
                        keep.append({"id": "kick", "wallclock": datetime.fromtimestamp(next_wc, timezone.utc)
                                     .strftime("%Y-%m-%dT%H:%M:%SZ"), "text": "kickoff"})
                    break
                keep.append(q)
            d["plays"] = keep
        return s

    clock = {"t": 0.0}

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock["t"], tz or timezone.utc)

    bot.datetime = FakeDT
    bot.time.time = lambda: clock["t"]
    bot.time.sleep = lambda x: clock.__setitem__("t", clock["t"] + max(0, x))
    real_discord = bot.discord
    log = []
    cur = {"s": None}

    def fake_discord(hook, embed, label="", content=None, files=None):
        if bot.CAPTURE is not None:
            return real_discord(hook, embed, label, content, files)
        name, data = next(iter(files.items()))
        bot._wait_for_send_slot()                        # same exact-second wait as a real post
        if name.startswith("td"):
            mid = str(len(log) + 1)
            bot._LAST_MSG.clear()
            bot._LAST_MSG.update(id=mid, channel_id="1")
            log.append(("post", round(clock["t"] - t_td), _score(data), _tag(data)))
        return True

    def fake_edit(hook, mid, png=None, name="", content=None):
        log.append(("edit", round(clock["t"] - t_td), _score(png), _tag(png), content))
        return True

    def _score(data):
        import re
        m = re.search(r"CHI (\d+)–(\d+) DET", (data or b"").decode(errors="ignore"))
        return f"{m.group(1)}-{m.group(2)}" if m else "?"

    def _tag(data):
        import re
        m = re.search(r"class='tryt [a-z]+'>([^<]+)<", (data or b"").decode(errors="ignore"))
        return m.group(1) if m else None

    bot.discord = fake_discord
    bot.discord_edit = fake_edit
    bot.render_card = lambda h, w=None: h.encode()
    bot.render_anim = lambda h, w=None: None
    bot.fetch = lambda path, tries=3: cur["s"] if "summary" in path else {"events": [event]}
    bot.NFL_HOOK = "sim"
    bot._log_plain = lambda *a: None
    bot.alert = lambda *a, **k: None
    others = [str(p["id"]) for p in summary0["scoringPlays"][:i]]

    def fresh_state():
        return {"nfl": {event["id"]: {"date": event["date"][:10], "tds": list(others), "pending": {},
                                       "preview": True}}, "nba": {}, "nfl_slots": {"p:" + event["date"]: {"pre": True}}}

    def run(st, until, timeline):
        while clock["t"] < until:
            for at, s in timeline:
                if clock["t"] >= at:
                    cur["s"] = s
            bot._NEXT_WAKE["t"] = None
            bot.nfl_prime_slot([event], st, lambda: ctx)
            clock["t"] += 5

    problems = []
    no_try, with_try = make_summary(False), make_summary(True, t_td + 70)

    # 1) try kicked ~40s after the TD: post at +60 without it; the bot sees the try on its next check after
    #    posting (it sleeps until the exact send second), so the edit lands 60s after that sighting
    clock["t"] = t_td + 5
    st = fresh_state()
    run(st, t_td + 90, [(0, no_try), (t_td + 45, with_try)])
    saved = json.loads(json.dumps(st))                 # restart: memory gone, state kept
    bot._TD_HTML.clear()
    st = saved
    run(st, t_td + 400, [(0, with_try)])
    print("1) normal:", log)
    posts = [x for x in log if x[0] == "post"]
    edits = [x for x in log if x[0] == "edit"]
    if not posts or posts[0][2] != "10-18" or "TO COME" not in (posts[0][3] or "") or not 59 <= posts[0][1] <= 66:
        problems.append("TD post not 10-18 'to come' at +60s")
    if len(edits) != 1 or edits[0][2] != "10-19" or "EXTRA POINT GOOD" not in (edits[0][3] or "") \
            or not 104 <= edits[0][1] <= 130 or edits[0][4] is not None:
        problems.append("edit not exactly once, 10-19 + EXTRA POINT GOOD, image only, at seen+60s")

    # 2) bot late: TD and try already in, both old enough -> one post with the full score, no edit
    log.clear()
    clock["t"] = t_td + 130
    st = fresh_state()
    run(st, t_td + 400, [(0, make_summary(True, t_td + 50))])
    print("2) late:", log)
    if [x[:4] for x in log] != [("post", log[0][1], "10-19", "✓ EXTRA POINT GOOD")] if log else True:
        problems.append("late post should be one post with 10-19 and no edit")

    # 3) overturned before the try comes in: never edited
    log.clear()
    clock["t"] = t_td + 5
    st = fresh_state()
    run(st, t_td + 70, [(0, no_try)])
    for r in st.get("td_posts", {}).values():
        r["done"] = "overturned"
    run(st, t_td + 400, [(0, with_try)])
    print("3) overturned:", log)
    if any(x[0] == "edit" for x in log):
        problems.append("overturned touchdown was edited")

    # 4) try never comes, game ends: give up, take "to come" off once
    log.clear()
    clock["t"] = t_td + 5
    st = fresh_state()
    run(st, t_td + 70, [(0, no_try)])
    event["status"]["type"].update(state="post", completed=True)
    run(st, t_td + 900, [(0, no_try)])
    event["status"]["type"].update(state="in", completed=False)
    print("4) no try:", log)
    edits = [x for x in log if x[0] == "edit"]
    if len(edits) != 1 or edits[0][3] is not None or edits[0][2] != "10-18":
        problems.append("give-up should edit once to remove 'to come' and keep 10-18")
    print("\nTRY FLOW OK" if not problems else "\nPROBLEMS:\n  " + "\n  ".join(problems))
    return not problems


if __name__ == "__main__":
    if sys.argv[1:] == ["flow"]:
        sys.exit(0 if flow() else 1)
    main(*sys.argv[1:])
