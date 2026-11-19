"""Telegram layer (aiogram 3): chat with the agent, item cards, lead capture, admin report."""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message as TgMessage,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import Agent, AgentResult
from app.bot import texts
from app.config import Settings
from app.db import Lead, Message, User, utcnow
from app.llm.base import ChatMessage
from app.verticals.base import Card

log = logging.getLogger(__name__)
router = Router(name="assistant")
PHONE_RE = re.compile(r"^\+?[\d\s\-()]{7,20}$")


class LeadFlow(StatesGroup):
    phone = State()


# ---------------------------------------------------------------- middleware

class ContextMiddleware(BaseMiddleware):
    """One DB session per update, the current user upserted, settings and agent injected."""

    def __init__(self, session_maker: async_sessionmaker[AsyncSession], settings: Settings, agent: Agent):
        self.session_maker = session_maker
        self.settings = settings
        self.agent = agent

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict
    ) -> Any:
        tg_user = data.get("event_from_user")
        async with self.session_maker() as session:
            data.update(session=session, settings=self.settings, agent=self.agent)
            if tg_user is not None and not tg_user.is_bot:
                user = await session.get(User, tg_user.id)
                if user is None:
                    user = User(id=tg_user.id)
                    session.add(user)
                user.username, user.first_name, user.language = tg_user.username, tg_user.full_name, tg_user.language_code
                user.is_blocked = False
                await session.flush()
                data["user"] = user
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


# ---------------------------------------------------------------- keyboards and rendering

def main_keyboard(agent: Agent) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=agent.vertical.human_button)]], resize_keyboard=True, is_persistent=True
    )


def phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=texts.SHARE_PHONE, request_contact=True)], [KeyboardButton(text=texts.CANCEL)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def card_markup(card: Card) -> InlineKeyboardMarkup | None:
    if not card.buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=b.text, callback_data=b.action) for b in card.buttons]]
    )


def card_text(card: Card) -> str:
    return f"<b>{escape(card.title)}</b>\n" + "\n".join(card.lines)  # lines are built by our code, already safe


def reply_html(result: AgentResult) -> str:
    """Model text is untrusted: escape it, then allow **bold** only."""
    text = escape(result.text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    if result.sources:
        items = "; ".join(f"[{n}] {escape(h.chunk.title)}" for n, h in _numbered(result))
        text += "\n\n" + texts.SOURCES.format(items=items)
    return text


def _numbered(result: AgentResult):
    numbers = [int(m) for m in re.findall(r"\[(\d{1,2})\]", result.text)]
    unique = list(dict.fromkeys(numbers))
    return zip(unique, result.sources)


# ---------------------------------------------------------------- start / help / cancel

@router.message(CommandStart())
async def start(message: TgMessage, state: FSMContext, agent: Agent) -> None:
    await state.clear()
    await message.answer(agent.vertical.welcome, reply_markup=main_keyboard(agent))
    buttons = [[InlineKeyboardButton(text=e, callback_data=f"ex:{n}")] for n, e in enumerate(agent.vertical.examples)]
    await message.answer(texts.EXAMPLES_TITLE, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.message(Command("help"))
async def help_cmd(message: TgMessage) -> None:
    await message.answer(texts.HELP)


@router.message(Command("cancel"))
@router.message(LeadFlow.phone, F.text == texts.CANCEL)
async def cancel(message: TgMessage, state: FSMContext, agent: Agent) -> None:
    if await state.get_state() is None:
        await message.answer(texts.NOTHING_TO_CANCEL)
        return
    await state.clear()
    await message.answer(texts.CANCELLED, reply_markup=main_keyboard(agent))


# ---------------------------------------------------------------- admin

@router.message(Command("admin"))
async def admin(message: TgMessage, session: AsyncSession, settings: Settings, agent: Agent, user: User) -> None:
    if not settings.is_admin(user.id):
        await ask(message, session, settings, agent, user, message.text or "")
        return
    count = lambda q: session.scalar(q)  # noqa: E731
    questions = await count(select(func.count()).select_from(Message).where(Message.role == "user"))
    assistant_meta = list(await session.scalars(select(Message.meta).where(Message.role == "assistant")))
    tokens_in = await count(select(func.coalesce(func.sum(Message.input_tokens), 0)))
    tokens_out = await count(select(func.coalesce(func.sum(Message.output_tokens), 0)))
    kinds = (await session.execute(select(Lead.kind, func.count()).group_by(Lead.kind))).all()
    cost = tokens_in / 1e6 * settings.price_input_per_m + tokens_out / 1e6 * settings.price_output_per_m
    await message.answer(
        texts.ADMIN_STATS.format(
            company=escape(agent.vertical.company),
            provider=f"{agent.provider.name}" + (f" ({settings.gemini_model})" if agent.provider.name == "gemini" else ""),
            users=await count(select(func.count()).select_from(User)),
            conversations=await count(select(func.count(func.distinct(Message.user_id)))),
            questions=questions,
            with_sources=sum(1 for m in assistant_meta if m.get("sources")),
            with_cards=sum(1 for m in assistant_meta if m.get("cards")),
            handoffs=sum(1 for m in assistant_meta if m.get("handoff")),
            leads=sum(n for _, n in kinds),
            lead_kinds=", ".join(f"{k}: {n}" for k, n in kinds) or "none yet",
            estimated=" (estimated, demo mode)" if agent.provider.name == "demo" else "",
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=cost,
            per_question=cost / questions if questions else 0,
            p_in=settings.price_input_per_m,
            p_out=settings.price_output_per_m,
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="📥 Export leads (CSV)", callback_data="admin:export")]]
        ),
    )


