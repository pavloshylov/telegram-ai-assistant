"""The assistant loop: retrieve -> ask the model -> run tools -> ask again -> answer with sources and cards."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from app.llm.base import ChatMessage, LLMError, LLMProvider, Usage
from app.rag import Hit, KnowledgeIndex, cited_hits, context_block
from app.verticals.base import Card, ToolContext, Vertical

log = logging.getLogger(__name__)

RULES = """Rules:
- Only help with {domain}. Politely decline anything else in one sentence.
- Facts about policies, fees, documents, delivery, payment or returns must come ONLY from the context below. \
Cite them as [1], [2]. Cite ONLY facts that come from the context: never cite for search results, \
greetings or refusals. If the context does not contain the answer, say you are not sure and call \
handoff_to_human in the same turn.
- Never invent items, prices or availability: to find items always call the search tool. Found items are shown \
to the user as cards under your message, so summarise in one or two sentences instead of listing every detail.
- Off-topic requests (weather, poems, other shops): decline in one sentence and do NOT call handoff_to_human. \
Never say a colleague "has been notified": a person is contacted only after the user shares a phone number. \
Never output JSON or raw tool data; write plain sentences.
- Whenever you offer or promise a human colleague, you MUST call handoff_to_human in that same turn: the bot \
shows the phone-number button only after the tool call. Never just ask for a phone number in text.
- When the user asks about, compares or picks among items already shown (see "Cards shown to the user" in the \
history), call get_* for those ids or repeat the earlier search filters, and name only items and numbers that \
the tool results contain. Never claim an item is the best if a returned item beats it.
- If the user's request is too vague to search (e.g. rent or buy is unknown), ask one short question.
- To book or order, the user taps the button on a card and the bot asks for a phone number. Never ask for payment details.
- Keep answers short (max 4 sentences). Reply in the user's language. No Markdown tables.
Today is {today}."""


@dataclass
class AgentResult:
    text: str
    cards: list[Card] = field(default_factory=list)
    sources: list[Hit] = field(default_factory=list)
    handoff: bool = False
    usage: Usage = field(default_factory=Usage)
    tool_log: list[dict] = field(default_factory=list)
    failed: bool = False


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        vertical: Vertical,
        index: KnowledgeIndex,
        rank_items=None,
        max_steps: int = 4,
        top_k: int = 4,
        min_score: float = 0.30,
        channel_note: str = "",
    ):
        self.provider = provider
        self.vertical = vertical
        self.index = index
        self.rank_items = rank_items
        self.max_steps = max_steps
        self.top_k = top_k
        self.min_score = min_score
        self.channel_note = channel_note  # extra rules for one channel (e.g. WhatsApp already knows the phone number)

    def system_prompt(self, hits: list[Hit]) -> str:
        v = self.vertical
        head = f"You are {v.assistant_name}, the assistant of {v.company}. {v.persona}"
        rules = RULES.format(domain=v.domain, today=date.today().isoformat())
        if self.channel_note:
            rules += f"\n{self.channel_note}"
        return f"{head}\n\n{rules}\n\nContext:\n{context_block(hits)}"

    async def answer(self, session, history: list[ChatMessage], user_text: str) -> AgentResult:
        hits = await asyncio.to_thread(self.index.search, user_text, self.top_k, self.min_score)
        system = self.system_prompt(hits)
        messages = [*history, ChatMessage(role="user", text=user_text)]
        ctx = ToolContext(session=session, rank_items=self.rank_items)
        result = AgentResult(text="")
        try:
            for _ in range(self.max_steps):
                reply = await self.provider.complete(system, messages, self.vertical.tool_specs)
                result.usage += reply.usage
                if not reply.tool_calls:
                    result.text = reply.text
                    break
                messages.append(ChatMessage(role="assistant", text=reply.text, tool_calls=reply.tool_calls))
                for call in reply.tool_calls:
                    output = await self.vertical.run_tool(ctx, call)
                    result.tool_log.append({"tool": call.name, "args": call.args, "result": output.data})
                    result.cards.extend(output.cards)
                    result.handoff = result.handoff or output.handoff
                    messages.append(
                        ChatMessage(role="tool", tool_name=call.name, tool_call_id=call.id, tool_result=output.data)
                    )
            else:
                log.warning("agent hit max_steps=%s", self.max_steps)
                result.handoff = True
        except LLMError:
            log.exception("LLM provider failed")
            result.failed = True
            result.handoff = True
            return result
        result.text = strip_json(result.text)
        if not result.handoff and HANDOFF_PROMISE.search(result.text):
            log.info("reply promises a colleague without calling handoff_to_human: showing the phone button anyway")
            result.handoff = True
        result.cards = _dedupe(result.cards)
        result.sources = cited_hits(result.text, hits)
        return result


def _dedupe(cards: list[Card]) -> list[Card]:
    seen: set[str] = set()
    unique = []
    for card in cards:
        if card.item_id not in seen:
            seen.add(card.item_id)
            unique.append(card)
    return unique


_JSON = json.JSONDecoder()

# A reply like "let me connect you with a colleague" must come with the phone-number button, even when the model
# forgot to call handoff_to_human (seen live with gemini-3.5-flash-lite). English and Ukrainian wording.
HANDOFF_PROMISE = re.compile(
    r"\b(?:connect(?:ing)?|put(?:ting)?|pass(?:ing)?|hand(?:ing)?)\b[^.!?\n]{0,25}\b(?:colleague|human|agent|person|team)\b"
    r"|(?:з.єдн|зв.яж|передам|передаю)\w*[^.!?\n]{0,25}(?:колег|менеджер|агент|людин|фахівц)",
    re.IGNORECASE,
)


def strip_json(text: str) -> str:
    """Models sometimes echo made-up tool data as a JSON block; users must never see it."""
    out, pos = [], 0
    for match in re.finditer(r"{", text):
        if match.start() < pos:
            continue
        try:
            value, end = _JSON.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, (dict, list)) and end - match.start() > 15:
            out.append(text[pos:match.start()])
            pos = end
    out.append(text[pos:])
    cleaned = re.sub(r"```(?:json)?\s*```", "", "".join(out))
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()
