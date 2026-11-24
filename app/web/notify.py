"""Lead notifications for the website widget: a Telegram message to the admins (optional).

The widget has no Telegram chat of its own, so a new lead is pushed to the admins' Telegram through the Bot API
(plain HTTPS call, no aiogram). Without BOT_TOKEN/ADMIN_IDS nothing is sent; leads are still saved and can be read
through the admin API.
"""
from __future__ import annotations

import logging
from html import escape
from typing import Protocol

import httpx

from app.bot import texts
from app.config import Settings
from app.db import Lead, Message

log = logging.getLogger(__name__)


class LeadNotifier(Protocol):
    async def notify(self, lead: Lead, item_title: str, transcript: list[Message]) -> None: ...


class TelegramNotifier:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, channel: str = "website"):
        self.channel = channel
        self.token = settings.bot_token
        self.admin_ids = sorted(settings.admin_ids)
        self.client = client or httpx.AsyncClient(timeout=10)

    async def notify(self, lead: Lead, item_title: str, transcript: list[Message]) -> None:
        lines = "\n".join(f"{'👤' if m.role == 'user' else '🤖'} {escape(m.text[:200])}" for m in transcript) or "—"
        text = texts.LEAD_NOTIFY.format(
            kind=f"{lead.kind} ({self.channel})", id=lead.id, user=escape(lead.name or f"{self.channel} visitor"),
            phone=escape(lead.phone),
            item=f"\nItem: {escape(lead.item_id)} · {escape(item_title)}" if lead.item_id else "",
            note=f"\nNote: {escape(lead.note)}" if lead.note and not lead.item_id else "",
            transcript=lines,
        )
        for admin_id in self.admin_ids:
            try:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={"chat_id": admin_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:  # a lead is saved even if the notification fails; never log the URL (token)
                log.warning("could not notify admin %s about lead %s: %s", admin_id, lead.id, type(exc).__name__)


def make_notifier(settings: Settings, channel: str = "website") -> LeadNotifier | None:
    if ":" in settings.bot_token and settings.admin_ids and "replace-me" not in settings.bot_token:
        return TelegramNotifier(settings, channel=channel)
    return None
