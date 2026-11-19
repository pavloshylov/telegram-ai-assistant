"""Entry point: python -m app.web   (website chat widget API + demo site)

Environment: same .env as the bot (VERTICAL, LLM_PROVIDER, GEMINI_API_KEY, DATABASE_URL, ...) plus
WIDGET_ALLOWED_ORIGINS, ADMIN_API_KEY, PORT/HOST. BOT_TOKEN is optional here: with BOT_TOKEN + ADMIN_IDS new leads
are also pushed to the admins' Telegram.
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
from app.web.api import create_app
from app.web.notify import make_notifier

log = logging.getLogger("assistant.web")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(require_bot_token=False)
    engine = make_engine(settings.database_url)
    await init_db(engine)
    session_maker = make_session_maker(engine)
    embedder = Embedder(cache_dir=str(settings.data_dir / "models"))
    agent = await build_agent(settings, session_maker, embedder)
    app = create_app(settings, session_maker, agent, make_notifier(settings))
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    log.info("vertical=%s provider=%s sections=%s -> http://%s:%s/", settings.vertical, agent.provider.name,
             len(agent.index.chunks), host, port)
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
    try:
        await server.serve()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
