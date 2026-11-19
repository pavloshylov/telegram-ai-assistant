"""Scripted demo replies (offline mode). Wording only; search, retrieval and citations are computed live."""
from app.llm.scripted import Rule

RULES = [
    Rule(
        r"weather|pizza|football|joke|bitcoin|recipe",
        "I can only help with Northwind Audio products, orders and delivery. Looking for headphones or a speaker?",
    ),
    Rule(
        r"\b(human|person|manager|operator|agent)\b|з людиною|менеджер",
        "Sure — a colleague will get back to you. Please share your phone number with the button below.",
        tool="handoff_to_human",
        args={"reason": "user asked for a person"},
    ),
    Rule(
        r"для бігу|бігати",
        "Для бігу раджу Pulse Run Open — відкриті навушники, тож ви чутимете дорогу, захист IP67. Якщо хочеться "
        "щільної посадки, Pulse Sport 2 з дужками. Обидві моделі є в наявності й укладаються в €130.",
        tool="search_products",
        args={"category": "earbuds", "max_price": 130, "waterproof": True, "need": "running, sport, outdoor"},
    ),
    Rule(
        r"\brun(ning)?\b|\bjog",
        "For running I'd pick the Pulse Run Open — open-ear, so you still hear traffic, and IP67 — or the Pulse "
        "Sport 2 with ear hooks if you prefer a secure in-ear fit. Both are in stock and under €130.",
        tool="search_products",
        args={"category": "earbuds", "max_price": 130, "waterproof": True, "need": "running, sport, outdoor"},
    ),
    Rule(
        r"flights?|plane|travel|commut",
        "For flights, noise cancelling matters most — here are our ANC headphones in stock. For long trips I'd look "
        "at the Aria ANC 500 (40 h battery) or the flagship Aria ANC 700; the Nordwave Office is the pick for calls.",
        tool="search_products",
        args={"category": "over-ear", "noise_cancelling": True, "need": "flights, travel, noise cancelling"},
    ),
    Rule(
        r"ukrain|україн",
        "Delivery to Ukraine takes 5–8 working days and costs €12 {cite:Options and prices}. You'll get a tracking "
        "link as soon as the parcel leaves our warehouse {cite:Tracking}.",
    ),
    Rule(
        r"return|opened|refund|повернут",
        "You can return anything within 30 days, and return shipping in the EU is free {cite:Returns}. Earbuds are "
        "the exception: for hygiene reasons they can only be returned if the sealed ear tips are unopened "
        "{cite:Returns}.",
    ),
    Rule(
        r"instal|klarna|split|розстроч",
        "Yes — orders from €150 can be paid in 3 interest-free instalments with Klarna {cite:Methods}.",
    ),
]
