"""HTTP API for the website chat widget (FastAPI).

Same engine as the Telegram bot: every rule (rate limit, history, guardrails, leads, usage report) lives in
`app.chat` and `app.agent`; this module only translates HTTP <-> those services.

- POST /api/chat       one visitor question -> reply, cited sources, item cards, hand-off flag
- POST /api/lead       phone number for a card action ("book:NH-103", "order:NA-07") or "human"
- GET  /api/config     persona, welcome text, sample questions, lead prompts (the widget builds its UI from this)
- GET  /api/health     liveness + database check
- GET  /api/admin/...  usage report and leads CSV, only with the X-Admin-Key header (off unless ADMIN_API_KEY is set)
- GET  /widget.js, /  the embeddable script and the demo site of the configured business
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from html import unescape
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import Agent, AgentResult
from app.bot import texts
from app.chat import PHONE_RE, collect_stats, converse, create_lead, recent_messages
from app.config import Settings
from app.db import Lead, User
from app.verticals.base import Card
from app.web.notify import LeadNotifier

WA_USER_OFFSET = 10**16  # WhatsApp users: phone number + this offset (Telegram ids < 2^52 ~ 4.5e15, widget ids are negative)

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
SESSION_RE = r"^[A-Za-z0-9_-]{16,64}$"
ACTION_RE = re.compile(r"^(book|order):[A-Z]{2}-\d+$")

FAILED_TEXT = "I couldn't answer right now. A colleague can help: please leave your phone number below."
RATE_LIMITED_TEXT = "You've sent a lot of messages in the last hour. Please try again a bit later, or ask for a person."
LEAD_THANKS = "Thank you! We'll contact you at {phone} within 2 working hours."


# ------------------------------------------------------------------ schemas

class ChatIn(BaseModel):
    session_id: str = Field(pattern=SESSION_RE)
    message: str = Field(min_length=1, max_length=1000)


class SourceOut(BaseModel):
    n: int
    title: str


class ButtonOut(BaseModel):
    text: str
    action: str


class CardOut(BaseModel):
    id: str
    title: str
    lines: list[str]
    buttons: list[ButtonOut]


class ChatOut(BaseModel):
    status: Literal["ok", "rate_limited", "failed"]
    reply: str
    sources: list[SourceOut] = []
    cards: list[CardOut] = []
    handoff: bool = False


class LeadIn(BaseModel):
    session_id: str = Field(pattern=SESSION_RE)
    action: str = Field(max_length=40)  # "human" or "book:NH-103" / "order:NA-07"
    phone: str = Field(max_length=40)
    name: str = Field(default="", max_length=80)


class LeadOut(BaseModel):
    ok: bool
    message: str


# ------------------------------------------------------------------ helpers

def html_to_markdown(value: str) -> str:
    """Card lines and welcome texts are Telegram HTML written by our code: keep <b> as **bold**, drop the rest."""
    value = re.sub(r"</?b>", "**", value)
    value = re.sub(r"<[^>]+>", "", value)
    return unescape(value)


def card_out(card: Card) -> CardOut:
    return CardOut(
        id=card.item_id, title=card.title, lines=[html_to_markdown(line) for line in card.lines],
        buttons=[ButtonOut(text=b.text, action=b.action) for b in card.buttons],
    )


def sources_out(result: AgentResult) -> list[SourceOut]:
    numbers = list(dict.fromkeys(int(m) for m in re.findall(r"\[(\d{1,2})\]", result.text)))
    return [SourceOut(n=n, title=h.chunk.title) for n, h in zip(numbers, result.sources)]


def visitor_id(session_id: str) -> int:
    """Stable negative user id for a widget session (Telegram ids are positive, so the two never collide)."""
    return -(int(hashlib.sha256(session_id.encode()).hexdigest()[:15], 16) + 1)


class HourlyLimiter:
    """Sliding one-hour window per key (client IP). In memory: fine for one process; use Redis for several."""

    def __init__(self, limit: int):
        self.limit = limit
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        window = self.hits[key]
        while window and window[0] <= now - 3600:
            window.popleft()
        if len(window) >= self.limit:
            return False
        window.append(now)
        if len(self.hits) > 10_000:  # forget idle clients
            for k in [k for k, w in self.hits.items() if not w or w[-1] <= now - 3600]:
                del self.hits[k]
        return True


# ------------------------------------------------------------------ app

def create_app(
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
    agent: Agent,
    notifier: LeadNotifier | None = None,
) -> FastAPI:
    app = FastAPI(title=f"{agent.vertical.company} website assistant", docs_url=None, redoc_url=None, openapi_url=None)
    vertical = agent.vertical
    ip_limiter = HourlyLimiter(settings.ip_rate_limit_per_hour)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.allowed_origins), allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Admin-Key"], max_age=600,
    )

    @asynccontextmanager
    async def unit_of_work():
        async with session_maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        """Browsers send Origin on cross-site POSTs: only allowed sites (or this server itself) may use the chat."""
        origin = request.headers.get("origin")
        if request.method == "POST" and origin:
            same_site = origin.split("://", 1)[-1] == request.headers.get("host")
            if not same_site and origin.rstrip("/") not in settings.allowed_origins:
                return JSONResponse({"detail": "origin not allowed"}, status_code=403)
        return await call_next(request)

    async def get_visitor(session: AsyncSession, session_id: str, request: Request) -> User:
        uid = visitor_id(session_id)
        user = await session.get(User, uid)
        if user is None:
            language = (request.headers.get("accept-language") or "")[:2].lower() or None
            user = User(id=uid, username=None, first_name="Website visitor", language=language)
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:  # two first requests of the same visitor at once
                await session.rollback()
                user = await session.get(User, uid)
        return user

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    # ---- public API

    @app.get("/api/health")
    async def health():
        async with session_maker() as session:
            await session.execute(text("select 1"))
        return {"status": "ok", "business": vertical.company, "provider": agent.provider.name,
                "knowledge_sections": len(agent.index.chunks)}

    @app.get("/api/config")
    async def config():
        return {
            "business": vertical.company,
            "assistant": vertical.assistant_name,
            "welcome": html_to_markdown(vertical.welcome),
            "examples": vertical.examples,
            "human_button": vertical.human_button,
            "lead_prompts": {k: a.prompt for k, a in vertical.lead_actions.items()}
            | {"human": html_to_markdown(texts.ASK_PHONE_HUMAN)},
        }

    @app.post("/api/chat")
    async def chat(body: ChatIn, request: Request):
        if not ip_limiter.allow(client_ip(request)):
            return JSONResponse(
                ChatOut(status="rate_limited", reply=RATE_LIMITED_TEXT).model_dump(), status_code=429,
                headers={"Retry-After": "3600"},
            )
        async with unit_of_work() as session:
            user = await get_visitor(session, body.session_id, request)
            turn = await converse(session, settings, agent, user.id, body.message)
        if turn.status == "rate_limited":
            return JSONResponse(
                ChatOut(status="rate_limited", reply=RATE_LIMITED_TEXT).model_dump(), status_code=429,
                headers={"Retry-After": "3600"},
            )
        if turn.status == "failed":
            return ChatOut(status="failed", reply=FAILED_TEXT, handoff=True)
        result = turn.result
        return ChatOut(
            status="ok", reply=result.text, sources=sources_out(result), cards=[card_out(c) for c in result.cards],
            handoff=result.handoff,
        )

    @app.post("/api/lead")
    async def lead(body: LeadIn, request: Request):
        phone = body.phone.strip()
        if not PHONE_RE.match(phone):
            raise HTTPException(422, "Please enter a valid phone number (digits, may start with +).")
        async with unit_of_work() as session:
            user = await get_visitor(session, body.session_id, request)
            item_id, item_title = None, ""
            if body.action == "human":
                kind, note = "human", "asked for a person (website)"
            else:
                if not ACTION_RE.match(body.action):
                    raise HTTPException(422, "unknown action")
                prefix, item_id = body.action.split(":", 1)
                lead_action = vertical.lead_actions.get(prefix)
                item = await session.get(vertical.catalog_model, item_id)
                if lead_action is None or item is None:
                    raise HTTPException(404, "This item is no longer available.")
                kind, note, item_title = lead_action.kind, item.title, item.title
            created = await create_lead(session, user.id, kind, item_id, phone, body.name.strip(), note)
            transcript = await recent_messages(session, user.id)
        if notifier is not None:
            await notifier.notify(created, item_title, transcript)
        return LeadOut(ok=True, message=LEAD_THANKS.format(phone=phone))

    # ---- admin API (explicit key; off by default)

    def require_admin(key: str | None) -> None:
        if not settings.admin_api_key:
            raise HTTPException(404)
        if not key or not secrets.compare_digest(key, settings.admin_api_key):
            raise HTTPException(401, "invalid admin key")

    @app.get("/api/admin/stats")
    async def admin_stats(x_admin_key: str | None = Header(default=None)):
        require_admin(x_admin_key)
        async with session_maker() as session:
            stats = await collect_stats(session, settings)
        return {"business": vertical.company, "provider": agent.provider.name, **stats.__dict__,
                "prices_per_1m_tokens": {"input": settings.price_input_per_m, "output": settings.price_output_per_m}}

    @app.get("/api/admin/leads.csv")
    async def admin_leads(x_admin_key: str | None = Header(default=None)):
        require_admin(x_admin_key)
        async with session_maker() as session:
            rows = (await session.execute(select(Lead, User).join(User, User.id == Lead.user_id).order_by(Lead.id))).all()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["lead_id", "created_at_utc", "kind", "item_id", "phone", "name", "channel", "note", "status"])
        for lead_row, u in rows:
            writer.writerow([lead_row.id, lead_row.created_at.strftime("%Y-%m-%d %H:%M"), lead_row.kind,
                             lead_row.item_id or "", lead_row.phone, lead_row.name,
                             "web" if u.id < 0 else ("whatsapp" if u.id >= WA_USER_OFFSET else "telegram"), lead_row.note, lead_row.status])
        return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="leads.csv"'})

    # ---- widget script and demo site

    @app.get("/widget.js")
    async def widget_js():
        return FileResponse(HERE / "static" / "widget.js", media_type="text/javascript",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/")
    async def demo_site():
        page = HERE / "demo" / f"{vertical.key}.html"
        if not page.is_file():
            raise HTTPException(404)
        return FileResponse(page, media_type="text/html")

    return app
