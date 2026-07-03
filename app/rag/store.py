"""Chroma vector store initialization -- shared by ingest and retrieval."""

from __future__ import annotations

from functools import lru_cache

import chromadb
from langchain_chroma import Chroma
from langchain_openai import OpenAIEmbeddings

from app.config import get_settings


@lru_cache
def get_embeddings() -> OpenAIEmbeddings:
    settings = get_settings()
    return OpenAIEmbeddings(
        model=settings.openai_embedding_model,
        api_key=settings.openai_api_key,
    )


@lru_cache
def get_chroma_client() -> chromadb.HttpClient:
    settings = get_settings()
    return chromadb.HttpClient(host=settings.chroma_host, port=settings.chroma_port)


def get_vectorstore() -> Chroma:
    """Return a Chroma vector store bound to the configured collection."""
    settings = get_settings()
    return Chroma(
        client=get_chroma_client(),
        collection_name=settings.chroma_collection,
        embedding_function=get_embeddings(),
    )
