"""Catalog tools (SQL filters + semantic ranking) and argument validation."""
from __future__ import annotations

from app.llm.base import ToolCall
from app.verticals.base import ToolContext, validate_args


async def run(agent, session_maker, name, args):
    async with session_maker() as session:
        ctx = ToolContext(session=session, rank_items=agent.rank_items)
        return await agent.vertical.run_tool(ctx, ToolCall(name=name, args=args))


async def test_listing_search_filters(stack):
    _, sm, agent = await stack("realty")
    out = await run(agent, sm, "search_listings",
                    {"deal": "rent", "district": "Chiado", "min_bedrooms": 2, "max_price": 2400, "pets": True})
    assert [c.item_id for c in out.cards] == ["NH-103"]
    assert out.data["shown"][0]["pets_allowed"] is True
    assert out.cards[0].buttons[0].action == "book:NH-103"


async def test_district_match_ignores_accents_and_case(stack):
    _, sm, agent = await stack("realty")
    out = await run(agent, sm, "search_listings", {"deal": "rent", "district": "Parque das Nações", "max_bedrooms": 0})
    assert [c.item_id for c in out.cards] == ["NH-112"]
    out = await run(agent, sm, "search_listings", {"deal": "rent", "district": "parque das nacoes"})
    assert out.data["error"] == "invalid arguments"  # not in the enum: the model is told, nothing invented


async def test_over_budget_shows_closest_options_labelled(stack):
    _, sm, agent = await stack("realty")
    out = await run(agent, sm, "search_listings", {"deal": "rent", "district": "Chiado", "max_price": 1000})
    assert out.data["count"] == 0 and "above budget" in out.data["note"]
    assert [c.item_id for c in out.cards] == ["NH-103", "NH-110"]


async def test_semantic_preferences_reorder_results(stack):
    _, sm, agent = await stack("realty")
    out = await run(agent, sm, "search_listings", {"deal": "rent", "preferences": "swimming pool and gym", "limit": 1})
    assert out.cards[0].item_id == "NH-106"


async def test_product_search_for_running(stack):
    _, sm, agent = await stack("shop")
    out = await run(agent, sm, "search_products",
                    {"category": "earbuds", "max_price": 130, "waterproof": True, "need": "running, sport, outdoor"})
    assert [c.item_id for c in out.cards][:2] == ["NA-10", "NA-06"]
    assert all(s["price_eur"] <= 130 for s in out.data["shown"])


async def test_out_of_stock_hidden_unless_asked(stack):
    _, sm, agent = await stack("shop")
    out = await run(agent, sm, "search_products", {"need": "foldable travel headphones", "limit": 5})
    assert "NA-04" not in [c.item_id for c in out.cards]
    out = await run(agent, sm, "search_products",
                    {"need": "foldable travel headphones", "include_out_of_stock": True, "limit": 1})
    assert out.cards[0].item_id == "NA-04" and out.cards[0].buttons == []  # no Order button when out of stock


async def test_get_item_and_unknown_tool(stack):
    _, sm, agent = await stack("shop")
    out = await run(agent, sm, "get_product", {"product_id": "na-07"})
    assert out.data["title"] == "Pulse Pro ANC" and out.data["multipoint"] is True
    assert (await run(agent, sm, "get_product", {"product_id": "NA-99"})).data == {"error": "product not found"}
    assert "unknown tool" in (await run(agent, sm, "delete_everything", {})).data["error"]


def test_validate_args():
    schema = {
        "type": "object",
        "properties": {
            "deal": {"type": "string", "enum": ["rent", "sale"]},
            "max_price": {"type": "integer", "minimum": 0},
            "pets": {"type": "boolean"},
        },
        "required": ["deal"],
    }
    clean, problems = validate_args(schema, {"deal": "rent", "max_price": "2400.0", "pets": "true", "hack": 1})
    assert clean == {"deal": "rent", "max_price": 2400, "pets": True} and problems == []
    _, problems = validate_args(schema, {"deal": "lease", "max_price": -5})
    assert "deal: must be one of ['rent', 'sale']" in problems and "max_price: must be >= 0" in problems
    _, problems = validate_args(schema, {"max_price": "cheap"})
    assert "max_price: expected integer" in problems and "deal: required" in problems
