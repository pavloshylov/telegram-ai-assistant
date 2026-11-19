"""The agent loop with a programmable fake model: tool calls, citations, guards, failures."""
from __future__ import annotations

from app.agent import strip_json
from app.llm.base import LLMError, LLMReply, ToolCall, Usage


class FakeModel:
    """Returns queued replies and records what the agent sent."""

    name = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def complete(self, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages), "tools": [t.name for t in tools]})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


async def test_tool_call_then_answer_with_citation(stack):
    model = FakeModel(
        LLMReply(tool_calls=[ToolCall("search_listings", {"deal": "rent", "district": "Belém"}, id="c1")], usage=Usage(900, 20)),
        LLMReply(text="Here is a flat by the river. Tenants pay no agency fee [1].", usage=Usage(1200, 40)),
    )
    _, sm, agent = await stack("realty", model)
    async with sm() as session:
        result = await agent.answer(session, [], "Flat in Belém, and do tenants pay an agency fee?")
    assert [c.item_id for c in result.cards] == ["NH-107"]
    assert [h.chunk.title for h in result.sources] == ["Fees and commission › Rentals"]
    assert result.usage.input_tokens == 2100 and result.usage.output_tokens == 60
    second = model.calls[1]["messages"]
    assert second[-1].role == "tool" and second[-1].tool_result["shown"][0]["id"] == "NH-107"
    assert "search_listings" in model.calls[0]["tools"] and "handoff_to_human" in model.calls[0]["tools"]
    assert "[1] Fees and commission › Rentals" in model.calls[0]["system"]


async def test_system_prompt_carries_rules_and_context(stack):
    model = FakeModel(LLMReply(text="ok"))
    _, sm, agent = await stack("shop", model)
    async with sm() as session:
        await agent.answer(session, [], "How long does delivery to Ukraine take?")
    system = model.calls[0]["system"]
    assert "You are Max, the assistant of Northwind Audio" in system
    assert "Never invent items, prices or availability" in system
    assert "Delivery › Options and prices" in system


async def test_bad_tool_args_are_reported_to_the_model(stack):
    model = FakeModel(
        LLMReply(tool_calls=[ToolCall("search_listings", {"district": "Chiado"})]),  # 'deal' missing
        LLMReply(text="Do you want to rent or buy?"),
    )
    _, sm, agent = await stack("realty", model)
    async with sm() as session:
        result = await agent.answer(session, [], "Something in Chiado")
    assert result.tool_log[0]["result"] == {"error": "invalid arguments", "details": ["deal: required"]}
    assert result.text == "Do you want to rent or buy?" and result.cards == []


async def test_handoff_tool_sets_flag(stack):
    model = FakeModel(
        LLMReply(tool_calls=[ToolCall("handoff_to_human", {"reason": "complaint"})]),
        LLMReply(text="I'll connect you with a colleague."),
    )
    _, sm, agent = await stack("shop", model)
    async with sm() as session:
        result = await agent.answer(session, [], "My order arrived broken!")
    assert result.handoff is True


async def test_loop_is_bounded(stack):
    looping = [LLMReply(tool_calls=[ToolCall("get_product", {"product_id": "NA-01"})]) for _ in range(10)]
    model = FakeModel(*looping)
    _, sm, agent = await stack("shop", model)
    async with sm() as session:
        result = await agent.answer(session, [], "Tell me about Aria ANC 700")
    assert len(model.calls) == agent.max_steps and result.handoff is True
    assert [c.item_id for c in result.cards] == ["NA-01"]  # same card once, not four times


async def test_provider_failure_is_reported_not_raised(stack):
    model = FakeModel(LLMError("Gemini API error 503"))
    _, sm, agent = await stack("realty", model)
    async with sm() as session:
        result = await agent.answer(session, [], "Any studios?")
    assert result.failed is True and result.handoff is True


def test_strip_json_removes_echoed_tool_data_and_keeps_prose():
    raw = 'I found a flat in Chiado [1].\n\n```json\n{"cards": [{"id": "NH-103", "price_eur": 2300}]}\n```\n\nShall I book it?'
    assert strip_json(raw) == "I found a flat in Chiado [1].\n\nShall I book it?"
    assert strip_json('{\n  "cards": [{"id": "NH-103"}]\n}') == ""
    plain = "Plain answer with {braces} and a price of 5 EUR."
    assert strip_json(plain) == plain
    assert strip_json('Unfinished {"cards": [') == 'Unfinished {"cards": ['


async def test_agent_hides_json_the_model_made_up(stack):
    model = FakeModel(LLMReply(text='Here you go.\n{"cards": [{"id": "NH-103", "title": "Flat"}]}'))
    _, sm, agent = await stack("realty", model)
    async with sm() as session:
        result = await agent.answer(session, [], "Show me something")
    assert result.text == "Here you go."



async def test_promise_of_a_colleague_without_the_tool_still_hands_off(stack):
    model = FakeModel(LLMReply(text="I am not sure about move-out cleaning, so let me connect you with a colleague."))
    _, sm, agent = await stack("realty", model)
    async with sm() as session:
        result = await agent.answer(session, [], "Do you offer cleaning after move-out?")
    assert result.handoff is True and result.tool_log == []


async def test_ordinary_answers_do_not_trigger_a_hand_off(stack):
    for text in (
        "Tenants pay no agency fee; the landlord pays our commission [1].",
        "I can only help with flats in central Lisbon.",
        "Our agents show flats every weekday from 9:00 to 18:00.",
        "Ось повнорозмірні навушники до 100 євро: Aria Kids і Basso Gamer.",
    ):
        model = FakeModel(LLMReply(text=text))
        _, sm, agent = await stack("realty", model)
        async with sm() as session:
            result = await agent.answer(session, [], "hello")
        assert result.handoff is False, text


async def test_ukrainian_promise_of_a_colleague_hands_off(stack):
    model = FakeModel(LLMReply(text="Я не впевнена, тому з’єдную вас із колегою, який допоможе."))
    _, sm, agent = await stack("realty", model)
    async with sm() as session:
        result = await agent.answer(session, [], "Чи є у вас клінінг після виїзду?")
    assert result.handoff is True
