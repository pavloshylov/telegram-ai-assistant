"""End-to-end through the Telegram layer: real dispatcher, agent (demo provider), retrieval and database."""
from __future__ import annotations

import csv
import io

from sqlalchemy import select

from app.db import Lead, Message
from tests.test_agent import FakeModel
from app.llm.base import LLMError, LLMReply


async def test_start_shows_welcome_examples_and_human_button(bot):
    env = await bot("realty")
    user = env.user(201, "Anna", "anna_k")
    await env.start(user)
    welcome, examples = env.texts(user)[-2:]
    assert "Nora, the assistant of <b>Northwind Homes</b>" in welcome
    assert env.chat(user).reply_keyboard.keyboard[0][0].text == "👤 Talk to an agent"
    assert env.last(user).button_texts()[0] == "2-bedroom to rent in Chiado up to €2,400, I have a dog"


async def test_example_question_answers_with_sources(bot):
    env = await bot("realty")
    user = env.user(201, "Anna")
    await env.start(user)
    await env.click(user, "Do tenants pay an agency fee?")
    answer = env.last(user).text
    assert "tenants pay no agency fee" in answer and "[1]" in answer
    assert "📚 <i>Sources: [1] Fees and commission › Rentals</i>" in answer


async def test_search_shows_cards_then_viewing_lead_reaches_admin(bot, admin):
    env = await bot("realty")
    anna = env.user(201, "Anna", "anna_k")
    await env.send(anna, "2-bedroom to rent in Chiado up to €2,400, I have a dog")
    reply, card = env.texts(anna)[-2:]
    assert "pet-friendly 2-bedroom in Chiado" in reply
    assert "Elegant 2-bedroom with balcony · Chiado" in card and "€2,300/month" in card
    await env.click(anna, "📅 Book a viewing")
    assert "Share your phone number" in env.last(anna).text
    assert env.chat(anna).reply_keyboard.keyboard[0][0].request_contact is True
    await env.send_contact(anna, "+351912345678")
    assert "We'll contact you at +351912345678" in env.last(anna).text
    note = env.last(admin).text
    assert "New viewing lead #1" in note and "NH-103" in note and "+351912345678" in note
    assert "2-bedroom to rent in Chiado" in note  # transcript of the conversation
    async with env.session_maker() as session:
        lead = await session.scalar(select(Lead))
    assert (lead.kind, lead.item_id, lead.phone) == ("viewing", "NH-103", "+351912345678")


async def test_human_button_validates_typed_phone_and_cancel(bot, admin):
    env = await bot("shop")
    tom = env.user(202, "Tom")
    await env.start(tom)
    await env.send(tom, "👤 Talk to a person")
    await env.send(tom, "call me maybe")
    assert "valid phone number" in env.last(tom).text
    await env.send(tom, "✖️ Cancel")
    assert env.last(tom).text == "OK, cancelled. Ask me anything."
    await env.send(tom, "👤 Talk to a person")
    await env.send(tom, "+48 600 100 200")
    assert "New human lead #1" in env.last(admin).text


async def test_handoff_from_the_model_asks_for_phone(bot):
    env = await bot("shop")
    tom = env.user(202, "Tom")
    await env.send(tom, "I want to talk to a human")
    texts = env.texts(tom)
    assert "colleague will get back to you" in texts[-1] and "contact you" not in texts[-1]  # one message, no duplicate
    assert env.chat(tom).reply_keyboard.keyboard[0][0].request_contact is True
    await env.send(tom, "+48 600 100 200")
    assert "We'll contact you" in env.last(tom).text


async def test_handoff_without_phone_mention_gets_a_hint(bot):
    from app.llm.base import ToolCall

    model = FakeModel(LLMReply(tool_calls=[ToolCall("handoff_to_human", {"reason": "complaint"})]),
                      LLMReply(text="I'm sorry about that. A colleague will help."))
    env = await bot("shop", model)
    tom = env.user(202, "Tom")
    await env.send(tom, "My order arrived broken")
    assert env.last(tom).text.endswith("📱 Share your phone number with the button below (or type it).")


