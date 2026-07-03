"""One-off script to (re)build the RAG knowledge base.

Usage::

    python -m scripts.ingest_knowledge          # additive
    python -m scripts.ingest_knowledge --reset  # wipe and rebuild
"""

from __future__ import annotations

import argparse

from loguru import logger

from app.rag.ingest import ingest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Propeller Drones RAG index")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop the existing Chroma collection before ingesting.",
    )
    args = parser.parse_args()

    count = ingest(reset=args.reset)
    logger.info("Done. {} chunks in the store.", count)


if __name__ == "__main__":
    main()
