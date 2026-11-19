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


CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: 390px; height: 844px; overflow: hidden; font-family: -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
.phone { width: 390px; height: 844px; display: flex; flex-direction: column;
  background: linear-gradient(160deg, #c8d9a8 0%, #9fc09a 45%, #86b2a0 100%); }
.status { height: 44px; background: #fff; display: flex; align-items: center; justify-content: space-between; padding: 0 22px;
  font-size: 15px; font-weight: 600; color: #000; }
.header { height: 56px; background: #fff; border-bottom: 1px solid #e3e3e3; display: flex; align-items: center; gap: 10px; padding: 0 12px; }
.header .back { color: #2f8de4; font-size: 26px; width: 18px; }
.avatar { width: 38px; height: 38px; border-radius: 50%; color: #fff; font-weight: 700; display: flex; align-items: center;
  justify-content: center; font-size: 15px; }
.header .name { font-size: 16px; font-weight: 600; color: #000; }
.header .sub { font-size: 13px; color: #8e8e93; }
.chat { flex: 1; overflow: hidden; display: flex; flex-direction: column; justify-content: flex-end; padding: 8px 8px 6px; gap: 6px; }
.row { display: flex; flex-direction: column; }
.row.user { align-items: flex-end; }
.row.bot { align-items: flex-start; }
.bubble { max-width: 84%; padding: 7px 10px 6px; border-radius: 16px; font-size: 14.5px; line-height: 1.34; color: #000;
  box-shadow: 0 1px 1px rgba(0,0,0,.12); word-wrap: break-word; overflow-wrap: anywhere; }
.row.bot .bubble { background: #fff; border-bottom-left-radius: 5px; }
.row.user .bubble { background: #e1fec6; border-bottom-right-radius: 5px; }
.bubble a { color: #2f8de4; text-decoration: none; }
.bubble code { font-family: Consolas, monospace; font-size: 13.5px; }
.bubble .meta { float: right; font-size: 11px; color: #8e8e93; margin: 6px 0 0 10px; }
.row.user .bubble .meta { color: #5fa34f; }
.doc { display: flex; gap: 10px; align-items: center; margin-bottom: 4px; }
.doc .ico { width: 40px; height: 40px; border-radius: 50%; background: #3f9be3; color: #fff; display: flex; align-items: center; justify-content: center; font-size: 18px; }
.contact { display: flex; gap: 10px; align-items: center; }
.contact .ico { width: 40px; height: 40px; border-radius: 50%; background: #f2a33a; color: #fff; display: flex; align-items: center; justify-content: center; font-weight: 700; }
.ikb { width: 84%; margin-top: 3px; display: flex; flex-direction: column; gap: 3px; }
.ikb .r { display: flex; gap: 3px; }
.ikb .b { flex: 1; background: rgba(40, 70, 50, .32); color: #fff; font-size: 13.5px; font-weight: 500; text-align: center;
  padding: 9px 6px; border-radius: 9px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.input { height: 50px; background: #fff; border-top: 1px solid #e3e3e3; display: flex; align-items: center; gap: 10px; padding: 0 12px; color: #8e8e93; font-size: 16px; }
.input .field { flex: 1; height: 34px; border: 1px solid #dcdcdc; border-radius: 17px; padding: 0 14px; display: flex; align-items: center; }
.rkb { background: #d4d7dc; padding: 6px 4px 10px; display: flex; flex-direction: column; gap: 6px; }
.rkb .r { display: flex; gap: 6px; }
.rkb .b { flex: 1; background: #fff; border-radius: 7px; padding: 11px 4px; text-align: center; font-size: 15px; color: #000;
  box-shadow: 0 1px 0 rgba(0,0,0,.25); }
.rkb .b.contact-btn { color: #2f8de4; font-weight: 600; }
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
        rows.append(f'<div class="row {bubble.sender}"><div class="bubble">{doc}{body}<span class="meta">{meta}</span></div>{kb}</div>')
    bottom = '<div class="input"><span style="font-size:22px">📎</span><div class="field">Message</div><span style="font-size:20px">🎤</span></div>'
    if shot.reply_keyboard:
        rk = "".join(
            '<div class="r">' + "".join(
                f'<div class="b{" contact-btn" if contact else ""}">{html.escape(text)}</div>' for text, contact in row
            ) + "</div>"
            for row in shot.reply_keyboard
        )
        bottom += f'<div class="rkb">{rk}</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="phone">
  <div class="status"><span>{t.strftime("%H:%M")}</span><span>▮▮▮ ◔</span></div>
  <div class="header"><span class="back">‹</span><div class="avatar" style="background:linear-gradient(135deg,{c1},{c2})">{letters}</div>
    <div><div class="name">{name}</div><div class="sub">bot</div></div></div>
  <div class="chat">{"".join(rows)}</div>
  {bottom}
</div></body></html>"""


def screenshot(html_path: Path, png_path: Path, profile: Path) -> None:
    subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
         "--force-device-scale-factor=3", "--window-size=390,844", f"--screenshot={png_path}", html_path.as_uri()],
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
