"""Entry point: python -m app"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import Agent
from app.bot.handlers import ContextMiddleware, router
from app.config import Settings, load_settings
from app.db import init_db, make_engine, make_session_maker
from app.engine import build_agent
from app.rag import Embedder

log = logging.getLogger("assistant")


def build_dispatcher(
    settings: Settings, session_maker: async_sessionmaker[AsyncSession], agent: Agent, storage: BaseStorage | None = None
) -> Dispatcher:
    dp = Dispatcher(storage=storage or MemoryStorage())
    dp.update.outer_middleware(ContextMiddleware(session_maker, settings, agent))
    dp.include_router(router)
    return dp


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    engine = make_engine(settings.database_url)
    await init_db(engine)
    session_maker = make_session_maker(engine)
    embedder = Embedder(cache_dir=str(settings.data_dir / "models"))
    agent = await build_agent(settings, session_maker, embedder)
    log.info("vertical=%s provider=%s chunks=%s", settings.vertical, agent.provider.name, len(agent.index.chunks))

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    await bot.set_my_commands([
        BotCommand(command="start", description="Sample questions"),
        BotCommand(command="help", description="How to use the assistant"),
        BotCommand(command="cancel", description="Stop the current step"),
    ])
    dp = build_dispatcher(settings, session_maker, agent)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
