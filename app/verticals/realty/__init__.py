"""Real estate agency assistant: RAG over agency policies + structured listing search via function calling."""
from __future__ import annotations

import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db import Listing
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
DISTRICTS = ["Alfama", "Baixa", "Chiado", "Campo de Ourique", "Avenidas Novas", "Parque das Nações", "Belém"]


def norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)).strip()


def price_label(listing: Listing) -> str:
    amount = f"€{listing.price_eur:,}"
    return f"{amount}/month" if listing.deal == "rent" else amount


def card(listing: Listing) -> Card:
    rooms = "Studio" if listing.rooms == 0 else f"{listing.rooms} bedroom{'s' if listing.rooms > 1 else ''}"
    extras = ["Furnished" if listing.furnished else "Unfurnished", "Pets allowed" if listing.pets else "No pets"]
    if listing.deal == "rent":
        available = date.fromisoformat(listing.available_from)
        extras.append(f"from {available.day} {available.strftime('%b')}")
    return Card(
        item_id=listing.id,
        title=f"🏠 {listing.title} · {listing.district}",
        lines=[f"{rooms} · {listing.area_m2} m² · floor {listing.floor}", f"<b>{price_label(listing)}</b>", " · ".join(extras)],
        buttons=[CardButton("📅 Book a viewing", f"book:{listing.id}")],
    )


def summary(listing: Listing) -> dict[str, Any]:
    return {
        "id": listing.id,
        "title": listing.title,
        "district": listing.district,
        "bedrooms": listing.rooms,
        "area_m2": listing.area_m2,
        "price_eur": listing.price_eur,
        "per": "month" if listing.deal == "rent" else "total",
        "furnished": listing.furnished,
        "pets_allowed": listing.pets,
        "available_from": listing.available_from,
    }


SEARCH_SPEC = ToolSpec(
    name="search_listings",
    description=(
        "Search the agency's flats. Call it whenever the user describes what they want to rent or buy. "
        "Results are shown to the user as cards with a 'Book a viewing' button."
    ),
    parameters={
        "type": "object",
        "properties": {
            "deal": {"type": "string", "enum": ["rent", "sale"], "description": "rent or buy (sale); rent if the user does not say"},
            "district": {"type": "string", "enum": DISTRICTS},
            "min_bedrooms": {"type": "integer", "minimum": 0, "description": "0 = studio"},
            "max_bedrooms": {"type": "integer", "minimum": 0},
            "max_price": {"type": "integer", "minimum": 0, "description": "EUR per month for rent, total for sale"},
            "min_price": {"type": "integer", "minimum": 0},
            "pets": {"type": "boolean", "description": "true if the user has a pet"},
            "furnished": {"type": "boolean"},
            "preferences": {
                "type": "string",
                "description": "Soft wishes in plain words (balcony, river view, quiet, near metro) used for ranking",
            },
            "limit": {"type": "integer", "minimum": 1, "description": "1-5, default 3"},
        },
        "required": [],  # `deal` is optional: small local models often leave it out; it defaults to rent
    },
)


async def search_listings(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    limit = min(int(args.get("limit", 3)), 5)

    async def run(max_price: int | None) -> list[Listing]:
        query = select(Listing).where(Listing.deal == args.get("deal", "rent"))
        if "min_bedrooms" in args:
            query = query.where(Listing.rooms >= args["min_bedrooms"])
        if "max_bedrooms" in args:
            query = query.where(Listing.rooms <= args["max_bedrooms"])
        if max_price is not None:
            query = query.where(Listing.price_eur <= max_price)
        if "min_price" in args:
            query = query.where(Listing.price_eur >= args["min_price"])
        if args.get("pets"):
            query = query.where(Listing.pets.is_(True))
        if "furnished" in args:
            query = query.where(Listing.furnished.is_(args["furnished"]))
        rows = list(await ctx.session.scalars(query.order_by(Listing.price_eur)))
        if "district" in args:
            rows = [r for r in rows if norm(r.district) == norm(args["district"])]
        if args.get("preferences") and ctx.rank_items and rows:
            rows = ordered(rows, ctx.rank_items(args["preferences"], [r.id for r in rows]))
        return rows

    rows = await run(args.get("max_price"))
    if rows:
        found = rows[:limit]
        return ToolOutput({"count": len(rows), "shown": [summary(r) for r in found]}, cards=[card(r) for r in found])
    if "max_price" in args:  # nothing in budget: show the closest options above it, clearly labelled
        above = sorted(await run(None), key=lambda r: r.price_eur)[:2]
        if above:
            return ToolOutput(
                {"count": 0, "note": "no matches within budget; closest options above budget are shown",
                 "shown": [summary(r) for r in above]},
                cards=[card(r) for r in above],
            )
    return ToolOutput({"count": 0, "note": "no matching listings"})


GET_SPEC = ToolSpec(
    name="get_listing",
    description="Full details of one listing by id (e.g. NH-103), for follow-up questions about a flat already shown.",
    parameters={"type": "object", "properties": {"listing_id": {"type": "string"}}, "required": ["listing_id"]},
)


async def get_listing(ctx: ToolContext, args: dict[str, Any]) -> ToolOutput:
    listing = await ctx.session.get(Listing, args["listing_id"].upper())
    if listing is None:
        return ToolOutput({"error": "listing not found"})
    return ToolOutput({**summary(listing), "floor": listing.floor, "description": listing.description}, cards=[card(listing)])


VERTICAL = Vertical(
    key="realty",
    company="Northwind Homes",
    assistant_name="Nora",
    domain="flats for rent and sale in central Lisbon",
    persona=(
        "You help people rent or buy a flat: find suitable listings, explain fees, documents and how viewings work, "
        "and book viewings. Be warm and concise, like a good agent."
    ),
    welcome=(
        "👋 Hi! I'm Nora, the assistant of <b>Northwind Homes</b> in Lisbon.\n\n"
        "Tell me what you're looking for in your own words — for example <i>“2 bedrooms in Chiado up to €2,400, "
        "I have a dog”</i> — or ask about fees, documents and viewings.\n\n"
        "A human agent is one tap away: <b>👤 Talk to an agent</b>."
    ),
    examples=[
        "2-bedroom to rent in Chiado up to €2,400, I have a dog",
        "Do tenants pay an agency fee?",
        "What documents do I need to rent?",
    ],
    human_button="👤 Talk to an agent",
    tools=[Tool(SEARCH_SPEC, search_listings), Tool(GET_SPEC, get_listing), HANDOFF_TOOL],
    knowledge_dir=HERE / "knowledge",
    catalog_model=Listing,
    catalog_csv=HERE / "listings.csv",
    item_text=lambda r: f"{r.title}. {r.district}. {r.description}",
    lead_actions={"book": LeadAction("viewing", "📅 Great choice! Share your phone number and an agent will confirm the viewing time.")},
)
