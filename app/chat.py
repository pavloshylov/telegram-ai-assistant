"""Channel-neutral conversation service: used by the Telegram bot and by the website widget API.

One place for the rules that must not differ between channels: rate limit, history, conversation log with token
usage, leads and the usage report. Rendering (Telegram HTML, JSON for the widget) stays in each channel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import Agent, AgentResult
from app.bot import texts
from app.config import Settings
from app.db import Lead, Message, User
from app.llm.base import ChatMessage


PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")


@dataclass
class Turn:
    status: Literal["ok", "rate_limited", "failed"]
    result: AgentResult | None = None


async def converse(session: AsyncSession, settings: Settings, agent: Agent, user_id: int, question: str) -> Turn:
    """One user question: rate limit → history → agent → conversation log."""
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = await session.scalar(
        select(func.count()).select_from(Message).where(
            Message.user_id == user_id, Message.role == "user", Message.created_at >= hour_ago
        )
    )
    if recent >= settings.rate_limit_per_hour:
        return Turn("rate_limited")
    history = await load_history(session, user_id, settings.history_messages)
    session.add(Message(user_id=user_id, role="user", text=question[:4000]))
    await session.commit()  # release database locks while the model thinks (can take seconds on a busy API)
    result = await agent.answer(session, history, question)
    if result.failed:
        session.add(Message(user_id=user_id, role="assistant", text=texts.THINKING_FAILED, meta={"handoff": True, "failed": True}))
        return Turn("failed", result)
    session.add(Message(
        user_id=user_id, role="assistant", text=result.text,
        meta={"cards": [c.item_id for c in result.cards], "sources": [h.chunk.title for h in result.sources],
              "handoff": result.handoff, "tools": [t["tool"] for t in result.tool_log]},
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
    ))
    return Turn("ok", result)


async def load_history(session: AsyncSession, user_id: int, limit: int) -> list[ChatMessage]:
    """Recent turns for the model; shown cards are noted so follow-ups like 'the second one' work."""
    rows = list(await session.scalars(
        select(Message).where(Message.user_id == user_id).order_by(Message.id.desc()).limit(limit)
    ))
    history = []
    for row in reversed(rows):
        text = row.text
        if row.role == "assistant" and row.meta.get("cards"):
            text += f"\n(Cards shown to the user: {', '.join(row.meta['cards'])})"
        history.append(ChatMessage(role="user" if row.role == "user" else "assistant", text=text))
    while history and history[0].role != "user":  # providers expect the history to start with a user turn
        history.pop(0)
    return history


async def create_lead(
    session: AsyncSession, user_id: int, kind: str, item_id: str | None, phone: str, name: str, note: str
) -> Lead:
    lead = Lead(user_id=user_id, kind=kind, item_id=item_id, phone=phone, name=name, note=note)
    session.add(lead)
    await session.flush()
    return lead


async def recent_messages(session: AsyncSession, user_id: int, limit: int = 6) -> list[Message]:
    """Last messages of a user, oldest first (the transcript attached to a lead)."""
    rows = list(await session.scalars(
        select(Message).where(Message.user_id == user_id).order_by(Message.id.desc()).limit(limit)
    ))
    return list(reversed(rows))


@dataclass
class Stats:
    users: int
    conversations: int
    questions: int
    with_sources: int
    with_cards: int
    handoffs: int
    leads: int
    lead_kinds: dict[str, int]
    tokens_in: int
    tokens_out: int
    cost: float
    per_question: float


async def collect_stats(session: AsyncSession, settings: Settings) -> Stats:
    count = lambda q: session.scalar(q)  # noqa: E731
    questions = await count(select(func.count()).select_from(Message).where(Message.role == "user"))
    assistant_meta = list(await session.scalars(select(Message.meta).where(Message.role == "assistant")))
    tokens_in = await count(select(func.coalesce(func.sum(Message.input_tokens), 0)))
    tokens_out = await count(select(func.coalesce(func.sum(Message.output_tokens), 0)))
    kinds = dict((await session.execute(select(Lead.kind, func.count()).group_by(Lead.kind))).all())
    cost = tokens_in / 1e6 * settings.price_input_per_m + tokens_out / 1e6 * settings.price_output_per_m
    return Stats(
        users=await count(select(func.count()).select_from(User)),
        conversations=await count(select(func.count(func.distinct(Message.user_id)))),
        questions=questions,
        with_sources=sum(1 for m in assistant_meta if m.get("sources")),
        with_cards=sum(1 for m in assistant_meta if m.get("cards")),
        handoffs=sum(1 for m in assistant_meta if m.get("handoff")),
        leads=sum(kinds.values()),
        lead_kinds=kinds,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost=cost,
        per_question=cost / questions if questions else 0,
    )
