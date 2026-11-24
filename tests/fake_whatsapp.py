"""In-process fake of the WhatsApp Cloud API for end-to-end tests (counterpart of fake_telegram.py).

`FakeGraph` replaces Meta's messages endpoint behind an httpx MockTransport. It enforces the documented limits
(text 4096, 3 reply buttons, button title 20, list 10 rows ...) and answers like Meta does when they are broken,
so a test fails when the channel would be rejected by the real API. It can also simulate a closed 24-hour window
and Graph errors. `payloads` builds webhook bodies in Meta's format and signs them.
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
PHONE_NUMBER_ID = "1000000000001"
TOKEN = "test-access-token"


@dataclass
class Sent:
    to: str
    kind: str  # text | buttons | list | read
    body: str = ""
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (id, title) of reply buttons or list rows
    raw: dict[str, Any] = field(default_factory=dict)


def graph_error(status: int, code: int, details: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {
        "message": f"(#{code}) {details}", "type": "OAuthException", "code": code,
        "error_data": {"messaging_product": "whatsapp", "details": details}, "fbtrace_id": "TRACE"}})


class FakeGraph:
    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.requests: list[httpx.Request] = []
        self.closed_window: set[str] = set()  # recipients that have not written for 24 h -> error 131047
        self.fail_next: list[tuple[int, int, str]] = []  # (HTTP status, Graph code, details), consumed one per request
        self._ids = itertools.count(1)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), base_url="http://graph.test")

    # -- helpers for assertions
    def texts(self, to: str | None = None) -> list[str]:
        return [s.body for s in self.sent if s.kind == "text" and (to is None or s.to == to)]

    def last(self) -> Sent:
        return self.sent[-1]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return graph_error(401, 190, "Your access token has expired.")
        if not request.url.path.endswith(f"/{PHONE_NUMBER_ID}/messages"):
            return httpx.Response(404, json={"error": {"message": "unknown path", "code": 100}})
        body = json.loads(request.content)
        if self.fail_next:
            status, code, details = self.fail_next.pop(0)
            return graph_error(status, code, details)
        if body.get("messaging_product") != "whatsapp":
            return graph_error(400, 100, "messaging_product is required")
        if body.get("status") == "read":  # mark as read / typing indicator
            self.sent.append(Sent("", "read", raw=body))
            return httpx.Response(200, json={"success": True})
        to = body.get("to", "")
        if to in self.closed_window:
            return graph_error(400, 131047, "Re-engagement message: more than 24 hours have passed")
        problem = self.validate(body)
        if problem:
            return graph_error(400, 100, problem)
        wamid = f"wamid.OUT{next(self._ids):04d}"
        self.sent.append(self.record(to, body))
        return httpx.Response(200, json={"messaging_product": "whatsapp", "contacts": [{"input": to, "wa_id": to}],
                                         "messages": [{"id": wamid}]})

    @staticmethod
    def record(to: str, body: dict[str, Any]) -> Sent:
        if body["type"] == "text":
            return Sent(to, "text", body["text"]["body"], raw=body)
        interactive = body["interactive"]
        if interactive["type"] == "button":
            buttons = [(b["reply"]["id"], b["reply"]["title"]) for b in interactive["action"]["buttons"]]
            return Sent(to, "buttons", interactive["body"]["text"], buttons, raw=body)
        rows = [(r["id"], r["title"]) for s in interactive["action"]["sections"] for r in s["rows"]]
        return Sent(to, "list", interactive["body"]["text"], rows, raw=body)

    @staticmethod
    def validate(body: dict[str, Any]) -> str | None:
        if not body.get("to"):
            return "to is required"
        if body.get("type") == "text":
            text = (body.get("text") or {}).get("body", "")
            return None if 0 < len(text) <= 4096 else "text body must be 1..4096 characters"
        if body.get("type") != "interactive":
            return "unsupported type"
        it = body["interactive"]
        if it["type"] == "button":
            buttons = it["action"]["buttons"]
            titles = [b["reply"]["title"] for b in buttons]
            if not 1 <= len(buttons) <= 3:
                return "reply buttons: 1 to 3"
            if any(not t or len(t) > 20 for t in titles) or len(set(titles)) != len(titles):
                return "button title: unique, max 20 characters"
            if any(len(b["reply"]["id"]) > 256 for b in buttons):
                return "button id: max 256 characters"
            if not 0 < len(it["body"]["text"]) <= 1024:
                return "button message body: 1..1024 characters"
            return None
        if it["type"] == "list":
            rows = [r for s in it["action"]["sections"] for r in s["rows"]]
            if not 1 <= len(rows) <= 10 or len(it["action"]["sections"]) > 10:
                return "list: 1 to 10 rows in total"
            if len(it["action"]["button"]) > 20 or not it["action"]["button"]:
                return "list button: max 20 characters"
            if any(len(r["title"]) > 24 or len(r.get("description", "")) > 72 or len(r["id"]) > 200 for r in rows):
                return "list row: title max 24, description max 72, id max 200"
            if len(it["action"]["sections"]) > 1 and any(not s.get("title") for s in it["action"]["sections"]):
                return "section title required with more than one section"
            return None
        return "unsupported interactive type"


# ---------------------------------------------------------------- inbound webhooks

_counter = itertools.count(1)


def envelope(value: dict[str, Any]) -> dict[str, Any]:
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "15550000000", "phone_number_id": PHONE_NUMBER_ID}, **value}
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA1", "changes": [{"field": "messages", "value": value}]}]}


def inbound(wa_id: str, message: dict[str, Any], name: str = "Test Customer", ts: int | None = None, mid: str | None = None):
    message = {"from": wa_id, "id": mid or f"wamid.IN{next(_counter):04d}", "timestamp": str(ts or int(time.time())), **message}
    return envelope({"contacts": [{"profile": {"name": name}, "wa_id": wa_id}], "messages": [message]})


def text_in(wa_id: str, body: str, **kw):
    return inbound(wa_id, {"type": "text", "text": {"body": body}}, **kw)


def button_in(wa_id: str, button_id: str, title: str = "Button", **kw):
    return inbound(wa_id, {"type": "interactive", "interactive": {"type": "button_reply",
                                                                   "button_reply": {"id": button_id, "title": title}}}, **kw)


def list_in(wa_id: str, row_id: str, title: str = "Row", **kw):
    return inbound(wa_id, {"type": "interactive", "interactive": {"type": "list_reply",
                                                                   "list_reply": {"id": row_id, "title": title}}}, **kw)


def status_in(wamid: str, status: str, recipient: str, code: int | None = None, details: str = ""):
    entry: dict[str, Any] = {"id": wamid, "status": status, "timestamp": str(int(time.time())), "recipient_id": recipient}
    if code:
        entry["errors"] = [{"code": code, "title": details, "error_data": {"details": details}}]
    return envelope({"statuses": [entry]})


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
