"""Turns engine output (AgentResult, Card) into WhatsApp messages. Pure functions, no network.

WhatsApp formatting: *bold*, _italic_. The engine writes **bold** (model) and <b> (our cards), so both are converted.
"""
from __future__ import annotations

import re
from html import unescape

from app.agent import AgentResult
from app.verticals.base import Card, Vertical
from app.web.api import sources_out
from app.whatsapp.graph import BUTTONS_MAX, BUTTON_BODY_MAX, TEXT_MAX, Row, clamp

CHANNEL_NOTE = (
    "Style for this chat (it overrides the length rule above where they differ): write like a friendly, competent "
    "person texting a customer on WhatsApp, not like a bot or a brochure. Short, natural sentences in the user's own "
    "language and register (informal if they are informal). Start with the answer itself, never with filler such as "
    "'Great question' or 'I'd be happy to help'. Never say 'As an AI' or 'virtual assistant'. Use at most one emoji, "
    "only where a person would. Use the user's first name at most once, and only if it is known. Keep it to 1-3 short "
    "sentences, and no bullet lists unless the user asks for a list. Where it fits, end with one light, specific "
    "question or next step that moves the conversation on (a preference, a budget, a date), never a generic 'anything "
    "else?'. Keep citing facts as [1], [2] exactly as the rules say (the channel removes the markers before sending), "
    "and keep every fact strictly from the context. "
    "If the user sincerely asks whether you are a bot or a person, answer honestly that you are the company's AI "
    "assistant and offer a colleague. "
    "Channel facts: WhatsApp. The user's phone number is already known from WhatsApp: never ask for it and never mention a "
    "phone-number button. When you offer a human colleague, still call handoff_to_human, and say that a colleague will "
    "contact them on this WhatsApp number. To book or order, the user taps the button under the item card."
)
HUMAN_ID = "human"
EXAMPLE_PREFIX = "ex:"

TEXT_ONLY = "I can only read text messages here 🙂 Could you type it out for me?"
FAILED_INTRO = "I can't answer this one right now, so I've passed it to a colleague who'll pick it up."
ERROR = "Sorry, something broke on our side. Could you send that again in a minute?"
RATE_LIMITED = "You've sent quite a few messages this hour, so I need a short break. Try again a bit later, or tap \"Talk to a person\"."
LEAD_THANKS = "✅ Got it, thank you! A colleague will message you on this number (+{phone}) within 2 working hours."
ITEM_GONE = "That one is no longer available, sorry. Want me to look for something similar?"
HUMAN_BUTTON = "👤 Talk to a person"
MENU_WORDS = {"hi", "hello", "hey", "start", "menu", "help", "/start", "/help"}


def to_whatsapp(value: str) -> str:
    """Model/card text -> WhatsApp markup. Anything that is not bold is plain text."""
    value = re.sub(r"</?(b|strong)>", "*", value)
    value = re.sub(r"</?(i|em)>", "_", value)
    value = re.sub(r"<[^>]+>", "", value)
    value = re.sub(r"\*\*(.+?)\*\*", r"*\1*", unescape(value))
    return value.strip()


def split_text(text: str, limit: int = TEXT_MAX) -> list[str]:
    """Chunks of at most `limit` characters, cut at paragraph, line or word boundaries when possible."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = max(text.rfind("\n\n", 0, limit), text.rfind("\n", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


CITE_MARK = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\]")


def reply_chunks(result: AgentResult, show_sources: bool = False) -> list[str]:
    """The model's answer. By default it reads like a chat message: the [n] markers are removed and there is no
    'Sources' line (the cited sections stay in the conversation log). `show_sources` keeps both."""
    text = to_whatsapp(result.text)
    if show_sources:
        cited = sources_out(result)
        if cited and text:
            text += "\n\n📚 _Sources: " + "; ".join(f"[{s.n}] {s.title}" for s in cited) + "_"
    else:
        text = re.sub(r" +([.,;:!?])", r"\1", CITE_MARK.sub("", text))
    return split_text(text)


def card_message(card: Card, human_button: str = HUMAN_BUTTON) -> tuple[str, list[tuple[str, str]]]:
    """Body text and reply buttons of an item card: its own actions (max 2) + 'Talk to a person'."""
    body = f"*{card.title}*\n" + "\n".join(to_whatsapp(line) for line in card.lines)
    buttons = [(b.action, b.text) for b in card.buttons[: BUTTONS_MAX - 1]]
    buttons.append((HUMAN_ID, clamp(human_button, 20)))
    return clamp(body, BUTTON_BODY_MAX), buttons


def menu_message(vertical: Vertical) -> tuple[str, str, list[Row]]:
    """Welcome text, list button label and rows: sample questions + the hand-off row (list limit: 10 rows)."""
    rows = [Row(f"{EXAMPLE_PREFIX}{n}", clamp(e, 24), clamp(e, 72) if len(e) > 24 else "") for n, e in enumerate(vertical.examples[:9])]
    rows.append(Row(HUMAN_ID, clamp(vertical.human_button, 24)))
    return to_whatsapp(vertical.welcome), "Try a question", rows
