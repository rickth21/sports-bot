#!/usr/bin/env python3
"""
How big does a card's text look in Discord WITHOUT opening it?

Donald posts each card as a plain image attachment; Discord desktop shows it scaled down to fit a box of about
550 x 350 CSS pixels (never scaled up). On-screen text size = font size x min(550 / width, 350 / height).

  python3 tools/inline_size.py golden.json [scenario-prefix]   # every card in a golden.py output
  python3 tools/inline_size.py card.html                       # one card

Prints, per card: rendered size, the inline scale, the smallest on-screen text and how much of the card's text is
under 11px on screen (the target: none of the meaningful text).
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

BOX_W, BOX_H, MIN_PX = 550, 350, 11.0

PROBE = """<script>addEventListener('load',()=>{const c=document.querySelector('.card');const r=c.getBoundingClientRect();
const W=r.width,H=r.height,s=Math.min(1,%d/W,%d/H);const out=[];
const walk=document.createTreeWalker(c,NodeFilter.SHOW_TEXT);let n;
while((n=walk.nextNode())){const t=n.textContent.trim();if(!t)continue;const el=n.parentElement;const cs=getComputedStyle(el);
if(cs.visibility=='hidden'||cs.display=='none'||parseFloat(cs.opacity)==0)continue;const b=el.getBoundingClientRect();if(!b.width)continue;
out.push([t.slice(0,40),Math.round(parseFloat(cs.fontSize)*s*10)/10,t.length]);}
const p=document.createElement('pre');p.id='probe';p.textContent=JSON.stringify({W:W,H:H,s:s,t:out});document.body.appendChild(p);});</script>"""


def measure(html_text):
    html_text = bot._shape_more(bot._moment_landscape(html_text))
    if hasattr(bot, "readable_inline"):
        html_text = bot.readable_inline(html_text)
    html_text = html_text.replace("</body>", PROBE % (BOX_W, BOX_H) + "</body>")
    chrome = bot.find_chrome()
    m = re.search(r"body,\.card\{width:(\d+)px", html_text)
    width = int(m.group(1)) if m else 880
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "c.html"
        f.write_text(html_text)
        dom = subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=9000",
                              f"--window-size={width},3000", "--dump-dom", f.as_uri()],
                             capture_output=True, text=True, timeout=90).stdout
    m = re.search(r'<pre id="probe">(.*?)</pre>', dom, re.S)
    if not m:
        return None
    import html as H
    return json.loads(H.unescape(m.group(1)))


def report(name, r):
    if not r:
        print(f"{name:34s} (no card)")
        return
    small = [t for t in r["t"] if t[1] < MIN_PX]
    chars = sum(t[2] for t in r["t"]) or 1
    sc = sum(t[2] for t in small)
    mn = min((t[1] for t in r["t"]), default=0)
    print(f"{name:34s} {r['W']:.0f}x{r['H']:.0f}  scale {r['s']:.2f}  smallest {mn:4.1f}px  "
          f"under {MIN_PX:.0f}px: {100 * sc / chars:3.0f}% of text"
          + (f"  e.g. {', '.join(repr(t[0]) + '=' + str(t[1]) for t in small[:4])}" if small else ""))


def main():
    src = sys.argv[1]
    if src.endswith(".html"):
        report(Path(src).name, measure(Path(src).read_text()))
        return
    data = json.load(open(src))
    pre = sys.argv[2] if len(sys.argv) > 2 else ""
    for k, items in data.items():
        if not k.startswith(pre):
            continue
        for i, it in enumerate(x for x in items if "html" in x):
            report(f"{k}#{i}", measure(it["html"]))


if __name__ == "__main__":
    main()
