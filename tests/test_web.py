"""Website widget API: chat, cards, leads, limits, origin rules, admin API (httpx against the ASGI app, no network)."""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import Lead, Message, User
from app.llm.base import LLMError, LLMReply, ToolCall, Usage
from app.web.api import HourlyLimiter, create_app, html_to_markdown, visitor_id
from tests.test_agent import FakeModel

SID = "visitor-session-0001"  # 20 chars, matches the allowed pattern
SID2 = "visitor-session-0002"


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, lead, item_title, transcript):
        self.sent.append((lead, item_title, [m.text for m in transcript]))


@pytest_asyncio.fixture
async def web(stack):
    """Factory: (vertical, replies..., **settings) -> (client, model, session_maker, notifier)."""
    clients = []

    async def build(vertical, *replies, **overrides):
        model = FakeModel(*replies)
        settings, sm, agent = await stack(vertical, model, **overrides)
        notifier = RecordingNotifier()
        app = create_app(settings, sm, agent, notifier)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client, model, sm, notifier

    yield build
    for client in clients:
        await client.aclose()


def search_then_answer():
    return (
        LLMReply(tool_calls=[ToolCall("search_listings", {"deal": "rent", "district": "Belém"}, id="c1")], usage=Usage(900, 20)),
        LLMReply(text="Here is a flat by the river. Tenants pay no agency fee [1].", usage=Usage(1200, 40)),
    )


async def test_health_and_config(web):
    client, *_ = await web("realty")
    health = (await client.get("/api/health")).json()
    assert health["status"] == "ok" and health["business"] == "Northwind Homes" and health["knowledge_sections"] > 5
    config = (await client.get("/api/config")).json()
    assert config["assistant"] == "Nora" and "<b>" not in config["welcome"] and "**Northwind Homes**" in config["welcome"]
    assert set(config["lead_prompts"]) == {"book", "human"} and len(config["examples"]) == 3


async def test_chat_returns_reply_sources_and_cards(web):
    client, model, sm, _ = await web("realty", *search_then_answer())
    res = await client.post("/api/chat", json={"session_id": SID, "message": "Flat in Belém, any agency fee?"})
    body = res.json()
    assert res.status_code == 200 and body["status"] == "ok" and body["handoff"] is False
    assert body["reply"].startswith("Here is a flat by the river")
    assert body["sources"] == [{"n": 1, "title": "Fees and commission › Rentals"}]
    card = body["cards"][0]
    assert card["id"] == "NH-107" and card["buttons"] == [{"text": "📅 Book a viewing", "action": "book:NH-107"}]
    assert any(line == "**€1,300/month**" for line in card["lines"])  # <b> became **bold**, no HTML reaches the widget
    async with sm() as session:
        user = await session.get(User, visitor_id(SID))
        assert user.id < 0 and user.first_name == "Website visitor"
        rows = list(await session.scalars(select(Message).where(Message.user_id == user.id).order_by(Message.id)))
        assert [r.role for r in rows] == ["user", "assistant"] and rows[1].input_tokens == 2100


async def test_history_is_kept_per_visitor(web):
    client, model, *_ = await web("realty", LLMReply(text="First answer."), LLMReply(text="Second answer."),
                                  LLMReply(text="Someone else."))
    await client.post("/api/chat", json={"session_id": SID, "message": "My first question"})
    await client.post("/api/chat", json={"session_id": SID, "message": "And a follow-up"})
    await client.post("/api/chat", json={"session_id": SID2, "message": "A different visitor"})
    second = [m.text for m in model.calls[1]["messages"]]
    assert second[:2] == ["My first question", "First answer."] and second[-1] == "And a follow-up"
    assert [m.text for m in model.calls[2]["messages"]] == ["A different visitor"]  # no leakage between visitors


@pytest.mark.parametrize("payload", [
    {"session_id": "short", "message": "hi"},
    {"session_id": "has spaces in the id!!", "message": "hi"},
    {"session_id": SID, "message": ""},
    {"session_id": SID, "message": "x" * 1001},
    {"message": "no session"},
])
async def test_chat_rejects_bad_input(web, payload):
    client, model, *_ = await web("realty")
    res = await client.post("/api/chat", json=payload)
    assert res.status_code == 422 and model.calls == []


