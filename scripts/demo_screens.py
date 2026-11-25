"""Renders README / portfolio screenshots from scripted demo conversations.

The bot runs its real handlers, retrieval (local embeddings), SQL catalog search, lead flow and database against
the in-process fake Telegram API used by the tests. The only scripted part is the model's wording (offline demo
provider); every source, card, button and lead on the screens is real bot output. Names and data are fictional.

Usage: python scripts/demo_screens.py  ->  docs/screens/<vertical>_*.png (1170x2532, phone-sized)
"""
from __future__ import annotations

import asyncio
import copy
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.bot.handlers import router  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import init_db, make_engine, make_session_maker  # noqa: E402
from app.engine import build_agent  # noqa: E402
from app.main import build_dispatcher  # noqa: E402
from app.rag import Embedder  # noqa: E402
from tests.fake_telegram import FakeTelegram, Harness  # noqa: E402

OUT = ROOT / "docs" / "screens"
# Chat wallpaper: own doodle tile (simple line icons, low opacity) over the green gradient, like Telegram's default.
_DOODLES = [
    # (x, y, rotate, path in a 24x24 box)
    (8, 10, -15, "M12 3l2.6 5.6 6 .6-4.5 4 1.3 6-5.4-3.1-5.4 3.1 1.3-6-4.5-4 6-.6z"),            # star
    (70, 6, 10, "M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"),          # heart
    (132, 14, -8, "M5 8h11v6a5 5 0 0 1-5 5h-1a5 5 0 0 1-5-5zM16 10h2a2 2 0 0 1 0 4h-2M8 3v3M11 3v3"),  # cup
    (190, 4, 20, "M4 20c8 0 16-6 16-16C10 4 4 10 4 20zM4 20l9-9"),                               # leaf
    (30, 66, 0, "M3 12a9 9 0 1 0 18 0 9 9 0 1 0-18 0M12 7v5l3 2"),                                # clock
    (98, 60, -20, "M4 6h16v10H8l-4 4z"),                                                          # chat bubble
    (160, 70, 15, "M12 3v18M3 12h18M6 6l12 12M18 6L6 18"),                                        # sparkle
    (214, 58, -10, "M6 3h12v18l-6-4-6 4z"),                                                       # bookmark
    (6, 128, 25, "M3 17l6-6 4 4 8-8M15 7h6v6"),                                                   # chart
    (64, 118, -5, "M12 2a6 6 0 0 1 6 6c0 4.5-6 12-6 12S6 12.5 6 8a6 6 0 0 1 6-6zM12 6a2 2 0 1 0 0 4 2 2 0 1 0 0-4"),  # pin
    (126, 130, 12, "M3 7h18v12H3zM8 7V5h8v2"),                                                    # bag
    (188, 120, -18, "M9 18V6l10-2v12M9 18a3 3 0 1 1-6 0 3 3 0 1 1 6 0M19 16a3 3 0 1 1-6 0 3 3 0 1 1 6 0"),  # note
    (36, 184, 8, "M2 12c3-4 6-4 10 0s7 4 10 0"),                                                  # wave
    (100, 186, -12, "M4 4h16v12H4zM2 20h20"),                                                     # laptop
    (160, 182, 18, "M12 2l3 7h7l-5.5 4.5L18 21l-6-4-6 4 1.5-7.5L2 9h7z"),                         # star 2
    (216, 178, -6, "M12 4a8 8 0 1 0 0 16 8 8 0 1 0 0-16M9 10h.01M15 10h.01M8.5 14.5a4.5 4.5 0 0 0 7 0"),  # smile
]
_TILE = ('<svg xmlns="http://www.w3.org/2000/svg" width="248" height="236" viewBox="0 0 248 236">'
         + "".join(f'<g transform="translate({x} {y}) rotate({r} 12 12) scale(1.05)" fill="none" stroke="#2f5a2a" '
                   f'stroke-opacity=".16" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="{d}"/></g>'
                   for x, y, r, d in _DOODLES)
         + "</svg>")
