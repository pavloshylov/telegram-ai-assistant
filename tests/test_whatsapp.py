"""WhatsApp channel end to end: real webhook app, real engine and database, fake Graph API (no network)."""
from __future__ import annotations

import json
import time

import httpx
import pytest_asyncio
from sqlalchemy import func, select

from app.db import Lead, Message, User, WhatsAppEvent, WhatsAppOutbound
from app.llm.base import LLMError, LLMReply, ToolCall, Usage
from app.whatsapp.api import WA_USER_OFFSET, create_app, user_id_for, valid_signature
from app.whatsapp.graph import GraphClient
from app.agent import AgentResult
from app.whatsapp import render
from app.whatsapp.render import split_text, to_whatsapp
from tests.fake_whatsapp import (
    APP_SECRET, PHONE_NUMBER_ID, TOKEN, VERIFY_TOKEN, FakeGraph, button_in, envelope, list_in, sign, status_in, text_in,
)
from tests.test_agent import FakeModel

USER = "351910000001"  # fictional number


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead, item_title, transcript):
        self.sent.append((lead, item_title, [m.text for m in transcript]))


class Wa:
    """Drives the webhook the way Meta does: signed POSTs, then waits until the background answer is finished."""

    def __init__(self, client, graph, model, sm, notifier, app):
        self.client, self.graph, self.model, self.sm, self.notifier, self.app = client, graph, model, sm, notifier, app

    async def post(self, payload, secret=APP_SECRET, signature=None):
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json", "X-Hub-Signature-256": signature or sign(body, secret)}
        res = await self.client.post("/webhook", content=body, headers=headers)
        await self.app.state.channel.drain()
        return res


@pytest_asyncio.fixture
async def wa(stack):
    clients = []

    async def build(vertical, *replies, **overrides):
        model = FakeModel(*replies)
        settings, sm, agent = await stack(
            vertical, model, whatsapp_token=TOKEN, whatsapp_phone_number_id=PHONE_NUMBER_ID,
            whatsapp_verify_token=VERIFY_TOKEN, whatsapp_app_secret=APP_SECRET, **overrides)
        graph, notifier = FakeGraph(), RecordingNotifier()
        graph_client = GraphClient(settings, graph.client(), retries=2, backoff_s=0)
        app = create_app(settings, sm, agent, graph_client, notifier)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return Wa(client, graph, model, sm, notifier, app)

    yield build
    for client in clients:
        await client.aclose()


def search_then_answer():
    return (
        LLMReply(tool_calls=[ToolCall("search_listings", {"deal": "rent", "district": "Belém"}, id="c1")], usage=Usage(900, 20)),
        LLMReply(text="Here is a **flat by the river**. Tenants pay no agency fee [1].", usage=Usage(1200, 40)),
    )


# ---------------------------------------------------------------- verification and signature

async def test_verify_handshake_returns_the_challenge(wa):
    env = await wa("realty")
    ok = await env.client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"})
    assert ok.status_code == 200 and ok.text == "1158201444"


async def test_verify_handshake_rejects_wrong_token_or_mode(wa):
    env = await wa("realty")
    bad = await env.client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"})
    assert bad.status_code == 403
    no_mode = await env.client.get("/webhook", params={"hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1"})
    assert no_mode.status_code == 403 and (await env.client.get("/webhook")).status_code == 403


def test_signature_check():
    body = b'{"a": 1}'
    assert valid_signature(APP_SECRET, body, sign(body))
    assert not valid_signature(APP_SECRET, body, sign(body, "other-secret"))
    assert not valid_signature(APP_SECRET, body, sign(b"tampered"))
    assert not valid_signature(APP_SECRET, body, None) and not valid_signature(APP_SECRET, body, "sha256=")
    assert not valid_signature(APP_SECRET, body, sign(body).removeprefix("sha256="))  # prefix is mandatory
    assert not valid_signature("", body, sign(body, ""))  # no secret configured: nothing is accepted