async def test_per_visitor_rate_limit_refuses_without_calling_the_model(web):
    client, model, *_ = await web("shop", LLMReply(text="one"), LLMReply(text="two"), rate_limit_per_hour=2)
    for _ in range(2):
        assert (await client.post("/api/chat", json={"session_id": SID, "message": "hello"})).status_code == 200
    res = await client.post("/api/chat", json={"session_id": SID, "message": "again"})
    assert res.status_code == 429 and res.json()["status"] == "rate_limited" and res.headers["retry-after"] == "3600"
    assert len(model.calls) == 2


async def test_ip_rate_limit_stops_session_id_rotation(web):
    client, model, *_ = await web("shop", LLMReply(text="one"), LLMReply(text="two"), ip_rate_limit_per_hour=1)
    assert (await client.post("/api/chat", json={"session_id": SID, "message": "hello"})).status_code == 200
    res = await client.post("/api/chat", json={"session_id": SID2, "message": "new id, same address"})
    assert res.status_code == 429 and len(model.calls) == 1


async def test_model_failure_answers_politely_and_offers_a_person(web):
    client, *_ = await web("realty", LLMError("Gemini API error 503"))
    res = await client.post("/api/chat", json={"session_id": SID, "message": "Any studios?"})
    body = res.json()
    assert res.status_code == 200 and body["status"] == "failed" and body["handoff"] is True and body["cards"] == []


async def test_handoff_tool_sets_the_flag(web):
    client, *_ = await web(
        "shop",
        LLMReply(tool_calls=[ToolCall("handoff_to_human", {"reason": "student discount"}, id="h1")]),
        LLMReply(text="I am not sure about student discounts; let me get a colleague."),
    )
    body = (await client.post("/api/chat", json={"session_id": SID, "message": "Student discount?"})).json()
    assert body["handoff"] is True and body["status"] == "ok"


async def test_lead_from_a_card_button_is_saved_and_notified(web):
    client, _, sm, notifier = await web("realty", *search_then_answer())
    await client.post("/api/chat", json={"session_id": SID, "message": "Flat in Belém"})
    res = await client.post("/api/lead", json={"session_id": SID, "action": "book:NH-107", "phone": "+351 912 345 678",
                                               "name": "Anna"})
    assert res.status_code == 200 and "+351 912 345 678" in res.json()["message"]
    async with sm() as session:
        lead = (await session.scalars(select(Lead))).one()
        assert (lead.kind, lead.item_id, lead.name) == ("viewing", "NH-107", "Anna") and lead.user_id < 0
    saved, item_title, transcript = notifier.sent[0]
    assert item_title == "1-bedroom near the river promenade" and transcript[0] == "Flat in Belém"


async def test_human_lead_and_validation(web):
    client, _, sm, notifier = await web("shop")
    assert (await client.post("/api/lead", json={"session_id": SID, "action": "human", "phone": "abc"})).status_code == 422
    assert (await client.post("/api/lead", json={"session_id": SID, "action": "drop:table", "phone": "+48 600 100 200"})).status_code == 422
    assert (await client.post("/api/lead", json={"session_id": SID, "action": "order:NA-99", "phone": "+48 600 100 200"})).status_code == 404
    assert notifier.sent == []
    ok = await client.post("/api/lead", json={"session_id": SID, "action": "human", "phone": "+48 600 100 200"})
    assert ok.status_code == 200
    async with sm() as session:
        assert (await session.scalars(select(Lead.kind))).one() == "human"


async def test_origin_rules(web):
    client, model, *_ = await web("shop", LLMReply(text="ok"), LLMReply(text="ok"), allowed_origins=("https://shop.example.com",))
    body = {"session_id": SID, "message": "hello"}
    evil = await client.post("/api/chat", json=body, headers={"Origin": "https://evil.example.org"})
    assert evil.status_code == 403 and model.calls == []
    allowed = await client.post("/api/chat", json=body, headers={"Origin": "https://shop.example.com"})
    assert allowed.status_code == 200 and allowed.headers["access-control-allow-origin"] == "https://shop.example.com"
    same_site = await client.post("/api/chat", json=body, headers={"Origin": "http://test"})  # the demo site on this server
    assert same_site.status_code == 200