import base64 as _b64  # noqa: E402
WALLPAPER = "data:image/svg+xml;base64," + _b64.b64encode(_TILE.encode()).decode()
CHROME = os.environ.get("CHROME", r"C:\Program Files\Google\Chrome\Application\chrome.exe")
ADMIN_ID = 100
HEADERS = {"realty": ("Northwind Homes", "NH", "#2e7d5b", "#4fb38a"), "shop": ("Northwind Audio", "NA", "#5b3fc4", "#8c6cf0")}


@dataclass
class Snapshot:
    name: str
    title: str
    bubbles: list
    reply_keyboard: list[list[tuple[str, bool]]] | None


def snap(env: Harness, user, name: str, title: str, last_n: int | None = None) -> Snapshot:
    chat = env.chat(user)
    bubbles = copy.deepcopy(chat.bubbles[-last_n:] if last_n else chat.bubbles)
    kb = None
    if chat.reply_keyboard:
        kb = [[(b.text, bool(b.request_contact)) for b in row] for row in chat.reply_keyboard.keyboard]
    return Snapshot(name, title, bubbles, kb)


def card_bubble(env: Harness, user, needle: str):
    return next(b for b in env.chat(user).bot_bubbles() if needle in b.text)


async def realty(env: Harness) -> list[Snapshot]:
    shots = []
    owner, anna = env.user(ADMIN_ID, "Owner", "owner_demo"), env.user(201, "Anna", "anna_k")
    await env.start(anna)
    shots.append(snap(env, anna, "realty_01_welcome", "Welcome with sample questions"))
    await env.send(anna, "2-bedroom to rent in Chiado up to €2,400, I have a dog")
    shots.append(snap(env, anna, "realty_02_search", "Free-text request → structured search → listing card", last_n=3))
    await env.send(anna, "Do tenants pay an agency fee?")
    await env.send(anna, "What documents do I need to rent?")
    shots.append(snap(env, anna, "realty_03_rag", "Answers from the agency's documents, with sources", last_n=4))
    await env.click_in(anna, card_bubble(env, anna, "Elegant 2-bedroom with balcony · Chiado"), "📅 Book a viewing")
    await env.send_contact(anna, "+351 912 345 678")
    shots.append(snap(env, anna, "realty_04_booking", "Viewing request: phone via Telegram's contact button", last_n=4))
    shots.append(snap(env, owner, "realty_05_lead", "The agent gets the lead with the conversation", last_n=1))
    olena = env.user(202, "Олена", None, "uk")
    await env.send(olena, "Шукаю двокімнатну в Шіаду до 2400 євро, маю собаку")
    await env.send(olena, "What's the weather tomorrow?")
    shots.append(snap(env, olena, "realty_06_language", "Replies in the client's language; stays on topic", last_n=5))
    await env.send(owner, "/admin")
    shots.append(snap(env, owner, "realty_07_admin", "Owner report: questions, hand-offs, leads, token cost", last_n=1))
    return shots


async def shop(env: Harness) -> list[Snapshot]:
    shots = []
    owner, tom = env.user(ADMIN_ID, "Owner", "owner_demo"), env.user(301, "Tom", "tom_r")
    await env.start(tom)
    shots.append(snap(env, tom, "shop_01_welcome", "Welcome with sample questions"))
    await env.send(tom, "Earbuds for running under €130")
    shots.append(snap(env, tom, "shop_02_search", "Needs → filters + semantic ranking → product cards", last_n=5))
    await env.send(tom, "How long does delivery to Ukraine take?")
    await env.send(tom, "Can I return earbuds I already opened?")
    shots.append(snap(env, tom, "shop_03_rag", "Delivery and return rules from the store's documents", last_n=4))
    await env.click_in(tom, card_bubble(env, tom, "Pulse Run Open · Pulse"), "🛒 Order")
    await env.send_contact(tom, "+48 600 100 200")
    shots.append(snap(env, tom, "shop_04_order", "Order request with one tap", last_n=4))
    shots.append(snap(env, owner, "shop_05_lead", "The team gets the order lead with the conversation", last_n=1))
    await env.send(tom, "Do you sell cables?")
    await env.send(tom, "I'd like to talk to a person")
    shots.append(snap(env, tom, "shop_06_handoff", "Honest about limits, hands off to a person", last_n=5))
    await env.send(owner, "/admin")
    shots.append(snap(env, owner, "shop_07_admin", "Owner report: questions, hand-offs, leads, token cost", last_n=1))
    return shots