async def test_invalid_or_missing_signature_is_rejected_and_nothing_runs(wa):
    env = await wa("realty", LLMReply(text="should never be asked"))
    payload = text_in(USER, "Hello there")
    wrong = await env.post(payload, secret="not-the-secret")
    body = json.dumps(payload).encode()
    missing = await env.client.post("/webhook", content=body)
    assert wrong.status_code == 403 and missing.status_code == 403
    assert env.graph.sent == [] and len(env.model.replies) == 1
    async with env.sm() as session:
        assert await session.scalar(select(func.count()).select_from(WhatsAppEvent)) == 0


async def test_app_refuses_to_start_without_secret_or_verify_token(stack):
    settings, sm, agent = await stack("shop", FakeModel(), whatsapp_verify_token="x")
    try:
        create_app(settings, sm, agent)
    except SystemExit as exc:
        assert "WHATSAPP_APP_SECRET" in str(exc)
    else:
        raise AssertionError("expected SystemExit")


# ---------------------------------------------------------------- conversation

async def test_question_gets_text_with_sources_and_a_card_with_buttons(wa):
    env = await wa("realty", *search_then_answer())
    res = await env.post(text_in(USER, "Flat in Belém, any agency fee?"))
    assert res.status_code == 200 and res.json()["messages"] == 1
    read, answer, card = env.graph.sent
    assert read.kind == "read" and read.raw["typing_indicator"] == {"type": "text"}
    assert answer.kind == "text" and answer.to == USER
    assert "*flat by the river*" in answer.body and "**" not in answer.body  # WhatsApp bold, not Markdown
    assert "[1]" not in answer.body and "Sources" not in answer.body  # chat reads like a person
    assert answer.body.strip().endswith("agency fee.")
    assert card.kind == "buttons" and card.body.startswith("*") and "NH-107" not in card.body
    assert card.buttons == [("book:NH-107", "📅 Book a viewing"), ("human", "👤 Talk to an agent")]
    async with env.sm() as session:
        user = await session.get(User, user_id_for(USER))
        assert user.id >= WA_USER_OFFSET and user.first_name == "Test Customer"
        rows = list(await session.scalars(select(Message).where(Message.user_id == user.id).order_by(Message.id)))
        assert [r.role for r in rows] == ["user", "assistant"] and rows[1].input_tokens == 2100
        assert await session.scalar(select(func.count()).select_from(WhatsAppOutbound)) == 2  # text + card are tracked


async def test_duplicate_delivery_is_answered_once(wa):
    env = await wa("shop", LLMReply(text="Answer one."), LLMReply(text="never used"))
    payload = text_in(USER, "Do you ship to Spain?", mid="wamid.SAME")
    first, second = await env.post(payload), await env.post(payload)
    assert first.json()["messages"] == 1 and second.json()["duplicates"] == 1 and second.json()["messages"] == 0
    assert env.graph.texts() == ["Answer one."] and len(env.model.calls) == 1


async def test_two_messages_in_one_batch_are_both_answered_in_order(wa):
    env = await wa("shop", LLMReply(text="First."), LLMReply(text="Second."))
    first, second = text_in(USER, "one", mid="wamid.A1"), text_in(USER, "two", mid="wamid.A2")
    second["entry"][0]["changes"][0]["value"]["messages"].insert(0, first["entry"][0]["changes"][0]["value"]["messages"][0])
    res = await env.post(second)
    assert res.json()["messages"] == 2 and env.graph.texts() == ["First.", "Second."]


async def test_unsupported_types_get_a_polite_text_and_no_model_call(wa):
    env = await wa("shop", LLMReply(text="never used"))
    for message in ({"type": "audio", "audio": {"id": "A1"}}, {"type": "image", "image": {"id": "I1"}},
                    {"type": "unsupported", "errors": [{"code": 131051}]}, {"type": "location", "location": {}}):
        from tests.fake_whatsapp import inbound
        await env.post(inbound(USER, message))
    assert env.graph.texts() == [render.TEXT_ONLY] * 4
    assert env.model.calls == []


