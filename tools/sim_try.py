#!/usr/bin/env python3
"""
Touchdown + try (extra point / 2-pt) cards with real data: the card as first posted (try not in yet) and the
edited card for each kind of try. Uses a real touchdown (default: Goff -> LaPorta 11 yd, CHI @ DET, 26 Nov 2026).

  python3 tools/sim_try.py OUT_DIR [EVENT_ID PLAY_ID]
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


if __name__ == "__main__":
    main(*sys.argv[1:])
