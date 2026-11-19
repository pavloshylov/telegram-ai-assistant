"""Scripted demo replies (offline mode). Wording only; search, retrieval and citations are computed live."""
from app.llm.scripted import Rule

RULES = [
    Rule(
        r"weather|pizza|football|joke|bitcoin|recipe",
        "I can only help with flats at Northwind Homes — renting, buying and viewings in Lisbon. "
        "Is there a flat I can help you find?",
    ),
    Rule(
        r"\b(agent|human|person|manager|call me)\b|з людиною|менеджер",
        "Of course — I'll connect you with an agent. Please share your phone number with the button below.",
        tool="handoff_to_human",
        args={"reason": "user asked for a human agent"},
    ),
    Rule(
        r"двокімнатн|2-кімнатн|дві спальні",
        "Є варіант у Шіаду, де можна з собакою: світла квартира з двома спальнями, балконом і ліфтом, "
        "€2 300 на місяць. Натисніть «Book a viewing», і агент підтвердить час перегляду.",
        tool="search_listings",
        args={"deal": "rent", "district": "Chiado", "min_bedrooms": 2, "max_bedrooms": 2, "max_price": 2400, "pets": True},
    ),
    Rule(
        r"(2|two)[- ]?bed.*chiado|chiado.*(2|two)[- ]?bed",
        "Good news: there's a pet-friendly 2-bedroom in Chiado within your budget — bright, with a balcony "
        "and a lift, at €2,300 a month. Tap “Book a viewing” and an agent will confirm a time.",
        tool="search_listings",
        args={"deal": "rent", "district": "Chiado", "min_bedrooms": 2, "max_bedrooms": 2, "max_price": 2400, "pets": True},
    ),
    Rule(
        r"\bbuy\b.*campo de ourique|campo de ourique.*\bbuy\b",
        "I found a 2-bedroom for sale in Campo de Ourique within €650,000: a bright corner flat with a balcony "
        "and a lift at €620,000. Shall I book a viewing?",
        tool="search_listings",
        args={"deal": "sale", "district": "Campo de Ourique", "min_bedrooms": 2, "max_bedrooms": 2, "max_price": 650000},
    ),
    Rule(
        r"agency fee|commission|\bfees?\b",
        "No — at Northwind Homes tenants pay no agency fee; the landlord pays our commission {cite:Rentals}. "
        "Before moving in you usually pay the first month's rent plus a deposit of two months' rent {cite:Rentals}.",
    ),
    Rule(
        r"documents?|paperwork|документ",
        "You'll need a passport or EU ID, a Portuguese tax number (NIF) and proof of income — for example your last "
        "3 payslips {cite:Documents for a rental application}. No NIF yet? We can help you get one in about a week "
        "{cite:Documents for a rental application}.",
    ),
    Rule(
        r"video|online viewing|not in lisbon|remote viewing",
        "Yes — if you're not in Lisbon yet, an agent can show the flat on a live video call over WhatsApp or Google "
        "Meet, free of charge {cite:Video viewings}.",
    ),
]