async def test_other_phone_number_id_is_ignored(wa):
    env = await wa("shop", LLMReply(text="never used"))
    payload = text_in(USER, "hi there")
    payload["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "999"
    res = await env.post(payload)
    assert res.json()["messages"] == 0 and env.graph.sent == []


async def test_menu_words_send_a_list_with_examples_and_a_person_row(wa):
    env = await wa("realty")
    await env.post(text_in(USER, "Hi!"))
    menu = env.graph.sent[-1]
    assert menu.kind == "list" and "Northwind Homes" in menu.body and "<b>" not in menu.body
    assert [r[0] for r in menu.buttons] == ["ex:0", "ex:1", "ex:2", "human"] and all(len(r[1]) <= 24 for r in menu.buttons)
    assert env.model.calls == []


async def test_list_row_asks_the_example_question(wa):
    env = await wa("realty", LLMReply(text="Tenants usually pay no agency fee."))
    await env.post(list_in(USER, "ex:1", "Do tenants pay an agency fee?"))
    assert env.model.calls[0]["messages"][-1].text == "Do tenants pay an agency fee?"
    assert env.graph.texts() == ["Tenants usually pay no agency fee."]


# ---------------------------------------------------------------- leads and hand-off

async def test_book_button_creates_a_lead_with_the_whatsapp_number_and_notifies_staff(wa):
    env = await wa("realty", *search_then_answer())
    await env.post(text_in(USER, "Flat in Belém?"))
    env.graph.sent.clear()
    await env.post(button_in(USER, "book:NH-107", "📅 Book a viewing"))
    assert env.graph.texts()[-1].startswith("✅ Got it, thank you!") and f"+{USER}" in env.graph.texts()[-1]
    async with env.sm() as session:
        lead = await session.scalar(select(Lead))
        assert (lead.kind, lead.item_id, lead.phone, lead.name) == ("viewing", "NH-107", f"+{USER}", "Test Customer")
    (notified, title, transcript), = env.notifier.sent
    assert notified.item_id == "NH-107" and title and any("Belém" in t for t in transcript)
    assert not any("phone number" in t.lower() and "?" in t for t in env.graph.texts())  # never asks for the number


async def test_order_button_in_the_shop(wa):
    env = await wa("shop")
    await env.post(button_in(USER, "order:NA-02", "🛒 Order"))
    async with env.sm() as session:
        lead = await session.scalar(select(Lead))
        assert lead.kind == "order" and lead.item_id == "NA-02" and lead.phone == f"+{USER}"


async def test_unknown_item_button_says_it_is_gone(wa):
    env = await wa("shop")
    await env.post(button_in(USER, "order:NA-99", "🛒 Order"))
    assert env.graph.texts() == [render.ITEM_GONE]
    async with env.sm() as session:
        assert await session.scalar(select(func.count()).select_from(Lead)) == 0


async def test_talk_to_a_person_button_is_a_human_lead_once_per_hour(wa):
    env = await wa("shop")
    await env.post(button_in(USER, "human", "👤 Talk to a person"))
    await env.post(button_in(USER, "human", "👤 Talk to a person"))
    async with env.sm() as session:
        leads = list(await session.scalars(select(Lead)))
    assert len(leads) == 1 and leads[0].kind == "human" and len(env.notifier.sent) == 1
    assert len([t for t in env.graph.texts() if t.startswith("✅ Got it, thank you!")]) == 2  # the user is always answered


async def test_model_handoff_creates_the_lead_without_asking_for_a_number(wa):
    env = await wa("shop", LLMReply(tool_calls=[ToolCall("handoff_to_human", {"reason": "student discount"}, id="h1")]),
                   LLMReply(text="I am not sure about student discounts; let me get a colleague."))
    await env.post(text_in(USER, "Student discount?"))
    assert env.graph.texts()[0].startswith("I am not sure") and env.graph.texts()[1].startswith("✅ Got it, thank you!")
    async with env.sm() as session:
        lead = await session.scalar(select(Lead))
        assert lead.kind == "human" and lead.note == "assistant hand-off (WhatsApp)"


async def test_model_failure_hands_off_instead_of_going_silent(wa):
    env = await wa("realty", LLMError("Gemini API error 503"))
    await env.post(text_in(USER, "Anything?"))
    assert env.graph.texts()[0].startswith("I can't answer this one right now") and "+" + USER in env.graph.texts()[0]
    async with env.sm() as session:
        assert (await session.scalar(select(Lead))).kind == "human"


async def test_per_user_rate_limit_stops_before_the_model(wa):
    env = await wa("shop", LLMReply(text="one"), LLMReply(text="two"), rate_limit_per_hour=2)
    for i in range(3):
        await env.post(text_in(USER, f"question {i}"))
    assert env.graph.texts()[:2] == ["one", "two"] and env.graph.texts()[2].startswith("You've sent quite a few messages")
    assert len(env.model.calls) == 2


# ---------------------------------------------------------------- WhatsApp limits

def test_split_text_respects_the_limit_and_keeps_all_words():
    text = ("Paragraph about flats. " * 60 + "\n\n") * 6
    chunks = split_text(text)
    assert len(chunks) > 1 and all(0 < len(c) <= 4096 for c in chunks)
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())
    assert split_text("x" * 9000) == ["x" * 4096, "x" * 4096, "x" * 808]  # no spaces: hard cut


