#!/usr/bin/env python3
"""Render captured Discord embeds as a Discord-style HTML page (for previews / screenshots).

  python3 tools/render.py posts.json out.html
"""
import html
import json
import re
import sys

CSS = """
*{box-sizing:border-box}
body{margin:0;background:#313338;color:#dbdee1;font:16px/1.375 'gg sans','Noto Sans','Helvetica Neue',Helvetica,Arial,sans-serif;width:760px}\n.big{max-height:none!important}
.chan{padding:12px 16px;border-bottom:1px solid #26272b;color:#f2f3f5;font-weight:600;font-size:16px}
.chan span{color:#80848e;margin-right:6px}
.msg{display:flex;gap:16px;padding:10px 16px 6px}
.av{width:40px;height:40px;border-radius:50%;object-fit:cover;background:#d97757;flex:none;display:flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:15px}
.who{color:#f2f3f5;font-weight:600;font-size:16px}
.tag{background:#5865f2;color:#fff;font-size:10px;font-weight:600;padding:1px 4px;border-radius:3px;margin:0 4px;vertical-align:2px}
.time{color:#949ba4;font-size:12px}
.sep{color:#949ba4;font-size:12px;text-align:center;margin:14px 16px 4px;border-top:1px solid #3f4147;line-height:0}
.sep b{background:#313338;padding:0 8px;font-weight:600}
.embed{display:grid;margin-bottom:6px;grid-template-columns:auto 80px;max-width:600px;margin-top:4px;background:#2b2d31;border-radius:4px;border-left:4px solid;padding:8px 16px 16px 12px;column-gap:16px}
.main{min-width:0}
.thumb{width:80px;height:80px;object-fit:contain;margin-top:8px}
.author{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:14px;font-weight:600;color:#f2f3f5}
.author img{width:24px;height:24px;border-radius:50%;object-fit:contain;background:#fff1}
.title{margin-top:8px;font-size:16px;font-weight:600;color:#f2f3f5}
.desc{margin-top:8px;font-size:14px;line-height:1.375rem;white-space:normal}
.desc h3{font-size:20px;margin:4px 0 2px;color:#f2f3f5;line-height:1.3}
.fields{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:8px}
.f{font-size:14px;line-height:1.3rem;min-width:0}
.f.full{grid-column:1/-1}
.fn{font-weight:600;color:#f2f3f5;margin-bottom:2px}
pre{background:#1e1f22;border:1px solid #1e1f22;border-radius:4px;padding:7px;margin:4px 0 0;font:13px/1.2 Consolas,'Andale Mono',monospace;color:#dbdee1;white-space:pre;overflow:hidden}
code{background:#1e1f22;padding:0 3px;border-radius:3px;font:13px Consolas,monospace}
.footer{display:flex;align-items:center;gap:8px;margin-top:8px;font-size:12px;color:#949ba4;grid-column:1/-1}
.footer img{width:20px;height:20px;border-radius:50%;object-fit:contain}
strong{color:#f2f3f5}
.att{display:block;max-width:550px;max-height:none;border-radius:8px;margin-top:6px}
.big{width:100%;max-width:520px;border-radius:8px;margin-top:12px;display:block}
.content{font-size:15px;margin-top:2px}
.desc blockquote,.q{border-left:4px solid #4e5058;padding-left:10px;color:#dbdee1}
em{color:#dbdee1}
"""


def md(text):
    """Small subset of Discord markdown -> HTML."""
    out, parts = [], re.split(r"```(?:\w*\n)?(.*?)```", text or "", flags=re.S)
    for i, part in enumerate(parts):
        if i % 2:
            out.append(f"<pre>{html.escape(part.strip(chr(10)))}</pre>")
            continue
        part = part.strip("\n") if i else part.rstrip("\n")
        if not part:
            continue
        lines = []
        for ln in html.escape(part).split("\n"):
            if ln.startswith("&gt; "):
                lines.append(f"<div class='q'>{ln[5:]}</div>")
                continue
            if ln.startswith("### "):
                lines.append(f"<h3>{ln[4:]}</h3>")
                continue
            ln = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", ln)
            ln = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", ln)
            ln = re.sub(r"`(.+?)`", r"<code>\1</code>", ln)
            lines.append(ln + "<br>")
        out.append("".join(lines).replace("</h3><br>", "</h3>"))
    s = "".join(out)
    return re.sub(r"(<br>)+$", "", s)


def embed_html(e):
    col = f"#{e.get('color', 0x202225):06x}"
    a = e.get("author") or {}
    author = (f"<div class='author'>{'<img src=' + chr(39) + html.escape(a['icon_url']) + chr(39) + '>' if a.get('icon_url') else ''}"
              f"{html.escape(a.get('name', ''))}</div>") if a else ""
    title = f"<div class='title'>{md(e['title'])}</div>" if e.get("title") else ""
    desc = f"<div class='desc'>{md(e['description'])}</div>" if e.get("description") else ""
    fields = ""
    if e.get("fields"):
        fs = []
        for f in e["fields"]:
            cls = "f" if f.get("inline") else "f full"
            name = "" if f["name"] == "​" else md(f["name"])
            val = "" if f["value"] == "​" else md(f["value"])
            fs.append(f"<div class='{cls}'><div class='fn'>{name}</div>{val}</div>")
        fields = f"<div class='fields'>{''.join(fs)}</div>"
    img = e.get("image", {}).get("url")
    image = f"<img class='big' src='{html.escape(img)}'>" if img else ""
    th = e.get("thumbnail", {}).get("url")
    thumb = f"<img class='thumb' src='{html.escape(th)}'>" if th else "<div></div>"
    ft = e.get("footer") or {}
    footer = (f"<div class='footer'>{'<img src=' + chr(39) + html.escape(ft['icon_url']) + chr(39) + '>' if ft.get('icon_url') else ''}"
              f"{html.escape(ft.get('text', ''))}</div>") if ft else ""
    return (f"<div class='embed' style='border-color:{col}'><div class='main'>{author}{title}{desc}{fields}{image}</div>"
            f"{thumb}{footer}</div>")


def page(embeds, labels=None, channel="🏈︱fantasy"):
    body = [f"<div class='chan'><span>#</span>{html.escape(channel)}</div>"]
    for i, e in enumerate(embeds):
        if labels and labels[i]:
            body.append(f"<div class='sep'><b>{html.escape(labels[i])}</b></div>")
        body.append("<div class='msg'><img class='av' src='https://raw.githubusercontent.com/rickth21/sports-bot/main/avatar.png'><div style='min-width:0'>"
                    f"<span class='who'>{html.escape((e.get('username') if isinstance(e, dict) else None) or 'Donald Claude')}</span><span class='tag'>APP</span>"
                    f"<span class='time'>{html.escape((labels or [''] * len(embeds))[i] or '')}</span>"
                    + (f"<div class='content'>{md(e['content'])}</div>" if isinstance(e, dict) and e.get("content") else "")
                    + "".join(f"<img class='att' src='{a}'>" for a in (e.get("attachments") or [] if isinstance(e, dict) else []))
                    + "".join(embed_html(x) for x in (e["embeds"] if isinstance(e, dict) and "embeds" in e
                                                      else (e if isinstance(e, list) else [e])))
                    + "</div></div>")
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}</style></head>"
            f"<body>{''.join(body)}<div style='height:16px'></div>"
            "<script>window.addEventListener('load',()=>document.body.setAttribute('data-h', Math.ceil(document.body.getBoundingClientRect().height)))</script>"
            "</body></html>")


if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    labels = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None
    open(sys.argv[2], "w").write(page(data, labels))