# Android Telegram look (default "Day" theme): Roboto + Noto Color Emoji, blue app bar, flat SVG icons.
FONTS = ('<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&family=Noto+Color+Emoji&display=block" '
         'rel="stylesheet">')
_ICON = ('<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="{c}" stroke-width="2" '
         'stroke-linecap="round" stroke-linejoin="round">{d}</svg>')
ICON_BACK = _ICON.format(s=24, c="#fff", d='<path d="M20 12H5"/><path d="M11 5l-7 7 7 7"/>')
ICON_MORE = ('<svg width="24" height="24" viewBox="0 0 24 24" fill="#fff"><circle cx="12" cy="5" r="2"/><circle cx="12" cy="12" r="2"/>'
             '<circle cx="12" cy="19" r="2"/></svg>')
ICON_SMILE = _ICON.format(s=26, c="#8a959e", d='<circle cx="12" cy="12" r="9.5"/><path d="M8 14.5a5 5 0 0 0 8 0"/>'
                          '<path d="M9 9.5h.01M15 9.5h.01" stroke-width="3"/>')
ICON_CLIP = _ICON.format(s=26, c="#8a959e", d='<path d="M21 11.5l-8.6 8.6a5.5 5.5 0 0 1-7.8-7.8l8.6-8.6a3.7 3.7 0 0 1 5.2 5.2'
                         'l-8.6 8.6a1.8 1.8 0 0 1-2.6-2.6l7.9-7.9"/>')
ICON_MIC = _ICON.format(s=26, c="#8a959e", d='<rect x="9" y="2.5" width="6" height="11.5" rx="3"/>'
                        '<path d="M5.5 10.5a6.5 6.5 0 0 0 13 0"/><path d="M12 17v4"/>')
STATUS_ICONS = (
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="#fff"><path d="M12 21l11.5-14.3A18.6 18.6 0 0 0 12 2.6 18.6 18.6 0 0 0 .5 6.7z"/></svg>'
    '<svg width="15" height="15" viewBox="0 0 24 24" fill="#fff"><path d="M2 22h20V2z"/></svg>'
    '<svg width="9" height="15" viewBox="0 0 12 20" fill="#fff"><rect x="3.5" y="0" width="5" height="2" rx="1"/>'
    '<rect x="0" y="2" width="12" height="18" rx="2"/></svg>')
CHECKS = ('<svg width="16" height="10" viewBox="0 0 16 10" fill="none" stroke="#4fae4e" stroke-width="1.6" stroke-linecap="round" '
          'stroke-linejoin="round"><path d="M1 5.5l3 3 6-7"/><path d="M7 8.5l1 0 6-7"/></svg>')
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: 390px; height: 844px; overflow: hidden; font-family: Roboto, "Noto Color Emoji", sans-serif;
  -webkit-font-smoothing: antialiased; }