async def test_preflight_only_for_allowed_sites(web):
    client, *_ = await web("shop", allowed_origins=("https://shop.example.com",))
    ask = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
    good = await client.options("/api/chat", headers={"Origin": "https://shop.example.com", **ask})
    bad = await client.options("/api/chat", headers={"Origin": "https://evil.example.org", **ask})
    assert good.status_code == 200 and good.headers["access-control-allow-origin"] == "https://shop.example.com"
    assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers


async def test_admin_api_is_off_without_a_key_and_protected_with_one(web):
    off, *_ = await web("shop")
    assert (await off.get("/api/admin/stats")).status_code == 404
    client, _, sm, _ = await web("shop", LLMReply(text="Hello!", usage=Usage(1000, 50)), admin_api_key="s3cret-key")
    await client.post("/api/chat", json={"session_id": SID, "message": "hi"})
    await client.post("/api/lead", json={"session_id": SID, "action": "human", "phone": "+48 600 100 200"})
    assert (await client.get("/api/admin/stats")).status_code == 401
    assert (await client.get("/api/admin/stats", headers={"X-Admin-Key": "wrong"})).status_code == 401
    stats = (await client.get("/api/admin/stats", headers={"X-Admin-Key": "s3cret-key"})).json()
    assert stats["questions"] == 1 and stats["tokens_in"] == 1000 and stats["lead_kinds"] == {"human": 1}
    csv_text = (await client.get("/api/admin/leads.csv", headers={"X-Admin-Key": "s3cret-key"})).text
    assert "channel" in csv_text.splitlines()[0] and ",web," in csv_text


async def test_widget_script_and_demo_site_are_served(web):
    for vertical, brand in (("realty", "Northwind Homes"), ("shop", "Northwind Audio")):
        client, *_ = await web(vertical)
        script = await client.get("/widget.js")
        assert script.status_code == 200 and "javascript" in script.headers["content-type"]
        assert "AssistantWidget" in script.text and "innerHTML" in script.text  # innerHTML only for the static launcher icon
        page = await client.get("/")
        assert page.status_code == 200 and brand in page.text and '/widget.js"' in page.text


def test_widget_never_inserts_server_text_as_html():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "app" / "web" / "static" / "widget.js").read_text(encoding="utf-8")
    uses = [line.strip() for line in source.splitlines() if "innerHTML" in line or "insertAdjacentHTML" in line or "outerHTML" in line]
    assert len(uses) == 1 and uses[0].startswith("launcher.innerHTML = '<svg")  # the only one: a constant icon


async def test_telegram_notifier_posts_the_lead_to_every_admin_without_leaking_the_token(caplog):
    from app.config import Settings
    from app.db import Lead
    from app.web.notify import TelegramNotifier, make_notifier

    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), request.read().decode()))
        return httpx.Response(200 if "111" in sent[-1][1] else 502, json={"ok": True})

    settings = Settings(bot_token="123:SECRET", admin_ids=frozenset({111, 222}))
    notifier = TelegramNotifier(settings, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    lead = Lead(id=7, user_id=-5, kind="viewing", item_id="NH-103", phone="+351 912 345 678", name="Anna <b>", note="x")
    await notifier.notify(lead, "Elegant 2-bedroom", [])
    assert len(sent) == 2 and all(url.endswith("/bot123:SECRET/sendMessage") for url, _ in sent)
    assert "Anna &lt;b&gt;" in sent[0][1] and "viewing (website)" in sent[0][1] and "NH-103" in sent[0][1]
    assert "SECRET" not in caplog.text  # a failing admin (502) is logged by type only, never with the URL/token
    assert make_notifier(Settings(bot_token="")) is None and make_notifier(Settings(bot_token="123:SECRET")) is None
    assert make_notifier(settings) is not None


def test_helpers():
    assert html_to_markdown("Price <b>€2,300</b> &amp; <i>more</i>") == "Price **€2,300** & more"
    assert visitor_id(SID) < 0 and visitor_id(SID) == visitor_id(SID) and visitor_id(SID) != visitor_id(SID2)
    limiter = HourlyLimiter(2)
    assert limiter.allow("a", now=0) and limiter.allow("a", now=10) and not limiter.allow("a", now=20)
    assert limiter.allow("b", now=20) and limiter.allow("a", now=3601)  # window slides