@router.callback_query(F.data == "admin:export")
async def export_leads(cb: CallbackQuery, session: AsyncSession, settings: Settings, user: User) -> None:
    if not settings.is_admin(user.id):
        await cb.answer()
        return
    rows = (await session.execute(select(Lead, User).join(User, User.id == Lead.user_id).order_by(Lead.id))).all()
    if not rows:
        await cb.answer(texts.EXPORT_EMPTY, show_alert=True)
        return
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["lead_id", "created_at_utc", "kind", "item_id", "phone", "name", "telegram", "note", "status"])
    for lead, u in rows:
        writer.writerow([lead.id, lead.created_at.strftime("%Y-%m-%d %H:%M"), lead.kind, lead.item_id or "",
                         lead.phone, lead.name, f"@{u.username}" if u.username else u.id, lead.note, lead.status])
    data = ("﻿" + buf.getvalue()).encode("utf-8")
    await cb.message.answer_document(BufferedInputFile(data, filename=f"leads_{utcnow():%Y-%m-%d}.csv"))
    await cb.answer()


# ---------------------------------------------------------------- lead capture

async def start_lead(target: TgMessage, state: FSMContext, kind: str, item_id: str | None, prompt: str, note: str = "") -> None:
    await state.set_state(LeadFlow.phone)
    await state.update_data(kind=kind, item_id=item_id, note=note)
    await target.answer(prompt, reply_markup=phone_keyboard())


@router.message(F.text.startswith("👤 "))
async def human_button(message: TgMessage, state: FSMContext, agent: Agent) -> None:
    if message.text != agent.vertical.human_button:
        return
    await start_lead(message, state, "human", None, texts.ASK_PHONE_HUMAN, note="asked for a person")


@router.callback_query(F.data.regexp(r"^(book|order):[A-Z]{2}-\d+$"))
async def card_action(cb: CallbackQuery, state: FSMContext, session: AsyncSession, agent: Agent) -> None:
    prefix, item_id = cb.data.split(":", 1)
    action = agent.vertical.lead_actions.get(prefix)
    item = await session.get(agent.vertical.catalog_model, item_id)
    if action is None or item is None:
        await cb.answer(texts.ITEM_GONE, show_alert=True)
        return
    await cb.answer()
    await start_lead(cb.message, state, action.kind, item_id, action.prompt, note=item.title)


@router.message(LeadFlow.phone, F.contact | F.text)
async def got_phone(
    message: TgMessage, state: FSMContext, session: AsyncSession, settings: Settings, agent: Agent, user: User, bot: Bot
) -> None:
    phone = message.contact.phone_number if message.contact else (message.text or "").strip()
    if not message.contact and not PHONE_RE.match(phone):
        await message.answer(texts.BAD_PHONE)
        return
    data = await state.get_data()
    await state.clear()
    lead = Lead(user_id=user.id, kind=data["kind"], item_id=data.get("item_id"), phone=phone,
                name=(message.contact.first_name if message.contact else user.first_name) or "", note=data.get("note", ""))
    session.add(lead)
    await session.flush()
    await message.answer(texts.LEAD_THANKS.format(phone=escape(phone)), reply_markup=main_keyboard(agent))
    await notify_admins(bot, session, settings, lead, user)


