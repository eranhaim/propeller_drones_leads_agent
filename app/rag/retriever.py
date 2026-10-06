"""Retriever wrapper around the Chroma vector store."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional

from langchain_core.documents import Document
from loguru import logger

from app.rag.store import get_vectorstore


@dataclass(frozen=True)
class Snippet:
    """A single retrieved knowledge snippet."""

    content: str
    source: str
    topic: str

    def format(self) -> str:
        header = f"[Source: {self.source} | Topic: {self.topic}]"
        return f"{header}\n{self.content}".strip()


@dataclass(frozen=True)
class VideoMatch:
    """A semantically retrieved approved video transcript."""

    video_id: str
    relevance: float


@lru_cache(maxsize=1)
def _retriever(k: int = 5, fetch_k: int = 20, lambda_mult: float = 0.5):
    """Cached MMR retriever."""
    vs = get_vectorstore()
    return vs.as_retriever(
        search_type="mmr",
        search_kwargs={"k": k, "fetch_k": fetch_k, "lambda_mult": lambda_mult},
    )


def _to_snippets(docs: List[Document]) -> List[Snippet]:
    out: List[Snippet] = []
    for d in docs:
        meta = d.metadata or {}
        out.append(
            Snippet(
                content=d.page_content.strip(),
                source=str(meta.get("source", "unknown")),
                topic=str(meta.get("topic", "general")),
            )
        )
    return out


def search(
    query: str,
    k: int = 5,
    topic: Optional[str] = None,
) -> List[Snippet]:
    """Search the knowledge base. Optionally filter by ``topic`` metadata."""
    if not query.strip():
        return []

    if topic:
        vs = get_vectorstore()
        docs = vs.similarity_search(query, k=k, filter={"topic": topic})
    else:
        docs = _retriever(k=k).invoke(query)

    logger.debug("RAG search '{}' -> {} docs (topic={})", query, len(docs), topic)
    return _to_snippets(docs)


def search_as_text(query: str, k: int = 5, topic: Optional[str] = None) -> str:
    """Return the RAG search result as one text block suitable for LLM tools."""
    snippets = search(query, k=k, topic=topic)
    if not snippets:
        return "לא נמצא מידע רלוונטי במאגר הידע."
    return "\n\n---\n\n".join(s.format() for s in snippets)


def search_video_catalog(query: str, k: int = 3) -> List[VideoMatch]:
    """Semantically rank approved video transcripts in the shared Chroma RAG."""
    if not query.strip():
        return []
    try:
        results = get_vectorstore().similarity_search_with_relevance_scores(
            query,
            k=k,
            filter={"topic": "video_catalog"},
        )
    except Exception as exc:
        logger.debug("Video catalog retrieval unavailable: {}", exc)
        return []

    matches: List[VideoMatch] = []
    seen: set[str] = set()
    for document, relevance in results:
        video_id = str((document.metadata or {}).get("video_id") or "").strip()
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        matches.append(VideoMatch(video_id=video_id, relevance=float(relevance)))
    logger.debug("Video RAG '{}' -> {} match(es)", query, len(matches))
    return matches
