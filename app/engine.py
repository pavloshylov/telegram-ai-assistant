"""Builds the assistant for the configured vertical: catalog, knowledge index, item ranking, provider, agent."""
from __future__ import annotations

import importlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import Agent
from app.config import Settings
from app.db import load_catalog
from app.llm.base import LLMProvider
from app.rag import Embedder, ItemIndex, KnowledgeIndex
from app.verticals.base import Vertical


def get_vertical(key: str) -> Vertical:
    return importlib.import_module(f"app.verticals.{key}").VERTICAL


def make_provider(settings: Settings, vertical: Vertical) -> LLMProvider:
    if settings.llm_provider == "gemini":
        from app.llm.gemini import GeminiProvider

        return GeminiProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            fallback_model=settings.gemini_fallback_model,
            timeout_s=settings.gemini_timeout_s,
        )
    from app.llm.scripted import ScriptedProvider

    rules = importlib.import_module(f"app.verticals.{vertical.key}.demo").RULES
    return ScriptedProvider(rules, vertical.examples)


async def build_agent(
    settings: Settings,
    session_maker: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    provider: LLMProvider | None = None,
) -> Agent:
    vertical = get_vertical(settings.vertical)
    async with session_maker() as session:
        await load_catalog(session, vertical.catalog_model, vertical.catalog_csv)
        await session.commit()
        items = list(await session.scalars(select(vertical.catalog_model)))
    index = KnowledgeIndex.build(vertical.knowledge_dir, embedder, settings.data_dir / f"index_{vertical.key}.npz")
    item_index = ItemIndex([i.id for i in items], [vertical.item_text(i) for i in items], embedder)
    return Agent(
        provider or make_provider(settings, vertical),
        vertical,
        index,
        rank_items=item_index.rank,
        top_k=settings.rag_top_k,
        min_score=settings.rag_min_score,
    )
