"""RAG ingestion: scrape Propeller Drones' website and load local documents.

Run via ``python -m scripts.ingest_knowledge`` after configuring ``.env``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List
from urllib.parse import urljoin

from langchain_community.document_loaders import (
    DirectoryLoader,
    PyPDFLoader,
    TextLoader,
    WebBaseLoader,
)
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from loguru import logger

from app.config import get_settings
from app.rag.store import get_vectorstore


# Curated set of high-value pages from propeller-drones.com. We hard-code
# these instead of crawling because the site is small and we want deterministic,
# high-quality knowledge chunks.
WEBSITE_PATHS: List[str] = [
    # Highest-priority page for lead-conversion: full course details,
    # salaries, instructors, industries, FAQ.
    "/training-center/%D7%9B%D7%9C-%D7%94%D7%A4%D7%A8%D7%98%D7%99%D7%9D-%D7%A7%D7%95%D7%A8%D7%A1%D7%99-%D7%94%D7%98%D7%A1%D7%AA-%D7%A8%D7%97%D7%A4%D7%A0%D7%99%D7%9D/",
    "/",
    "/training-center/",
    "/training-center/%D7%A8%D7%99%D7%A9%D7%99%D7%95%D7%9F-%D7%9E%D7%A1%D7%97%D7%A8%D7%99-%D7%9C%D7%A8%D7%97%D7%A4%D7%9F/",
    "/services/advanced-flight-services/",
    "/services/high-pressure-washing/",
]

DEFAULT_CHUNK_SIZE = 1000
DEFAULT_CHUNK_OVERLAP = 200

KNOWLEDGE_DIR = Path("data/knowledge")


def _website_urls() -> List[str]:
    base = get_settings().propeller_website_base.rstrip("/")
    return [urljoin(base + "/", path.lstrip("/")) for path in WEBSITE_PATHS]


def load_website_documents() -> List[Document]:
    urls = _website_urls()
    logger.info("Loading {} pages from propeller-drones.com", len(urls))

    loader = WebBaseLoader(
        web_paths=urls,
        requests_kwargs={"timeout": 30},
    )
    docs = loader.load()

    for doc in docs:
        doc.metadata.setdefault("source", doc.metadata.get("source", "website"))
        doc.metadata["origin"] = "website"
        doc.metadata["topic"] = _infer_topic_from_url(doc.metadata.get("source", ""))

    logger.info("Fetched {} website documents", len(docs))
    return docs


def _infer_topic_from_url(url: str) -> str:
    url = url.lower()
    # "כל-הפרטים-קורסי-הטסת-רחפנים" -- the flagship course-details page
    if "%d7%9b%d7%9c-%d7%94%d7%a4%d7%a8%d7%98%d7%99%d7%9d" in url:
        return "course_details"
    # "רישיון-מסחרי-לרחפן" -- commercial-license focused page
    if "%d7%a8%d7%99%d7%a9%d7%99%d7%95%d7%9f" in url:
        return "course_license"
    if "training" in url:
        return "courses"
    if "washing" in url:
        return "service_washing"
    if "flight-services" in url:
        return "service_flight"
    if url.rstrip("/").endswith("propeller-drones.com"):
        return "about"
    return "general"


def load_local_documents(directory: Path = KNOWLEDGE_DIR) -> List[Document]:
    """Load PDFs and text files from ``data/knowledge``."""
    if not directory.exists():
        logger.info("Knowledge directory {} does not exist, skipping", directory)
        return []

    docs: List[Document] = []

    pdf_loader = DirectoryLoader(
        str(directory),
        glob="**/*.pdf",
        loader_cls=PyPDFLoader,
        show_progress=False,
        use_multithreading=False,
    )
    try:
        pdf_docs = pdf_loader.load()
    except Exception as exc:  # noqa: BLE001
        logger.warning("PDF loading failed: {}", exc)
        pdf_docs = []

    # NOTE: python glob doesn't support brace expansion ("*.{txt,md}"), so
    # we load each extension separately and concatenate.
    text_docs = []
    for pattern in ("**/*.txt", "**/*.md"):
        _loader = DirectoryLoader(
            str(directory),
            glob=pattern,
            loader_cls=TextLoader,
            loader_kwargs={"encoding": "utf-8"},
            show_progress=False,
        )
        try:
            text_docs.extend(_loader.load())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Text loading failed for pattern {}: {}", pattern, exc)

    for doc in pdf_docs + text_docs:
        doc.metadata["origin"] = "document"
        src = str(doc.metadata.get("source", "")).lower()
        if "academy_products" in src:
            doc.metadata["topic"] = "course_details"
        elif "ecommerce_store" in src or "store" in src:
            doc.metadata["topic"] = "shop"
        elif "faq_from_leads" in src or "faq" in src:
            doc.metadata["topic"] = "faq"
        else:
            doc.metadata.setdefault("topic", "documents")
        docs.append(doc)

    logger.info(
        "Loaded {} local documents ({} PDFs, {} text)",
        len(docs), len(pdf_docs), len(text_docs),
    )
    return docs


def chunk_documents(
    docs: Iterable[Document],
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> List[Document]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", " ", ""],
    )
    chunks = splitter.split_documents(list(docs))
    logger.info("Split into {} chunks", len(chunks))
    return chunks


def ingest(reset: bool = False) -> int:
    """Full ingestion: load, split, embed, store. Returns number of chunks added."""
    website_docs = load_website_documents()
    local_docs = load_local_documents()
    all_docs = website_docs + local_docs

    if not all_docs:
        logger.warning("No documents to ingest -- aborting")
        return 0

    chunks = chunk_documents(all_docs)

    vectorstore = get_vectorstore()

    if reset:
        try:
            settings = get_settings()
            from app.rag.store import get_chroma_client

            get_chroma_client().delete_collection(settings.chroma_collection)
            logger.info("Reset: dropped collection {}", settings.chroma_collection)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Reset failed (collection may not exist yet): {}", exc)
        vectorstore = get_vectorstore()

    vectorstore.add_documents(chunks)
    logger.info("Ingested {} chunks into Chroma", len(chunks))
    return len(chunks)
