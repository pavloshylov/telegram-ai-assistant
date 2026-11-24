"""Entry point: python -m app.whatsapp   (WhatsApp Cloud API webhook server)

Environment: the same .env as the bot (VERTICAL, LLM_PROVIDER, GEMINI_API_KEY, DATABASE_URL, ...) plus
WHATSAPP_TOKEN, WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_VERIFY_TOKEN, WHATSAPP_APP_SECRET and PORT/HOST (default 127.0.0.1:8001).
BOT_TOKEN + ADMIN_IDS are optional: with them new leads are also pushed to the admins' Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from app.config import load_settings
from app.db import init_db, make_engine, make_session_maker
from app.engine import build_agent
from app.rag import Embedder
from app.web.notify import make_notifier
from app.whatsapp.api import create_app

log = logging.getLogger("assistant.whatsapp")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs full request URLs
    settings = load_settings(require_bot_token=False)
    missing = [n for n, v in [("WHATSAPP_TOKEN", settings.whatsapp_token), ("WHATSAPP_PHONE_NUMBER_ID", settings.whatsapp_phone_number_id),
                              ("WHATSAPP_VERIFY_TOKEN", settings.whatsapp_verify_token), ("WHATSAPP_APP_SECRET", settings.whatsapp_app_secret)] if not v]
    if missing:
        raise SystemExit(f"missing in .env: {', '.join(missing)}")
    engine = make_engine(settings.database_url)
    await init_db(engine)
    session_maker = make_session_maker(engine)
    agent = await build_agent(settings, session_maker, Embedder(cache_dir=str(settings.data_dir / "models")))
    app = create_app(settings, session_maker, agent, notifier=make_notifier(settings, channel="WhatsApp"))
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8001"))
    log.info("vertical=%s provider=%s sections=%s -> webhook http://%s:%s/webhook", settings.vertical, agent.provider.name,
             len(agent.index.chunks), host, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    try:
        await server.serve()
    finally:
        await app.state.channel.drain()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