async def test_shop_order_flow_and_ukrainian_question(bot, admin):
    env = await bot("shop")
    olena = env.user(203, "Олена", None, lang="uk")
    await env.send(olena, "Порадьте навушники для бігу до 130 євро")
    texts = env.texts(olena)
    assert "Для бігу раджу Pulse Run Open" in texts[-4] and "Pulse Run Open · Pulse" in texts[-3]
    run_open = next(b for b in env.chat(olena).bot_bubbles() if "Pulse Run Open · Pulse" in b.text)
    await env.click_in(olena, run_open, "🛒 Order")
    await env.send_contact(olena, "+380501234567")
    assert "New order lead #1" in env.last(admin).text and "NA-10" in env.last(admin).text


async def test_unscripted_question_in_demo_mode_is_honest(bot):
    env = await bot("realty")
    user = env.user(201, "Anna")
    await env.send(user, "Is parking included?")
    answer = env.last(user).text
    assert answer.startswith("This demo runs without a live AI model")
    assert "Fees and commission › Other costs" in answer


async def test_history_and_token_usage_are_stored(bot):
    model = FakeModel(LLMReply(text="Hello! [1]"), LLMReply(text="Still here."))
    env = await bot("shop", model)
    tom = env.user(202, "Tom")
    await env.send(tom, "How long does delivery to Ukraine take?")
    await env.send(tom, "And returns?")
    second_call = model.calls[1]["messages"]
    assert [m.role for m in second_call] == ["user", "assistant", "user"]
    async with env.session_maker() as session:
        rows = list(await session.scalars(select(Message).order_by(Message.id)))
    assert [r.role for r in rows] == ["user", "assistant", "user", "assistant"]
    assert rows[1].meta["sources"] == ["Delivery › Options and prices"]


async def test_rate_limit(bot):
    env = await bot("shop", rate_limit_per_hour=2)
    tom = env.user(202, "Tom")
    for _ in range(3):
        await env.send(tom, "How long does delivery to Ukraine take?")
    assert "a lot of messages in the last hour" in env.last(tom).text


async def test_provider_outage_offers_a_person(bot):
    env = await bot("realty", FakeModel(LLMError("503")))
    user = env.user(201, "Anna")
    await env.send(user, "Any studios?")
    assert "couldn't answer right now" in env.last(user).text
    assert env.chat(user).reply_keyboard.keyboard[0][0].request_contact is True


async def test_non_text_message(bot):
    env = await bot("realty")
    user = env.user(201, "Anna")
    await env.send_sticker_like(user)
    assert env.last(user).text == "I can read text messages only. Please type your question."


async def test_admin_report_and_csv_export(bot, admin):
    env = await bot("realty")
    anna = env.user(201, "Anna", "anna_k")
    await env.send(anna, "Do tenants pay an agency fee?")
    await env.send(anna, "2-bedroom to rent in Chiado up to €2,400, I have a dog")
    await env.click(anna, "📅 Book a viewing")
    await env.send_contact(anna, "+351912345678")
    await env.send(admin, "/admin")
    report = env.last(admin).text
    assert "Questions: <b>2</b>" in report and "with sources cited: 1" in report and "cards shown: 1" in report
    assert "Leads: <b>1</b> (viewing: 1)" in report and "Estimated model cost" in report
    await env.click(admin, "📥 Export leads (CSV)")
    filename, data = env.last(admin).document
    rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    assert filename.startswith("leads_") and rows[0]["item_id"] == "NH-103" and rows[0]["telegram"] == "@anna_k"


async def test_admin_command_is_not_available_to_users(bot):
    env = await bot("realty")
    anna = env.user(201, "Anna")
    await env.send(anna, "/admin")
    assert "Estimated model cost" not in env.last(anna).text
