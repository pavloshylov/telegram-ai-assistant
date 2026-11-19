from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.bot.handlers import router  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import init_db, make_engine, make_session_maker  # noqa: E402
from app.engine import build_agent  # noqa: E402
from app.main import build_dispatcher  # noqa: E402
from app.rag import Embedder  # noqa: E402
from tests.fake_telegram import FakeTelegram, Harness  # noqa: E402

ADMIN_ID = 100
MODEL_CACHE = Path(__file__).resolve().parent.parent / "data" / "models"


@pytest.fixture(scope="session")
def embedder() -> Embedder:
    """The real local embedding model (downloaded once, ~220 MB), shared by all tests."""
    return Embedder(cache_dir=str(MODEL_CACHE))


def make_settings(tmp_path: Path, vertical: str, **overrides) -> Settings:
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    return Settings(bot_token="42:TEST", vertical=vertical, admin_ids=frozenset({ADMIN_ID}), database_url=url,
                    data_dir=tmp_path, **overrides)


@pytest_asyncio.fixture
async def stack(tmp_path, embedder):
    """Factory: (vertical, provider=None, **settings) -> (agent, session_maker). Cleans up engines afterwards."""
    engines = []

    async def build(vertical: str, provider=None, **overrides):
        settings = make_settings(tmp_path, vertical, **overrides)
        engine = make_engine(settings.database_url)
        if os.environ.get("TEST_DATABASE_URL"):
            from app.db import Base

            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
        await init_db(engine)
        engines.append(engine)
        session_maker = make_session_maker(engine)
        agent = await build_agent(settings, session_maker, embedder, provider)
        return settings, session_maker, agent

    yield build
    for engine in engines:
        await engine.dispose()


@pytest_asyncio.fixture
async def bot(stack):
    """Factory: (vertical, provider=None, **settings) -> Harness driving the real dispatcher."""

    async def build(vertical: str, provider=None, **overrides) -> Harness:
        settings, session_maker, agent = await stack(vertical, provider, **overrides)
        harness = Harness(build_dispatcher(settings, session_maker, agent), FakeTelegram())
        harness.session_maker = session_maker
        harness.agent = agent
        return harness

    yield build
    router._parent_router = None  # module-level router: detach for the next test


@pytest.fixture
def admin():
    return Harness.user(ADMIN_ID, "Pavlo", "owner_demo")
