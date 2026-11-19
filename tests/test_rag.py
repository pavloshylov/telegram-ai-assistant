"""Retrieval quality on the demo knowledge bases (real local embedding model)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.rag import KnowledgeIndex, cited_hits, context_block, load_chunks

ROOT = Path(__file__).resolve().parent.parent / "app" / "verticals"

# question -> section that must be among the top 3 hits
REALTY = {
    "Do tenants pay an agency fee?": "Fees and commission › Rentals",
    "What documents do I need to rent a flat?": "Renting a flat › Documents for a rental application",
    "Can I see the flat over a video call?": "Viewings › Video viewings",
    "Can foreigners get a mortgage?": "Buying a property › Mortgages",
    "When is your office open?": "About Northwind Homes › Office and hours",
    "Are pets allowed?": "Renting a flat › Pets",
    "Які документи потрібні для оренди?": "Renting a flat › Documents for a rental application",  # Ukrainian
}
SHOP = {
    "How long does delivery to Ukraine take?": "Delivery › Options and prices",
    "Can I return earbuds I already opened?": "Returns and warranty › Returns",
    "Can I pay in instalments?": "Payment › Methods",
    "Do you have instalments?": "Payment › Methods",  # rare word: needs the BM25 bonus
    "How long is the warranty?": "Returns and warranty › Warranty",
    "Скільки коштує доставка?": "Delivery › Options and prices",  # Ukrainian
    "Ile kosztuje dostawa?": "Delivery › Options and prices",  # Polish
}
OFF_TOPIC = ["What's the weather tomorrow?", "Who won the football match yesterday?", "Tell me a joke"]


@pytest.fixture(scope="module")
def indexes(embedder, tmp_path_factory):
    cache = tmp_path_factory.mktemp("idx")
    return {
        key: KnowledgeIndex.build(ROOT / key / "knowledge", embedder, cache / f"{key}.npz") for key in ("realty", "shop")
    }


@pytest.mark.parametrize("vertical,question,expected", [("realty", q, s) for q, s in REALTY.items()]
                         + [("shop", q, s) for q, s in SHOP.items()])
def test_relevant_section_in_top3(indexes, vertical, question, expected):
    titles = [h.chunk.title for h in indexes[vertical].search(question, k=3, min_score=0.30)]
    assert expected in titles, titles


@pytest.mark.parametrize("vertical", ["realty", "shop"])
@pytest.mark.parametrize("question", OFF_TOPIC)
def test_off_topic_returns_nothing(indexes, vertical, question):
    assert indexes[vertical].search(question, k=3, min_score=0.30) == []


def test_chunks_follow_headings():
    chunks = load_chunks(ROOT / "shop" / "knowledge")
    titles = {c.title for c in chunks}
    assert "Delivery › Tracking" in titles and all(c.text for c in chunks)


def test_index_cache_is_reused_and_invalidated(embedder, tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text("# Doc\n## One\nFirst text.\n", encoding="utf-8")
    cache = tmp_path / "cache.npz"
    first = KnowledgeIndex.build(kb, embedder, cache)
    stamp = cache.stat().st_mtime_ns
    KnowledgeIndex.build(kb, embedder, cache)
    assert cache.stat().st_mtime_ns == stamp  # unchanged docs: loaded from cache
    (kb / "a.md").write_text("# Doc\n## One\nChanged text.\n## Two\nMore.\n", encoding="utf-8")
    second = KnowledgeIndex.build(kb, embedder, cache)
    assert len(first.chunks) == 1 and len(second.chunks) == 2


def test_citations_map_to_sources(indexes):
    hits = indexes["shop"].search("delivery to Ukraine and tracking", k=4)
    text = "It takes 5–8 days [1]. Tracking link by email [2]. Again [1]. Bogus [9]."
    assert [h.chunk.title for h in cited_hits(text, hits)] == [hits[0].chunk.title, hits[1].chunk.title]
    assert context_block(hits).startswith("[1] ")
    assert context_block([]) == "(no relevant documents found)"
