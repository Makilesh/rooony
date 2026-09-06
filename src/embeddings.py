"""Query-side embedding.

The fixed stack for this project is BAAI/bge-small-en-v1.5 at 384 dims, run
locally — no embedding API calls — and we never embed images: screenshots are
turned into text by ocrmac and the *text* is what gets embedded. This module is
a thin shim over embed.py so the MCP server and the indexing pipeline can never
drift apart on model or dimension.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import embed  # noqa: E402


def embed_text(text: str) -> list[float]:
    """One query vector (carries the bge query instruction prefix)."""
    return embed.embed_query(text)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Document vectors (no prefix - bge stores passages bare)."""
    return embed.embed_texts(texts, embed.TASK_DOCUMENT)


def embed_image(path: str):
    raise NotImplementedError(
        "images are never embedded in this pipeline - extract.py OCRs the "
        "screenshot and embed.py embeds that text with bge-small-en-v1.5 "
        "instead. See readme.md."
    )