.phone { width: 390px; height: 844px; display: flex; flex-direction: column;
  background: url("{WALLPAPER}") repeat, linear-gradient(165deg, #d5e3b0 0%, #a9cba0 40%, #8fbcaa 75%, #7fb0ac 100%);
  background-size: 196px 186px, auto; }
.status { height: 26px; background: #436d8c; display: flex; align-items: center; justify-content: space-between;
  padding: 0 14px 0 16px; font-size: 13px; font-weight: 500; color: #fff; }
.status .icons { display: flex; gap: 5px; align-items: center; }
.header { height: 56px; background: #517da2; display: flex; align-items: center; gap: 14px; padding: 0 12px 0 14px;
  box-shadow: 0 1px 3px rgba(0,0,0,.18); z-index: 1; }
.header .back, .header .more { display: flex; align-items: center; }
.header .more { margin-left: auto; }
.avatar { width: 40px; height: 40px; border-radius: 50%; background: linear-gradient(180deg, #72d5fd, #2a9ef1);
  color: #fff; font-weight: 500; display: flex; align-items: center; justify-content: center; font-size: 16px; }
.header .name { font-size: 18px; font-weight: 500; color: #fff; line-height: 1.2; }
.header .sub { font-size: 14px; color: #d7e4ef; }
.chat { flex: 1; overflow: hidden; display: flex; flex-direction: column; justify-content: flex-end; padding: 8px 8px 8px; gap: 5px; }
.row { display: flex; flex-direction: column; }
.row.user { align-items: flex-end; }
.row.bot { align-items: flex-start; }
.bubble { max-width: 84%; padding: 6px 9px 6px 10px; border-radius: 12px; font-size: 15.5px; line-height: 1.32; color: #000;
  box-shadow: 0 1px 1.5px rgba(0,0,0,.16); word-wrap: break-word; overflow-wrap: anywhere; }
.row.bot .bubble { background: #fff; border-bottom-left-radius: 4px; }
.row.user .bubble { background: #effedd; border-bottom-right-radius: 4px; }
.bubble a { color: #2678b6; text-decoration: none; }
.bubble .meta { float: right; font-size: 12px; color: #a1aab3; margin: 7px 0 -2px 10px; display: inline-flex; gap: 3px; align-items: center; }
.row.user .bubble .meta { color: #6aaf5e; }
.doc { display: flex; gap: 10px; align-items: center; margin-bottom: 4px; }
.doc .ico { width: 44px; height: 44px; border-radius: 50%; background: #4fa4e8; color: #fff; display: flex; align-items: center;
  justify-content: center; font-size: 20px; }
.doc .fn { font-weight: 500; font-size: 14.5px; }
.ikb { max-width: 84%; width: 84%; margin-top: 3px; display: flex; flex-direction: column; gap: 3px; }
.ikb .r { display: flex; gap: 3px; }
.ikb .b { flex: 1; background: rgba(40, 72, 60, .30); color: #fff; font-size: 14.5px; font-weight: 500; text-align: center;
  padding: 10px 6px; border-radius: 8px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; position: relative; }
.ikb .b.url::after { content: ""; position: absolute; top: 5px; right: 5px; width: 6px; height: 6px;
  border-top: 1.5px solid #fff; border-right: 1.5px solid #fff; }
.input { height: 52px; background: #fff; display: flex; align-items: center; gap: 12px; padding: 0 12px;
  box-shadow: 0 -1px 2px rgba(0,0,0,.08); }
.input .field { flex: 1; color: #a3abb3; font-size: 17px; }
.rkb { background: #f0f2f4; padding: 6px 6px 10px; display: flex; flex-direction: column; gap: 6px; border-top: 1px solid #e1e4e7; }
.rkb .r { display: flex; gap: 6px; }
.rkb .b { flex: 1; background: #fff; border-radius: 6px; padding: 12px 4px; text-align: center; font-size: 15px; color: #222;
  box-shadow: 0 1px 1px rgba(0,0,0,.18); }
.alert { position: absolute; left: 50%; top: 42%; transform: translateX(-50%); background: rgba(30,30,30,.86); color: #fff;
  font-size: 15px; padding: 12px 18px; border-radius: 12px; max-width: 300px; text-align: center; }
.bubble code { font-family: "Roboto Mono", Consolas, monospace; font-size: 13.5px; }
.contact { display: flex; gap: 10px; align-items: center; }
.contact .ico { width: 44px; height: 44px; border-radius: 50%; background: #f2a33a; color: #fff; display: flex; align-items: center;
  justify-content: center; font-weight: 500; font-size: 18px; }
.rkb .b.contact-btn { color: #2678b6; font-weight: 500; }
.avatar { font-size: 16px; }
"""


def render_html(shot: Snapshot, vertical: str, clock: datetime) -> str:
    name, letters, c1, c2 = HEADERS[vertical]
    rows = []
    t = clock
    for bubble in shot.bubbles:
        t += timedelta(minutes=1) if bubble.sender == "user" else timedelta(seconds=15)
        meta = ("edited " if bubble.edited else "") + t.strftime("%H:%M")
        if bubble.sender == "user":
            if bubble.text.startswith("📱 "):
                phone = html.escape(bubble.text[2:].strip())
                body = f'<div class="contact"><div class="ico">A</div><div><b>Contact</b><br>{phone}</div></div>'
            else:
                text = "/start" if bubble.text.startswith("/start ") else bubble.text
                body = html.escape(text).replace("\n", "<br>")
        else:
            body = bubble.text.replace("\n", "<br>")
        doc = ""
        if bubble.document:
            doc = (f'<div class="doc"><div class="ico">⬇</div><div><b>{html.escape(bubble.document[0])}</b><br>'
                   f'<span style="font-size:12px;color:#8e8e93">{len(bubble.document[1]) / 1024:.1f} KB</span></div></div>')
        kb = ""
        if bubble.markup:
            kb_rows = ["".join(f'<div class="b">{html.escape(b.text)}</div>' for b in row) for row in bubble.markup.inline_keyboard]
            kb = '<div class="ikb">' + "".join(f'<div class="r">{r}</div>' for r in kb_rows) + "</div>"
        rows.append(f'<div class="row {bubble.sender}"><div class="bubble">{doc}{body}<span class="meta">{meta}{CHECKS if bubble.sender == "user" else ""}</span></div>{kb}</div>')
    bottom = f'<div class="input">{ICON_SMILE}<div class="field">Message</div>{ICON_CLIP}{ICON_MIC}</div>'
    if shot.reply_keyboard:
        rk = "".join(
            '<div class="r">' + "".join(
                f'<div class="b{" contact-btn" if contact else ""}">{html.escape(text)}</div>' for text, contact in row
            ) + "</div>"
            for row in shot.reply_keyboard
        )
        bottom += f'<div class="rkb">{rk}</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8">{FONTS}<style>{CSS.replace("{WALLPAPER}", WALLPAPER)}</style></head><body>
<div class="phone">
  <div class="status"><span>{t.strftime("%H:%M")}</span><span class="icons">{STATUS_ICONS}</span></div>
  <div class="header"><span class="back">{ICON_BACK}</span><div class="avatar" style="background:linear-gradient(180deg,{c1},{c2})">{letters}</div>
    <div><div class="name">{name}</div><div class="sub">bot</div></div><span class="more">{ICON_MORE}</span></div>
  <div class="chat">{"".join(rows)}</div>
  {bottom}
</div></body></html>"""


def screenshot(html_path: Path, png_path: Path, profile: Path) -> None:
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
         "--force-device-scale-factor=3", "--window-size=390,844", "--virtual-time-budget=8000",
         f"--screenshot={png_path}", html_path.as_uri()],
        check=True, capture_output=True, timeout=60,
    )


async def run_vertical(vertical: str, scenario, embedder: Embedder, tmp: Path) -> list[Snapshot]:
    settings = Settings(bot_token="42:DEMO", vertical=vertical, admin_ids=frozenset({ADMIN_ID}), data_dir=tmp,
                        database_url=f"sqlite+aiosqlite:///{(tmp / f'{vertical}.db').as_posix()}")
    engine = make_engine(settings.database_url)
    await init_db(engine)
    session_maker = make_session_maker(engine)
    agent = await build_agent(settings, session_maker, embedder)
    env = Harness(build_dispatcher(settings, session_maker, agent), FakeTelegram())
    try:
        return await scenario(env)
    finally:
        router._parent_router = None
        await engine.dispose()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="assistant_demo_"))
    embedder = Embedder(cache_dir=str(ROOT / "data" / "models"))
    OUT.mkdir(parents=True, exist_ok=True)
    captions = []
    clock = datetime.now().replace(hour=10, minute=12, second=0, microsecond=0)
    for vertical, scenario in (("realty", realty), ("shop", shop)):
        for shot in await run_vertical(vertical, scenario, embedder, tmp):
            page = tmp / f"{shot.name}.html"
            page.write_text(render_html(shot, vertical, clock), encoding="utf-8")
            screenshot(page, OUT / f"{shot.name}.png", tmp / "chrome")
            captions.append(f"{shot.name}: {shot.title}")
            print(f"{shot.name}.png  {shot.title}")
    (OUT / "captions.txt").write_text("\n".join(captions) + "\n", encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
