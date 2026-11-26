#!/usr/bin/env python3
"""
Before/after mock-ups of in-game cards at the size Discord shows them in the channel (no click):
plain image attachments fit a ~550 x 350 box. Real CHI @ DET data (26 Nov 2026).

  python3 tools/sim_bigger.py OUT_DIR [halftime.html]   # halftime.html: a half-time card's HTML (e.g. from golden.py)
Writes bigger-<card>.png (before above, after below, both at inline size) and bigger-<card>-full.png (after, full size),
plus bigger-<card>-anim.gif for the big-game cards (the animated version Discord gets).
"""
import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

EVENT = "401873081"
PAGE = """<!doctype html><html><head><meta charset='utf-8'><style>
body{margin:0;background:#313338;font:16px/1.4 'gg sans','Helvetica Neue',Arial,sans-serif;color:#dbdee1;width:660px}
.m{display:flex;gap:16px;padding:14px 16px 6px}.av{width:40px;height:40px;border-radius:50%;flex:none}
.who{color:#f2f3f5;font-weight:600}.tag{background:#5865f2;color:#fff;font-size:10px;font-weight:600;padding:1px 4px;border-radius:3px;margin:0 4px}
.t{color:#949ba4;font-size:12px}.lab{color:#f2f3f5;font-weight:800;font-size:13px;letter-spacing:1px;padding:10px 16px 0}
img.a{display:block;max-width:550px;max-height:350px;width:auto;height:auto;border-radius:8px;margin-top:6px}
</style></head><body>{BODY}</body></html>"""


def msg(label, png_path, who):
    return (f"<div class='lab'>{label}</div><div class='m'><img class='av' "
            f"src='https://raw.githubusercontent.com/rickth21/sports-bot/main/avatar.png'><div><span class='who'>{who}</span>"
            f"<span class='tag'>APP</span><span class='t'>Today at 20:51</span><img class='a' src='{png_path.as_uri()}'></div></div>")


def shot(html_text, out):
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "p.html"
        f.write_text(html_text)
        subprocess.run([bot.find_chrome(), "--headless=new", "--no-sandbox", "--hide-scrollbars", "--force-device-scale-factor=2",
                        "--window-size=660,1900", f"--screenshot={out}", "--virtual-time-budget=4000", f.as_uri()],
                       check=True, capture_output=True, timeout=60)
    from PIL import Image
    im = Image.open(out)                                    # trim the empty bottom
    px = im.convert("RGB").load()
    h = im.height
    while h > 10 and all(px[x, h - 1] == (49, 51, 56) for x in range(0, im.width, 9)):
        h -= 1
    im.crop((0, 0, im.width, min(im.height, h + 24))).save(out)


def main(out, ht_path=None):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    bot.CONFIG["animations"] = {"enabled": False}
    bot.NFL_HOOK = "sim"
    evs = bot.fetch("football/nfl/scoreboard?seasontype=2&week=12").get("events", [])
    ev = copy.deepcopy(next(e for e in evs if e["id"] == EVENT))
    ev["status"]["type"].update(state="in", completed=False)
    summ = bot.fetch(f"football/nfl/summary?event={EVENT}")
    st = {"nfl": {}, "nba": {}}
    bot._NFL_ST["st"] = st
    ctx = bot.nfl_ctx(st, evs)
    real_rc = bot.render_card
    grab = []

    def html_of(fn):
        grab.clear()
        bot.render_card = lambda h, w=None: (grab.append(h), b"x" * 30000)[1]
        bot.CAPTURE = []
        try:
            fn()
        finally:
            bot.render_card, bot.CAPTURE = real_rc, None
        return grab[-1]

    def play(text_start):
        return next(p for p in summ["scoringPlays"] if p["text"].startswith(text_start))

    def td(p, full):
        q = p if full else bot.td_only_play(p, summ)
        tag = bot.td_try(p, summ) if full else {"label": "⏳ CONVERSION TO COME", "cls": "wait"}

        def go():
            bot._TRY_TAG["tag"] = tag
            try:
                bot._post_td_track(ev, q, summ, ctx)
            finally:
                bot._TRY_TAG["tag"] = None
        return html_of(go)

    cards = {
        "swift-td": (td(play("D'Andre Swift"), False), "Donald 🏈"),
        "laporta-td-3-rows": (td(play("Sam LaPorta"), True), "Donald 🏈"),
        "fg": (html_of(lambda: bot.post_fg_clutch(ev, play("Jake Bates 40"), summ, ctx)), "Donald 🏈"),
    }
    # big games: Gibbs' 70-yard TD (43.2 by full-time) and his earlier TD, when he stood at about 30
    cards["gibbs-40-monster"] = (td(play("Jahmyr Gibbs 70"), True), "Donald 🏈")
    real_now = bot.player_now
    bot.player_now = lambda s, n: 30.2 if "Gibbs" in n else real_now(s, n)
    try:
        cards["gibbs-30-big-game"] = (td([p for p in summ["scoringPlays"] if p["text"].startswith("Jahmyr Gibbs 1")][1], True),
                                      "Donald 🏈")
    finally:
        bot.player_now = real_now
    if ht_path:
        cards["halftime"] = (Path(ht_path).read_text(), "Donald 🏈")
    for name, (h, who) in cards.items():
        b, a = tmp / f"{name}-before.png", tmp / f"{name}-after.png"
        b.write_bytes(bot._render_card_small(h))
        a.write_bytes(bot.render_card(h))
        if name.startswith("gibbs"):
            gif = bot.render_anim(h)
            if gif:
                (out / f"bigger-{name}-anim.gif").write_bytes(gif)
        (out / f"bigger-{name}-full.png").write_bytes(a.read_bytes())
        shot(PAGE.replace("{BODY}", msg("BEFORE (as it looks now)", b, who) + msg("AFTER", a, who)), out / f"bigger-{name}.png")
        print("wrote", out / f"bigger-{name}.png")
    # NBA weekly board (lock-in), new size; "before" = the board as posted from main
    w = int(bot.fetch_url(f"{bot.SLEEPER}/state/nba")["week"])
    d = bot.wb_nba_data(w)
    a = tmp / "board-after.png"
    a.write_bytes(bot.render_card(bot.wb_html(d)))
    (out / "bigger-nba-board-full.png").write_bytes(a.read_bytes())
    before = out / "nba-lockin-fixed.png"
    body = (msg("BEFORE (as it looks now)", before, "Donald 🏀") if before.exists() else "") + msg("AFTER", a, "Donald 🏀")
    shot(PAGE.replace("{BODY}", body), out / "bigger-nba-board.png")
    print("wrote", out / "bigger-nba-board.png")


if __name__ == "__main__":
    main(*sys.argv[1:])