def test_markup_conversion():
    assert to_whatsapp("<b>Bold</b> and **model bold** &amp; <i>it</i><br>") == "*Bold* and *model bold* & _it_"


async def test_long_answer_is_sent_in_several_messages(wa):
    long_text = "\n\n".join(f"Section {i}: " + "lorem ipsum " * 80 for i in range(12))
    env = await wa("shop", LLMReply(text=long_text))
    await env.post(text_in(USER, "Tell me everything"))
    sent = env.graph.texts()
    assert len(sent) >= 2 and all(len(t) <= 4096 for t in sent)
    assert "Section 0" in sent[0] and "Section 11" in sent[-1]


async def test_card_buttons_never_exceed_whatsapp_limits(wa):
    env = await wa("shop", LLMReply(tool_calls=[ToolCall("search_products", {"category": "over-ear"}, id="c1")]),
                   LLMReply(text="Here are some options."))
    await env.post(text_in(USER, "Over-ear headphones?"))
    cards = [s for s in env.graph.sent if s.kind == "buttons"]
    assert cards and all(len(s.buttons) <= 3 and all(len(t) <= 20 for _, t in s.buttons) and len(s.body) <= 1024 for s in cards)
    assert all(s.buttons[-1] == ("human", "👤 Talk to a person") for s in cards)  # the shop vertical's own wording


# ---------------------------------------------------------------- 24-hour window, Graph errors, statuses

async def test_reply_outside_the_24h_window_is_not_sent_and_is_recorded(wa, caplog):
    env = await wa("shop", LLMReply(text="Late answer."))
    old = int(time.time()) - 25 * 3600
    await env.post(text_in(USER, "Is this still open?", ts=old, mid="wamid.OLD"))
    assert env.graph.sent == []  # nothing sent: free text is not allowed here
    assert "window is closed" in caplog.text
    async with env.sm() as session:
        assert (await session.get(WhatsAppEvent, "wamid.OLD")).status == "window_closed"


async def test_late_button_tap_still_saves_the_lead_but_sends_nothing(wa):
    env = await wa("shop")
    await env.post(button_in(USER, "order:NA-02", "🛒 Order", ts=int(time.time()) - 30 * 3600))
    assert env.graph.sent == []
    async with env.sm() as session:
        assert (await session.scalar(select(Lead))).item_id == "NA-02"


async def test_graph_window_error_131047_is_recorded_not_raised(wa):
    env = await wa("shop", LLMReply(text="Answer."))
    env.graph.closed_window.add(USER)
    res = await env.post(text_in(USER, "Hello shop", mid="wamid.W1"))
    assert res.status_code == 200 and [x for x in env.graph.sent if x.kind != "read"] == []
    async with env.sm() as session:
        assert (await session.get(WhatsAppEvent, "wamid.W1")).status == "window_closed"


