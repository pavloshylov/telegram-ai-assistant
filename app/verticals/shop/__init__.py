"""Online store assistant: RAG over store policies + product search (filters + semantic ranking) via function calling."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db import Product
from app.llm.base import ToolSpec
from app.verticals.base import (
    HANDOFF_TOOL,
    Card,
    CardButton,
    LeadAction,
    Tool,
    ToolContext,
    ToolOutput,
    Vertical,
    ordered,
)

HERE = Path(__file__).parent
CATEGORIES = ["over-ear", "earbuds", "speaker", "accessory"]
ICONS = {"over-ear": "🎧", "earbuds": "🎵", "speaker": "🔊", "accessory": "🧩"}


def price(value: float) -> str:
    return f"€{value:,.0f}" if value == int(value) else f"€{value:,.2f}"


def card(p: Product) -> Card:
    features = []
    if p.anc:
        features.append("Noise cancelling")
    if p.multipoint:
        features.append("Multipoint")
    if p.battery_h:
        features.append(f"{p.battery_h} h battery")
    if p.water_rating != "none":
        features.append(p.water_rating)
    stock = f"in stock ({p.stock})" if p.stock > 0 else "out of stock"
    return Card(
        item_id=p.id,
        title=f"{ICONS.get(p.category, '🛍')} {p.title} · {p.brand}",
        lines=[f"<b>{price(p.price_eur)}</b> · {stock}", " · ".join(features) or p.category, f"{p.warranty_years}-year warranty"],
        buttons=[CardButton("🛒 Order", f"order:{p.id}")] if p.stock > 0 else [],
    )


def summary(p: Product) -> dict[str, Any]:
    return {
        "id": p.id,
        "title": p.title,
        "category": p.category,
        "price_eur": p.price_eur,
        "in_stock": p.stock > 0,
        "noise_cancelling": p.anc,
        "water_rating": p.water_rating,
        "battery_hours": p.battery_h,
        "multipoint": p.multipoint,
        "warranty_years": p.warranty_years,
    }


SEARCH_SPEC = ToolSpec(
    name="search_products",
    description=(
        "Search the store catalog. Call it whenever the user asks for a product, a recommendation or a price. "
        "Results are shown to the user as cards with an 'Order' button."
    ),
    parameters={
        "type": "object",
        "properties": {
            "need": {
                "type": "string",
                "description": "What the product is for, in plain words (running, flights, calls, kids, beach)",
            },
            "category": {"type": "string", "enum": CATEGORIES},
            "max_price": {"type": "number", "minimum": 0, "description": "EUR"},
            "min_price": {"type": "number", "minimum": 0},
            "noise_cancelling": {"type": "boolean"},
            "waterproof": {"type": "boolean", "description": "needs a water/sweat rating"},
            "multipoint": {"type": "boolean"},
            "include_out_of_stock": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "description": "1-5, default 3"},
        },
    },
)


async def search_products(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    limit = min(int(args.get("limit", 3)), 5)
    query = select(Product)
    if "category" in args:
        query = query.where(Product.category == args["category"])
    if "max_price" in args:
        query = query.where(Product.price_eur <= args["max_price"])
    if "min_price" in args:
        query = query.where(Product.price_eur >= args["min_price"])
    if "noise_cancelling" in args:
        query = query.where(Product.anc.is_(args["noise_cancelling"]))
    if args.get("waterproof"):
        query = query.where(Product.water_rating != "none")
    if "multipoint" in args:
        query = query.where(Product.multipoint.is_(args["multipoint"]))
    if not args.get("include_out_of_stock"):
        query = query.where(Product.stock > 0)
    rows = list(await ctx.session.scalars(query.order_by(Product.price_eur)))
    if args.get("need") and ctx.rank_items and rows:
        rows = ordered(rows, ctx.rank_items(args["need"], [r.id for r in rows]))
    found = rows[:limit]
    if not found:
        return ToolOutput({"count": 0, "note": "no matching products; suggest relaxing a filter"})
    return ToolOutput({"count": len(rows), "shown": [summary(r) for r in found]}, cards=[card(r) for r in found])


GET_SPEC = ToolSpec(
    name="get_product",
    description="Full details of one product by id (e.g. NA-07), for follow-up questions about a product already shown.",
    parameters={"type": "object", "properties": {"product_id": {"type": "string"}}, "required": ["product_id"]},
)


async def get_product(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    product = await ctx.session.get(Product, args["product_id"].upper())
    if product is None:
        return ToolOutput({"error": "product not found"})
    return ToolOutput({**summary(product), "description": product.description}, cards=[card(product)])


VERTICAL = Vertical(
    key="shop",
    company="Northwind Audio",
    assistant_name="Max",
    domain="headphones, earbuds and speakers sold by the store, and the store's delivery, payment and return rules",
    persona=(
        "You are a helpful shop assistant: recommend products from the catalog for the user's needs, compare them "
        "honestly, and answer questions about delivery, payment, returns and warranty."
    ),
    welcome=(
        "👋 Hi! I'm Max from <b>Northwind Audio</b>.\n\n"
        "Tell me what you need — for example <i>“earbuds for running under €130”</i> — or ask about delivery, "
        "returns and warranty.\n\n"
        "Prefer a person? Tap <b>👤 Talk to a person</b>."
    ),
    examples=[
        "Earbuds for running under €130",
        "How long does delivery to Ukraine take?",
        "Can I return earbuds I already opened?",
    ],
    human_button="👤 Talk to a person",
    tools=[Tool(SEARCH_SPEC, search_products), Tool(GET_SPEC, get_product), HANDOFF_TOOL],
    knowledge_dir=HERE / "knowledge",
    catalog_model=Product,
    catalog_csv=HERE / "products.csv",
    item_text=lambda p: f"{p.title}. {p.category}. {p.description}",
    lead_actions={"order": LeadAction("order", "🛒 Almost done! Share your phone number and we'll confirm the order and delivery.")},
)
