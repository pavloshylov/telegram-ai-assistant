"""Thin client for the WhatsApp Cloud API messages endpoint (Meta Graph API).

Limits below are the ones in Meta's Cloud API reference (checked 2026-11-23, see docs/WHATSAPP.md):
text body 4096 characters, reply buttons 3 (title 20, body 1024), list messages 10 rows (button 20, row title 24,
row description 72, body 4096). Errors keep Meta's numeric code, because the code decides what to do next.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

TEXT_MAX = 4096
BUTTON_BODY_MAX = 1024
BUTTON_TITLE_MAX = 20
BUTTON_ID_MAX = 256
BUTTONS_MAX = 3
LIST_BODY_MAX = 4096
LIST_BUTTON_MAX = 20
LIST_ROW_TITLE_MAX = 24
LIST_ROW_DESC_MAX = 72
LIST_ROW_ID_MAX = 200
LIST_ROWS_MAX = 10

WINDOW_CLOSED = 131047  # "More than 24 hours have passed since the recipient last replied"
RATE_LIMITED = 130429  # Cloud API message throughput reached
TOKEN_EXPIRED = 190
RECIPIENT_NOT_ALLOWED = 131030  # not in Meta's docs page we read; seen on the test number for unlisted recipients


class GraphError(Exception):
    def __init__(self, status: int, code: int | None, details: str):
        super().__init__(f"Graph API error {code} (HTTP {status}): {details}")
        self.status, self.code, self.details = status, code, details

    @property
    def window_closed(self) -> bool:
        return self.code == WINDOW_CLOSED

    @property
    def retryable(self) -> bool:
        return self.code == RATE_LIMITED or self.status >= 500 or self.status == 0


@dataclass
class Row:
    id: str
    title: str
    description: str = ""


def clamp(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


class GraphClient:
    """Sends messages. Never logs the token or the full request URL."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None, retries: int = 2, backoff_s: float = 1.0):
        self.url = f"{settings.whatsapp_api_base}/{settings.whatsapp_graph_version}/{settings.whatsapp_phone_number_id}/messages"
        self.token = settings.whatsapp_token
        self.client = client or httpx.AsyncClient(timeout=15)
        self.retries, self.backoff_s = retries, backoff_s

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        last: GraphError | None = None
        for attempt in range(self.retries + 1):
            try:
                response = await self.client.post(
                    self.url, json={"messaging_product": "whatsapp", **body},
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            except httpx.HTTPError as exc:
                last = GraphError(0, None, f"network error: {type(exc).__name__}")
            else:
                if response.status_code < 300:
                    return response.json()
                try:
                    error = response.json().get("error", {})
                except ValueError:
                    error = {}
                details = (error.get("error_data") or {}).get("details") or error.get("message") or response.text[:200]
                last = GraphError(response.status_code, error.get("code"), str(details))
            if not last.retryable or attempt == self.retries:
                break
            log.warning("Graph API %s, retrying (%s/%s)", last.code or "network", attempt + 1, self.retries)
            await asyncio.sleep(self.backoff_s * (attempt + 1))
        assert last is not None
        if last.code == TOKEN_EXPIRED:
            log.error("WhatsApp access token expired or invalid: generate a new one in the App Dashboard")
        raise last

    @staticmethod
    def _wamid(result: dict[str, Any]) -> str:
        return (result.get("messages") or [{}])[0].get("id", "")

    async def send_text(self, to: str, body: str) -> str:
        if not 0 < len(body) <= TEXT_MAX:
            raise ValueError(f"text must be 1..{TEXT_MAX} characters")
        return self._wamid(await self._post({"to": to, "type": "text", "text": {"body": body, "preview_url": False}}))

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> str:
        """Reply buttons: (id, title) pairs, at most 3; titles are cut to 20 characters."""
        if not 1 <= len(buttons) <= BUTTONS_MAX:
            raise ValueError("reply buttons: 1..3")
        action = {"buttons": [{"type": "reply", "reply": {"id": i[:BUTTON_ID_MAX], "title": clamp(t, BUTTON_TITLE_MAX)}}
                              for i, t in buttons]}
        interactive = {"type": "button", "body": {"text": clamp(body, BUTTON_BODY_MAX)}, "action": action}
        return self._wamid(await self._post({"to": to, "type": "interactive", "interactive": interactive}))

    async def send_list(self, to: str, body: str, button: str, rows: list[Row]) -> str:
        if not 1 <= len(rows) <= LIST_ROWS_MAX:
            raise ValueError("list: 1..10 rows")
        row_json = []
        for row in rows:
            item = {"id": row.id[:LIST_ROW_ID_MAX], "title": clamp(row.title, LIST_ROW_TITLE_MAX)}
            if row.description:
                item["description"] = clamp(row.description, LIST_ROW_DESC_MAX)
            row_json.append(item)
        interactive = {
            "type": "list", "body": {"text": clamp(body, LIST_BODY_MAX)},
            "action": {"button": clamp(button, LIST_BUTTON_MAX), "sections": [{"title": "Options", "rows": row_json}]},
        }
        return self._wamid(await self._post({"to": to, "type": "interactive", "interactive": interactive}))

    async def mark_read_and_type(self, message_id: str) -> None:
        """Blue ticks + typing indicator (dismissed by Meta after 25 s or on our reply). Best effort."""
        try:
            await self._post({"status": "read", "message_id": message_id, "typing_indicator": {"type": "text"}})
        except GraphError as exc:
            log.info("could not mark message as read: %s", exc.code)
