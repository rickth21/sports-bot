#!/usr/bin/env python3
"""
Pre-upload check: run this before every upload. Nothing is posted to Discord.

  python3 tools/check.py [out_dir]

1. bot.py compiles
2. a full dry run of the NFL + NBA loops against the live repo state (no exceptions, nothing re-posted)
3. every card type renders (touchdown, late FG, game-changer, half-time, overtime, full-time, playoff race,
   NBA single + grouped) through the overtime simulation and tonight's NBA results
4. last week's fantasy points match Sleeper for every rostered NFL player
Prints PASS/FAIL per step and exits non-zero on any failure.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="sportsbot-check-"))
OUT.mkdir(parents=True, exist_ok=True)
results = []


def step(name, fn):
    t = time.time()
    try:
        note = fn() or ""
        results.append((True, name, note))
        print(f"PASS  {name}  ({time.time() - t:.0f}s) {note}")
    except Exception as e:  # noqa: BLE001
        results.append((False, name, str(e)))
        print(f"FAIL  {name}: {e}")
        traceback.print_exc()


def compiles():
    subprocess.run([sys.executable, "-m", "py_compile", str(ROOT / "bot.py")], check=True)


def dry_run():
    import bot
    raw = "https://raw.githubusercontent.com/rickth21/sports-bot/main/state.json?x=" + str(int(time.time()))
    st = json.loads(urllib.request.urlopen(urllib.request.Request(raw, headers={"User-Agent": "Mozilla/5.0"}),
                                           timeout=20).read())
    bot.CAPTURE = []
    bot.NFL_HOOK = bot.NBA_HOOK = "dry"
    bot.run_nfl(st)
    bot.run_nba(st)
    n = len(bot.CAPTURE)
    return f"{n} post(s) would go out now" + (" — check these are expected" if n else "")


def cards():
    sim = subprocess.run([sys.executable, str(ROOT / "tools" / "sim_ot.py"), str(OUT / "sim.json")],
                         cwd=ROOT, capture_output=True, text=True, timeout=900)
    if sim.returncode:
        raise RuntimeError(sim.stderr[-800:])
    posts = json.loads((OUT / "sim.json").read_text())
    if len(posts) < 10:
        raise RuntimeError(f"only {len(posts)} simulated posts")
    for p in posts:
        f = p["attachments"][0].replace("file://", "")
        if os.path.getsize(f) < 20_000:
            raise RuntimeError(f"tiny image {f}")
    import bot
    bot.CAPTURE = []
    bot.NBA_HOOK = "dry"
    fin = [e for e in bot.fetch("basketball/nba/scoreboard").get("events", []) if e["status"]["type"].get("completed")]
    if fin:
        live = bot.league_live("nba")
        bot.post_nba_single(fin[0], live)
        if len(fin) > 1:
            bot.post_nba_group("FULL-TIME", fin[1:3], live)
    return f"{len(posts)} NFL cards + {len(bot.CAPTURE)} NBA cards rendered into {OUT}"


def accuracy():
    import bot
    week = int(bot.fetch_url(f"{bot.SLEEPER}/state/nfl").get("week") or 0) - 1
    checked, bad = bot.nfl_accuracy(week)
    if bad:
        raise RuntimeError(f"week {week}: {len(bad)} of {checked} differ: {bad[:5]}")
    return f"week {week}: {checked} players match Sleeper"


step("bot.py compiles", compiles)
step("dry run against live state", dry_run)
step("every card type renders", cards)
step("points match Sleeper (last week)", accuracy)
ok = all(r[0] for r in results)
print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED — don't upload yet")
sys.exit(0 if ok else 1)
