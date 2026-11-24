"""WhatsApp Cloud API channel (FastAPI webhook) on the same engine as the Telegram bot and the website widget.

- GET  /webhook   Meta's verification handshake (hub.mode / hub.verify_token / hub.challenge)
- POST /webhook   inbound messages and delivery statuses; the X-Hub-Signature-256 header is checked on the raw body
- GET  /health    liveness + database check
- GET  /api/admin/stats   usage report (only with the X-Admin-Key header; off unless ADMIN_API_KEY is set)

Every rule that must not differ between channels (rate limit, history, guardrails, leads, usage report) stays in
`app.chat` / `app.agent`; this module translates WhatsApp <-> those services. What differs from Telegram:
the phone number is the user id (no "share your number" step), there are at most 3 reply buttons per message,
free-form replies are allowed only inside the 24-hour customer service window, and Meta repeats webhooks.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import Agent
from app.chat import collect_stats, converse, create_lead, recent_messages
from app.config import Settings
from app.db import Lead, User, WhatsAppEvent, WhatsAppOutbound
from app.web.api import ACTION_RE, WA_USER_OFFSET
from app.web.notify import LeadNotifier
from app.whatsapp import render
from app.whatsapp.graph import GraphClient, GraphError

log = logging.getLogger(__name__)

WINDOW = timedelta(hours=24)
STATUS_ORDER = {"accepted": 0, "sent": 1, "delivered": 2, "read": 3}
MAX_BODY = 4 * 1024 * 1024  # Meta documents payloads up to 3 MB


def user_id_for(wa_id: str) -> int:
    return WA_USER_OFFSET + int(wa_id)


def valid_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """`X-Hub-Signature-256: sha256=<hex>`; HMAC-SHA256 of the raw request body, keyed with the app secret."""
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip().lower())


class Replier:
    """Sends replies to one user for one inbound message; records what happened instead of failing silently."""

    def __init__(self, graph: GraphClient, session_maker: async_sessionmaker[AsyncSession], to: str, user_id: int,
                 window_open: bool):
        self.graph, self.session_maker, self.to, self.user_id = graph, session_maker, to, user_id
        self.window_closed = not window_open
        self.error: GraphError | None = None
        self.sent = 0

    @property
    def ok(self) -> bool:
        return not self.window_closed and self.error is None

    async def _send(self, call) -> bool:
        if self.window_closed or self.error is not None:
            return False
        try:
            wamid = await call
        except GraphError as exc:
            self.error = exc
            self.window_closed = exc.window_closed
            log.warning("could not send to user %s: Graph code %s (%s)", self.user_id, exc.code, exc.details)
            return False
        self.sent += 1
        if wamid:
            async with self.session_maker() as session:
                session.add(WhatsAppOutbound(id=wamid, user_id=self.user_id))
                await session.commit()
        return True

    async def text(self, body: str) -> bool:
        return await self._send(self.graph.send_text(self.to, body)) if self._can() else await self._skip()

    async def buttons(self, body: str, buttons: list[tuple[str, str]]) -> bool:
        return await self._send(self.graph.send_buttons(self.to, body, buttons)) if self._can() else await self._skip()

    async def menu(self, body: str, button: str, rows) -> bool:
        return await self._send(self.graph.send_list(self.to, body, button, rows)) if self._can() else await self._skip()

    def _can(self) -> bool:
        return self.ok

    async def _skip(self) -> bool:  # keeps un-awaited coroutines out of the picture when nothing may be sent
        if self.window_closed and self.error is None:
            log.warning("customer service window is closed for user %s: reply not sent (needs an approved template)",
                        self.user_id)
        return False

    @property
    def outcome(self) -> str:
        if self.window_closed:
            return "window_closed"
        return "send_failed" if self.error is not None else "replied"


class WhatsAppChannel:
    def __init__(self, settings: Settings, session_maker: async_sessionmaker[AsyncSession], agent: Agent,
                 graph: GraphClient, notifier: LeadNotifier | None = None):
        self.settings, self.session_maker, self.agent, self.graph, self.notifier = settings, session_maker, agent, graph, notifier
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # one conversation = one message at a time
        self.tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- webhook payload

    async def handle_payload(self, payload: dict[str, Any]) -> dict[str, int]:
        """Claims new message ids, applies statuses, and starts processing; returns counts (for logs and tests)."""
        counts = {"messages": 0, "duplicates": 0, "statuses": 0, "ignored": 0}
        if payload.get("object") != "whatsapp_business_account":
            counts["ignored"] += 1
            return counts
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                if change.get("field") != "messages":
                    counts["ignored"] += 1
                    continue
                if (value.get("metadata") or {}).get("phone_number_id") != self.settings.whatsapp_phone_number_id:
                    log.warning("webhook for another phone number id ignored")
                    counts["ignored"] += 1
                    continue
                names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts") or []}
                for message in value.get("messages") or []:
                    if await self._claim(message.get("id")):
                        counts["messages"] += 1
                        task = asyncio.create_task(self.process(message, names.get(message.get("from"), "")))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                    else:
                        counts["duplicates"] += 1
                for status in value.get("statuses") or []:
                    await self._apply_status(status)
                    counts["statuses"] += 1
                for error in value.get("errors") or []:
                    log.error("WhatsApp account-level error %s: %s", error.get("code"), error.get("title"))
        return counts

    async def drain(self) -> None:
        """Waits for running conversations (tests, graceful shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def _claim(self, message_id: str | None) -> bool:
        """False if this message id was already taken (Meta retries webhooks and says it can send duplicates)."""
        if not message_id:
            return False
        async with self.session_maker() as session:
            session.add(WhatsAppEvent(id=message_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        return True

    async def _apply_status(self, status: dict[str, Any]) -> None:
        wamid, new = status.get("id"), status.get("status")
        if not wamid or not new:
            return
        errors = status.get("errors") or []
        code = errors[0].get("code") if errors else None
        detail = ((errors[0].get("error_data") or {}).get("details") or errors[0].get("title") or "") if errors else ""
        async with self.session_maker() as session:
            row = await session.get(WhatsAppOutbound, wamid)
            if row is None:
                return
            if new == "failed":
                log.warning("WhatsApp delivery failed for user %s: code %s (%s)", row.user_id, code, detail)
            elif STATUS_ORDER.get(new, 0) <= STATUS_ORDER.get(row.status, 0):
                return  # statuses can arrive out of order: never go backwards
            row.status, row.error_code, row.detail = new, code, str(detail)[:500]
            row.updated_at = datetime.now(timezone.utc)
            await session.commit()

    # ---------------------------------------------------------------- one inbound message

    async def process(self, message: dict[str, Any], profile_name: str) -> None:
        wa_id = message.get("from")
        message_id = message["id"]
        if not wa_id or not str(wa_id).isdigit():  # users with a username may arrive without a phone number
            log.warning("message %s has no phone number (username user): not supported", message_id)
            await self._finish(message_id, "no_phone")
            return
        uid = user_id_for(wa_id)
        async with self.locks[uid]:
            sent_at = datetime.fromtimestamp(int(message.get("timestamp") or time.time()), timezone.utc)
            window_open = datetime.now(timezone.utc) - sent_at < WINDOW
            replier = Replier(self.graph, self.session_maker, wa_id, uid, window_open)
            try:
                status = await self._handle(message, wa_id, uid, profile_name, replier)
            except Exception:  # noqa: BLE001 — one broken message must not break the webhook
                log.exception("processing message %s failed", message_id)
                await replier.text(render.ERROR)
                status = "error"
            await self._finish(message_id, status if replier.ok else replier.outcome)

    async def _finish(self, message_id: str, status: str) -> None:
        async with self.session_maker() as session:
            row = await session.get(WhatsAppEvent, message_id)
            if row is not None:
                row.status = status
                await session.commit()

    async def _handle(self, message: dict[str, Any], wa_id: str, uid: int, name: str, replier: Replier) -> str:
        kind = message.get("type")
        action_id, question = None, ""
        if kind == "text":
            question = ((message.get("text") or {}).get("body") or "").strip()
        elif kind == "interactive":
            interactive = message.get("interactive") or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            action_id, question = reply.get("id"), (reply.get("title") or "").strip()
        if (kind not in {"text", "interactive"}) or not (question or action_id):
            await replier.text(render.TEXT_ONLY)  # voice notes, images, locations, reactions, stickers ...
            return "unsupported"

        async with self.session_maker() as session:
            user = await session.get(User, uid)
            if user is None:
                user = User(id=uid, username=None, first_name=name or "WhatsApp user", language=None)
                session.add(user)
            elif name:
                user.first_name = name
            await session.commit()
        if replier.ok:
            await self.graph.mark_read_and_type(message["id"])

        vertical = self.agent.vertical
        if action_id == render.HUMAN_ID:
            return await self._lead(uid, wa_id, name, replier, "human", None, "asked for a person (WhatsApp)")
        if action_id and ACTION_RE.match(action_id):
            prefix, item_id = action_id.split(":", 1)
            lead_action = vertical.lead_actions.get(prefix)
            async with self.session_maker() as session:
                item = await session.get(vertical.catalog_model, item_id) if lead_action else None
            if lead_action is None or item is None:
                await replier.text(render.ITEM_GONE)
                return "ok"
            return await self._lead(uid, wa_id, name, replier, lead_action.kind, item_id, item.title, item.title)
        if action_id and action_id.startswith(render.EXAMPLE_PREFIX):
            index = action_id.removeprefix(render.EXAMPLE_PREFIX)
            if index.isdigit() and int(index) < len(vertical.examples):
                question = vertical.examples[int(index)]
        if kind == "text" and question.lower().strip("!. ") in render.MENU_WORDS:
            body, button, rows = render.menu_message(vertical)
            await replier.menu(body, button, rows)
            return "ok"

        async with self.session_maker() as session:
            turn = await converse(session, self.settings, self.agent, uid, question)
            await session.commit()
        if turn.status == "rate_limited":
            await replier.text(render.RATE_LIMITED)
            return "rate_limited"
        if turn.status == "failed":
            return await self._lead(uid, wa_id, name, replier, "human", None, "assistant failed to answer (WhatsApp)",
                                    intro=render.FAILED_INTRO)
        result = turn.result
        for chunk in render.reply_chunks(result, self.settings.whatsapp_show_sources):
            await replier.text(chunk)
        for card in result.cards:
            body, buttons = render.card_message(card, self.agent.vertical.human_button)
            await replier.buttons(body, buttons)
        if result.handoff:
            return await self._lead(uid, wa_id, name, replier, "human", None, "assistant hand-off (WhatsApp)")
        return "ok"

    async def _lead(self, uid: int, wa_id: str, name: str, replier: Replier, kind: str, item_id: str | None,
                    note: str, item_title: str = "", intro: str = "") -> str:
        """The phone number is the WhatsApp number: no second question. Repeated hand-offs inside an hour reuse the lead."""
        phone = f"+{wa_id}"
        async with self.session_maker() as session:
            hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
            existing = None
            if kind == "human":
                existing = await session.scalar(select(Lead).where(
                    Lead.user_id == uid, Lead.kind == "human", Lead.created_at >= hour_ago))
            lead = existing or await create_lead(session, uid, kind, item_id, phone, name, note)
            transcript = await recent_messages(session, uid)
            await session.commit()
        if existing is None and self.notifier is not None:
            await self.notifier.notify(lead, item_title, transcript)
        await replier.text((intro + " " if intro else "") + render.LEAD_THANKS.format(phone=wa_id))
        return "ok"


def create_app(settings: Settings, session_maker: async_sessionmaker[AsyncSession], agent: Agent,
               graph: GraphClient | None = None, notifier: LeadNotifier | None = None) -> FastAPI:
    if not settings.whatsapp_app_secret:
        raise SystemExit("WHATSAPP_APP_SECRET is required: every webhook POST must be signature-checked")
    if not settings.whatsapp_verify_token:
        raise SystemExit("WHATSAPP_VERIFY_TOKEN is required for Meta's webhook verification")
    agent.channel_note = render.CHANNEL_NOTE
    channel = WhatsAppChannel(settings, session_maker, agent, graph or GraphClient(settings), notifier)
    app = FastAPI(title=f"{agent.vertical.company} WhatsApp assistant", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.channel = channel

    @app.get("/webhook")
    async def verify(mode: str | None = Query(None, alias="hub.mode"), token: str | None = Query(None, alias="hub.verify_token"),
                     challenge: str | None = Query(None, alias="hub.challenge")):
        if mode == "subscribe" and token and challenge is not None and secrets.compare_digest(
                token.encode(), settings.whatsapp_verify_token.encode()):
            return PlainTextResponse(challenge)
        raise HTTPException(403, "verification failed")

    @app.post("/webhook")
    async def receive(request: Request, x_hub_signature_256: str | None = Header(default=None)):
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413)
        if not valid_signature(settings.whatsapp_app_secret, body, x_hub_signature_256):
            log.warning("webhook POST with an invalid signature rejected")
            raise HTTPException(403, "invalid signature")
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, "invalid JSON") from None
        counts = await channel.handle_payload(payload)  # answers 200 right away; the model runs in the background
        return JSONResponse({"status": "ok", **counts})

    @app.get("/health")
    async def health():
        async with session_maker() as session:
            await session.execute(text("select 1"))
        return {"status": "ok", "business": agent.vertical.company, "provider": agent.provider.name,
                "channel": "whatsapp"}

    @app.get("/api/admin/stats")
    async def admin_stats(x_admin_key: str | None = Header(default=None)):
        if not settings.admin_api_key:
            raise HTTPException(404)
        if not x_admin_key or not secrets.compare_digest(x_admin_key, settings.admin_api_key):
            raise HTTPException(401, "invalid admin key")
        async with session_maker() as session:
            stats = await collect_stats(session, settings)
        return {"business": agent.vertical.company, "provider": agent.provider.name, **stats.__dict__}

    return app
