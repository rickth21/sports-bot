#!/usr/bin/env python3
"""
Before/after safety net for code clean-ups: run every kind of post through the bot with recorded web data,
a frozen clock and fixed randomness, and save the exact HTML of every card plus every Discord payload.

  python3 tools/golden.py record DATA.json OUT.json     # fetch live data once, save it, save outputs
  python3 tools/golden.py replay DATA.json OUT.json     # same inputs, no network: outputs must match
  python3 tools/golden.py compare A.json B.json         # byte-for-byte comparison

Run "record" on the old code, "replay" on the new code, then "compare".
"""
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

FROZEN = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc).timestamp()


def compare(a, b):
    A, B = json.load(open(a)), json.load(open(b))
    bad = 0
    for k in sorted(set(A) | set(B)):
        if A.get(k) != B.get(k):
            bad += 1
            x, y = A.get(k), B.get(k)
            print(f"DIFFERENT: {k}  ({len(x or [])} vs {len(y or [])} items)")
            for i, (p, q) in enumerate(zip(x or [], y or [])):
                if p != q:
                    ps, qs = json.dumps(p), json.dumps(q)
                    j = next((n for n in range(min(len(ps), len(qs))) if ps[n] != qs[n]), min(len(ps), len(qs)))
                    print(f"   item {i}: …{ps[max(0, j - 120):j + 120]}…\n   vs     …{qs[max(0, j - 120):j + 120]}…")
                    break
    total = len(set(A) | set(B))
    print(f"{total - bad} of {total} scenarios identical ({sum(len(v) for v in A.values())} outputs)")
    return bad == 0


def main(mode, data_path, out_path):
    data = json.load(open(data_path)) if mode == "replay" else {}

    class FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            d = datetime.fromtimestamp(FROZEN, timezone.utc)
            return d.astimezone(tz) if tz else d.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return datetime.fromtimestamp(FROZEN, timezone.utc).replace(tzinfo=None)

    clock = {"t": FROZEN}
    time.time = lambda: clock["t"]
    time.sleep = lambda s: clock.__setitem__("t", clock["t"] + float(s or 0))

    import bot
    bot.datetime = FrozenDT
    real_fetch = bot.fetch_url

    def fetch_url(url, tries=3):
        if mode == "replay":
            if url not in data:
                raise RuntimeError(f"not recorded: {url}")
            v = data[url]
        else:
            try:
                v = real_fetch(url, tries)
            except Exception as e:  # noqa: BLE001
                v = {"__error__": str(e)}
            data[url] = v
        if isinstance(v, dict) and "__error__" in v:
            raise RuntimeError(v["__error__"])
        return json.loads(json.dumps(v))
    bot.fetch_url = fetch_url

    rendered = []

    def fake_png(html_text, *a, **k):
        rendered.append(html_text)
        return b"PNG" + hashlib.sha1(html_text.encode()).hexdigest().encode() + b"x" * 30000
    bot.render_card = fake_png
    bot.render_png = fake_png
    bot.NFL_HOOK = bot.NBA_HOOK = "dry"
    bot.ALERT_HOOK = ""
    out = {}

    def norm(c):
        s = json.dumps(c, sort_keys=True, default=str)
        import re
        for path in set(re.findall(r"file://[^\"]+", s)):
            p = path[len("file://"):]
            try:
                s = s.replace(path, "file:" + hashlib.sha1(open(p, "rb").read()).hexdigest()[:16])
            except OSError:
                pass
        return json.loads(s)

    def scenario(name, fn):
        random.seed(12345)
        clock["t"] = FROZEN
        bot.CAPTURE = []
        rendered.clear()
        try:
            fn()
            err = None
        except Exception as e:  # noqa: BLE001
            import traceback
            err = "".join(traceback.format_exception_only(type(e), e)).strip()
        out[name] = [norm(c) for c in bot.CAPTURE] + [{"html": h} for h in rendered] + ([{"error": err}] if err else [])
        print(f"{name}: {len(bot.CAPTURE)} posts, {len(rendered)} cards" + (f"  ERROR {err}" if err else ""))

    tmp = Path(os.environ.get("TMPDIR", "/tmp")) / "golden-sims"
    tmp.mkdir(exist_ok=True)
    for sim in ("sim_ot", "sim_rest", "simulate"):
        def run_sim(sim=sim):
            mod = __import__(sim)
            mod.main(str(tmp / f"{sim}.json"))
        scenario(f"sim:{sim}", run_sim)

    nfl_lid, nba_lid = bot.CONFIG["nfl"]["sleeper_league_id"], bot.CONFIG["nba"]["sleeper_league_id"]
    scenario("nfl:sunday-window-22nov", lambda: bot.run_nfl({"nfl": {}, "nba": {}}, date="20261122", force=True))
    scenario("nfl:thursday-26nov", lambda: bot.run_nfl({"nfl": {}, "nba": {}}, date="20261126", force=True))
    scenario("nba:25nov", lambda: bot.run_nba({"nfl": {}, "nba": {}}, dates=["20261125"], force=True))

    def nba_cards():
        fin = [e for e in bot.fetch("basketball/nba/scoreboard?dates=20261125").get("events", [])
               if e["status"]["type"].get("completed")]
        live = bot.league_live("nba")
        bot.post_nba_single(fin[0], live)
        bot.post_nba_group("FULL-TIME", fin[1:3], live)
        bot.post_nba_group("HALF-TIME", fin[3:5], live)
    scenario("nba:cards", nba_cards)

    def trades():
        for sport, lid, n in (("nfl", nfl_lid, 4), ("nba", nba_lid, 2)):
            ts = []
            for w in range(0, 13):
                ts += [t for t in bot.fetch_url(f"{bot.SLEEPER}/league/{lid}/transactions/{w}") if t["type"] == "trade"]
            for t in sorted(ts, key=lambda t: -t["created"])[:n]:
                bot.post_trade(sport, t)
    scenario("trades", trades)

    def trade_watch():
        st = {}
        bot.maybe_trades(st)                              # first run: remembers, posts nothing
        st["trades_seen"]["nfl"] = st["trades_seen"]["nfl"][:-1]
        bot.maybe_trades(st)                              # one "new" trade: posts it
    scenario("trades:watch", trade_watch)

    def recaps():
        for sport, week in (("nfl", 11), ("nba", 5)):
            bot.post_weekly_recap(sport, week, bot.league_live(sport, week=week), bot.league_settings(sport))
    scenario("recaps", recaps)

    def daily():
        st = json.load(open(ROOT / "state.json")) if (ROOT / "state.json").exists() else {"nfl": {}, "nba": {}}
        bot.maybe_daily_race(st)
        bot.watchdog(st)
    scenario("daily+watchdog", daily)

    json.dump(out, open(out_path, "w"), indent=0, sort_keys=True)
    if mode == "record":
        json.dump(data, open(data_path, "w"))
        print(f"recorded {len(data)} web responses")


if __name__ == "__main__":
    if sys.argv[1] == "compare":
        sys.exit(0 if compare(sys.argv[2], sys.argv[3]) else 1)
    main(sys.argv[1], sys.argv[2], sys.argv[3])