async def notify_admins(bot: Bot, session: AsyncSession, settings: Settings, lead: Lead, user: User) -> None:
    recent = list(await session.scalars(
        select(Message).where(Message.user_id == user.id).order_by(Message.id.desc()).limit(6)
    ))
    transcript = "\n".join(
        f"{'👤' if m.role == 'user' else '🤖'} {escape(m.text[:200])}" for m in reversed(recent)
    ) or "—"
    who = f"{escape(user.first_name)} (@{escape(user.username)})" if user.username else escape(user.first_name)
    text = texts.LEAD_NOTIFY.format(
        kind=lead.kind, id=lead.id, user=who, phone=escape(lead.phone),
        item=f"\nItem: {escape(lead.item_id)} · {escape(lead.note)}" if lead.item_id else "",
        note=f"\nNote: {escape(lead.note)}" if lead.note and not lead.item_id else "",
        transcript=transcript,
    )
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except Exception:  # noqa: BLE001 — a lead is saved even if a notification fails
            log.warning("could not notify admin %s about lead %s", admin_id, lead.id, exc_info=True)


@router.message(LeadFlow.phone)
async def phone_expected(message: TgMessage) -> None:
    await message.answer(texts.BAD_PHONE)


# ---------------------------------------------------------------- questions

@router.callback_query(F.data.startswith("ex:"))
async def example(cb: CallbackQuery, session: AsyncSession, settings: Settings, agent: Agent, user: User, state: FSMContext) -> None:
    index = int(cb.data.split(":")[1]) if cb.data.split(":")[1].isdigit() else -1
    await cb.answer()
    if 0 <= index < len(agent.vertical.examples):
        question = agent.vertical.examples[index]
        await cb.message.answer(f"💬 <i>{escape(question)}</i>")
        await ask(cb.message, session, settings, agent, user, question, state)


@router.message(F.text)
async def text_message(message: TgMessage, session: AsyncSession, settings: Settings, agent: Agent, user: User, state: FSMContext) -> None:
    await ask(message, session, settings, agent, user, message.text, state)


@router.message()
async def other_message(message: TgMessage) -> None:
    await message.answer(texts.TEXT_ONLY)


async def ask(
    target: TgMessage, session: AsyncSession, settings: Settings, agent: Agent, user: User, question: str,
    state: FSMContext | None = None,
) -> None:
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = await session.scalar(
        select(func.count()).select_from(Message).where(
            Message.user_id == user.id, Message.role == "user", Message.created_at >= hour_ago
        )
    )
    if recent >= settings.rate_limit_per_hour:
        await target.answer(texts.RATE_LIMITED)
        return
    await target.bot.send_chat_action(target.chat.id, ChatAction.TYPING)
    history = await load_history(session, user.id, settings.history_messages)
    session.add(Message(user_id=user.id, role="user", text=question[:4000]))
    result = await agent.answer(session, history, question)
    if result.failed:
        session.add(Message(user_id=user.id, role="assistant", text=texts.THINKING_FAILED, meta={"handoff": True, "failed": True}))
        if state is not None:
            await start_lead(target, state, "human", None, texts.THINKING_FAILED, note="assistant failed to answer")
        return
    session.add(Message(
        user_id=user.id, role="assistant", text=result.text,
        meta={"cards": [c.item_id for c in result.cards], "sources": [h.chunk.title for h in result.sources],
              "handoff": result.handoff, "tools": [t["tool"] for t in result.tool_log]},
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens,
    ))
    handoff = result.handoff and state is not None
    if handoff:  # one message: the model's words + the phone button (no duplicate prompt)
        await state.set_state(LeadFlow.phone)
        await state.update_data(kind="human", item_id=None, note="assistant hand-off")
    if result.text or handoff:
        text = reply_html(result) if result.text else texts.ASK_PHONE_HUMAN
        if handoff and result.text and "phone" not in result.text.lower():
            text += "\n\n" + texts.PHONE_HINT
        await target.answer(text, reply_markup=phone_keyboard() if handoff else None)
    for card in result.cards:
        await target.answer(card_text(card), reply_markup=card_markup(card))


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


@router.error()
async def on_error(event: ErrorEvent) -> bool:
    log.exception("update %s failed", event.update.update_id, exc_info=event.exception)
    try:
        if event.update.callback_query:
            await event.update.callback_query.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
        elif event.update.message:
            await event.update.message.answer("⚠️ Something went wrong on our side. Please try again.")
    except Exception:  # noqa: BLE001
        log.warning("could not report the error", exc_info=True)
    return True