async def test_graph_api_error_is_recorded_and_the_webhook_still_answers_200(wa):
    env = await wa("shop", LLMReply(text="Answer."))
    env.graph.fail_next = [(400, 131030, "Recipient phone number not in allowed list")] * 2  # read marker + text
    res = await env.post(text_in(USER, "Hello shop", mid="wamid.F1"))
    assert res.status_code == 200
    async with env.sm() as session:
        assert (await session.get(WhatsAppEvent, "wamid.F1")).status == "send_failed"


async def test_rate_limited_graph_call_is_retried(wa):
    env = await wa("shop", LLMReply(text="Answer after retry."))
    env.graph.fail_next = [(429, 130429, "Cloud API message throughput has been reached.")]  # hits the read marker, retried
    await env.post(text_in(USER, "Hello shop"))
    assert env.graph.texts() == ["Answer after retry."] and len(env.graph.requests) == 3


async def test_expired_token_is_reported_without_leaking_it(wa, caplog):
    env = await wa("shop", LLMReply(text="Answer."))
    env.graph.fail_next = [(401, 190, "Your access token has expired.")] * 2
    await env.post(text_in(USER, "Hello shop", mid="wamid.T1"))
    assert "token expired" in caplog.text.lower() and TOKEN not in caplog.text
    async with env.sm() as session:
        assert (await session.get(WhatsAppEvent, "wamid.T1")).status == "send_failed"


async def test_delivery_statuses_update_the_outbound_log_and_never_go_backwards(wa):
    env = await wa("shop", LLMReply(text="Answer."))
    await env.post(text_in(USER, "Hello shop"))
    async with env.sm() as session:
        wamid = await session.scalar(select(WhatsAppOutbound.id).where(WhatsAppOutbound.status == "accepted"))
    await env.post(status_in(wamid, "read", USER))
    await env.post(status_in(wamid, "delivered", USER))  # arrives late
    async with env.sm() as session:
        assert (await session.get(WhatsAppOutbound, wamid)).status == "read"
    await env.post(status_in(wamid, "failed", USER, 131047, "More than 24 hours have passed"))
    async with env.sm() as session:
        row = await session.get(WhatsAppOutbound, wamid)
        assert (row.status, row.error_code) == ("failed", 131047)


async def test_unknown_status_and_foreign_objects_are_ignored(wa):
    env = await wa("shop")
    assert (await env.post(status_in("wamid.UNKNOWN", "delivered", USER))).status_code == 200
    assert (await env.post({"object": "page", "entry": []})).json()["ignored"] == 1
    other_field = envelope({})
    other_field["entry"][0]["changes"][0]["field"] = "account_update"
    assert (await env.post(other_field)).json()["ignored"] == 1


async def test_health_and_admin_stats(wa):
    env = await wa("shop", LLMReply(text="Answer.", usage=Usage(100, 10)), admin_api_key="k")
    assert (await env.client.get("/health")).json()["channel"] == "whatsapp"
    await env.post(text_in(USER, "Hello shop"))
    assert (await env.client.get("/api/admin/stats")).status_code == 401
    stats = (await env.client.get("/api/admin/stats", headers={"X-Admin-Key": "k"})).json()
    assert stats["questions"] == 1 and stats["tokens_in"] == 100


async def test_model_is_told_that_the_phone_number_is_already_known(wa):
    env = await wa("realty", LLMReply(text="Tenants pay no agency fee."))
    await env.post(text_in(USER, "Agency fee?"))
    system = env.model.calls[0]["system"]
    assert "Channel facts: WhatsApp" in system and "never ask for it" in system


def test_citation_markers_are_removed_unless_sources_are_enabled():
    result = AgentResult(text="Tenants pay no agency fee [1]. The deposit is two months [1, 2] , paid up front.")
    assert render.reply_chunks(result) == ["Tenants pay no agency fee. The deposit is two months, paid up front."]
    assert "[1]" in render.reply_chunks(result, show_sources=True)[0]
