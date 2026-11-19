"""Retrieval for RAG: Markdown knowledge base -> section chunks -> hybrid search.

Hybrid = semantic + keyword:
- embeddings run locally with fastembed (ONNX, multilingual MiniLM, 384 dims): no API key, no per-query cost,
  and a question in Ukrainian or Polish still finds an English section;
- BM25 adds a capped bonus for exact rare words the small embedding model under-weights ("Klarna", "PayPal").
Vectors are cached on disk and rebuilt only when the documents or the model change.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from rank_bm25 import BM25Okapi

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
KEYWORD_WEIGHT = 0.08  # score = cosine + 0.08 * min(bm25, 5)  ->  keyword bonus is at most 0.4
KEYWORD_CAP = 5.0
STOPWORDS = set(
    "a an the and or but if of to in on at by for with from about as is are was were be been being do does did "
    "have has had i you he she it we they me my your our their this that these those what which who whom how "
    "when where why can could should would will shall may might must not no yes there here s t d ll re ve m "
    "please tell know want need get any some much many more most so than too very just also".split()
)


def tokenize(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if w not in STOPWORDS]


@dataclass(frozen=True)
class Chunk:
    id: int
    source: str  # file name, e.g. "fees.md"
    title: str  # "Fees and commission › Rentals"
    text: str


@dataclass(frozen=True)
class Hit:
    chunk: Chunk
    score: float


class Embedder:
    """Thin wrapper so tests can swap in a fake; the real model loads lazily on first use."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL, cache_dir: str | None = None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model = None

    def embed(self, texts: list[str]) -> np.ndarray:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(self.model_name, cache_dir=self.cache_dir)
        vectors = np.array(list(self._model.embed(texts)), dtype=np.float32)
        return normalize(vectors)


def normalize(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


def load_chunks(knowledge_dir: Path) -> list[Chunk]:
    """One chunk per '## ' section; the document '# ' title is kept as context in the chunk title."""
    chunks: list[Chunk] = []
    for path in sorted(knowledge_dir.glob("*.md")):
        doc_title, section, lines = path.stem, None, []

        def flush() -> None:
            body = "\n".join(lines).strip()
            if body:
                title = f"{doc_title} › {section}" if section else doc_title
                chunks.append(Chunk(id=len(chunks), source=path.name, title=title, text=body))

        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                doc_title = line[2:].strip()
            elif line.startswith("## "):
                flush()
                section, lines = line[3:].strip(), []
            else:
                lines.append(line)
        flush()
    return chunks


class KnowledgeIndex:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray, embedder: Embedder):
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.bm25 = BM25Okapi([tokenize(f"{c.title} {c.text}") for c in chunks]) if chunks else None

    @classmethod
    def build(cls, knowledge_dir: Path, embedder: Embedder, cache_path: Path | None = None) -> "KnowledgeIndex":
        chunks = load_chunks(knowledge_dir)
        texts = [f"{c.title}\n{c.text}" for c in chunks]
        fingerprint = hashlib.sha256(json.dumps([embedder.model_name, texts]).encode()).hexdigest()
        if cache_path and cache_path.is_file():
            cached = np.load(cache_path, allow_pickle=False)
            if str(cached["fingerprint"]) == fingerprint:
                return cls(chunks, cached["vectors"], embedder)
        vectors = embedder.embed(texts) if texts else np.zeros((0, 1), dtype=np.float32)
        if cache_path:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez(cache_path, vectors=vectors, fingerprint=np.array(fingerprint))
        return cls(chunks, vectors, embedder)

    def search(self, query: str, k: int = 4, min_score: float = 0.35) -> list[Hit]:
        if not self.chunks or not query.strip():
            return []
        query_vector = self.embedder.embed([query])[0]
        scores = self.vectors @ query_vector
        terms = tokenize(query)
        if terms and self.bm25 is not None:
            scores = scores + KEYWORD_WEIGHT * np.minimum(self.bm25.get_scores(terms), KEYWORD_CAP)
        order = np.argsort(-scores)[:k]
        return [Hit(self.chunks[i], float(scores[i])) for i in order if scores[i] >= min_score]


class ItemIndex:
    """Semantic ranking of catalog items (after SQL filters): 'for running' finds sport earbuds."""

    def __init__(self, ids: list[str], texts: list[str], embedder: Embedder):
        self.ids = ids
        self.embedder = embedder
        self.vectors = embedder.embed(texts) if texts else np.zeros((0, 1), dtype=np.float32)
        self.position = {item_id: n for n, item_id in enumerate(ids)}

    def rank(self, query: str, candidate_ids: list[str]) -> list[str]:
        known = [i for i in candidate_ids if i in self.position]
        if not known or not query.strip():
            return candidate_ids
        query_vector = self.embedder.embed([query])[0]
        scores = {i: float(self.vectors[self.position[i]] @ query_vector) for i in known}
        return sorted(known, key=lambda i: -scores[i]) + [i for i in candidate_ids if i not in self.position]


def context_block(hits: list[Hit]) -> str:
    """Numbered context for the system prompt; the model cites it as [1], [2]."""
    if not hits:
        return "(no relevant documents found)"
    return "\n\n".join(f"[{n}] {hit.chunk.title}\n{hit.chunk.text}" for n, hit in enumerate(hits, start=1))


CITATION_RE = re.compile(r"\[(\d{1,2})\]")


def cited_hits(text: str, hits: list[Hit]) -> list[Hit]:
    """Sources the answer actually cites, in order of first mention."""
    seen: list[int] = []
    for match in CITATION_RE.finditer(text):
        n = int(match.group(1))
        if 1 <= n <= len(hits) and n not in seen:
            seen.append(n)
    return [hits[n - 1] for n in seen]
