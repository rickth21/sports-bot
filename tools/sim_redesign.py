#!/usr/bin/env python3
"""
Before/after mock-ups of the weekly recap (both posts), playoff picture and bracket, at Discord inline size.
Real league data: NFL week 11, NBA week 5, playoff picture + bracket from the current NFL standings.

  python3 tools/sim_redesign.py OUT_DIR
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot  # noqa: E402
import sim_bigger as sb  # noqa: E402


def main(out):
    out, tmp = Path(out), Path(tempfile.mkdtemp())
    cards = {}
    for sport, week in (("nfl", 11), ("nba", 5)):
        live, settings = bot.league_live(sport, week=week), bot.league_settings(sport)
        old1, old2 = bot._recap_wide_classic(sport, week, live, settings)
        new1, new2 = bot.recap_v2_html(sport, week, live, settings)
        cards[f"recap-{sport}-1"] = (old1, new1, "Donald 🏈" if sport == "nfl" else "Donald 🏀")
        cards[f"recap-{sport}-2"] = (old2, new2, "Donald 🏈" if sport == "nfl" else "Donald 🏀")
    settings = bot.league_settings("nfl")
    s = settings["settings"]
    po = int(s["playoff_week_start"])
    week = bot.completed_week("nfl", settings)
    reg = min(week, po - 1)
    scores, rec = bot.season_history("nfl", reg)
    status = bot.playoff_status(settings, rec, reg)
    odds, _, _ = bot.simulate_odds("nfl", settings, scores, rec, reg)
    changes = [(r, "alive", "in") for r in status if status[r]["seed"] == 1][:1] + \
              [(r, "alive", "out") for r in status if status[r]["seed"] == len(status)][:1]
    cards["playoff-picture"] = (bot._playoff_picture_classic("nfl", settings, rec, status, changes, odds, reg),
                                bot.playoff_picture_v2_html("nfl", settings, rec, status, changes, odds, reg), "Donald 🏈")
    br = bot.build_bracket("nfl", settings, status, week)
    bo = bot.bracket_odds(br, scores)
    cards["bracket"] = (bot._bracket_classic("nfl", settings, br, rec, bo, 0), bot.bracket_v2_html("nfl", settings, br, rec, bo, 0),
                        "Donald 🏈")
    for name, (old, new, who) in cards.items():
        b, a = tmp / f"{name}-b.png", tmp / f"{name}-a.png"
        b.write_bytes(bot.render_card(old))
        a.write_bytes(bot.render_card(new))
        (tmp / f"{name}.html").write_text(new)
        sb.shot(sb.PAGE.replace("{BODY}", sb.msg("BEFORE", b, who) + sb.msg("AFTER", a, who)), out / f"redesign-{name}.png")
        print("wrote", out / f"redesign-{name}.png", tmp / f"{name}.html")


if __name__ == "__main__":
    main(sys.argv[1])
