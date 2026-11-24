"""Prints what a WhatsApp live test did, from the database: transcript, leads, delivery statuses.

Usage: python scripts/whatsapp_report.py [DATABASE_URL]   (default: DATABASE_URL from .env or the SQLite file in data/)
Phone numbers are masked (first 3 and last 2 digits kept), so the output can go into docs/LIVE_TEST_WHATSAPP.md.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_settings  # noqa: E402
from app.db import Lead, Message, User, WhatsAppEvent, WhatsAppOutbound, make_engine, make_session_maker  # noqa: E402
from app.web.api import WA_USER_OFFSET  # noqa: E402


def mask(number: str) -> str:
    digits = number.lstrip("+")
    return "+" + digits[:3] + "*" * max(len(digits) - 5, 1) + digits[-2:]


async def main() -> None:
    settings = load_settings(require_bot_token=False)
    engine = make_engine(sys.argv[1] if len(sys.argv) > 1 else settings.database_url)
    sm = make_session_maker(engine)
    async with sm() as session:
        users = list(await session.scalars(select(User).where(User.id >= WA_USER_OFFSET)))
        for user in users:
            print(f"## WhatsApp user {mask(str(user.id - WA_USER_OFFSET))}\n")
            rows = list(await session.scalars(select(Message).where(Message.user_id == user.id).order_by(Message.id)))
            for m in rows:
                who = "USER" if m.role == "user" else "BOT "
                extra = ""
                if m.role == "assistant":
                    extra = (f"  [{m.input_tokens} in / {m.output_tokens} out tokens; tools {m.meta.get('tools') or '-'}; "
                             f"cards {m.meta.get('cards') or '-'}; sources {m.meta.get('sources') or '-'}; handoff {m.meta.get('handoff', False)}]")
                print(f"{who} {m.created_at:%H:%M:%S}  {m.text}{extra}\n")
            print("### Leads")
            for lead in await session.scalars(select(Lead).where(Lead.user_id == user.id).order_by(Lead.id)):
                print(f"- #{lead.id} {lead.kind} item={lead.item_id or '-'} phone={mask(lead.phone)} note={lead.note!r}")
            print("\n### Outgoing messages and what Meta reported")
            for out in await session.scalars(select(WhatsAppOutbound).where(WhatsAppOutbound.user_id == user.id).order_by(WhatsAppOutbound.updated_at)):
                print(f"- {out.id[:18]}… {out.status}" + (f" (error {out.error_code}: {out.detail})" if out.error_code else ""))
        events = list(await session.scalars(select(WhatsAppEvent).order_by(WhatsAppEvent.created_at)))
        counts: dict[str, int] = {}
        for e in events:
            counts[e.status] = counts.get(e.status, 0) + 1
        print(f"\n### Inbound events by outcome: {counts}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
