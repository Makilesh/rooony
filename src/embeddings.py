"""Query-side embedding.

Was CLIP via sentence-transformers (512-dim, image+text in one space). That is
gone: the fixed stack for this project is gemini-embedding-001 at 768 dims, no
local model downloads and no torch, and we never embed images — screenshots are
turned into text by OCR (and Gemini vision when OCR is empty) and the *text* is
what gets embedded. This module is now a thin shim over embed.py so the MCP
server and the indexing pipeline can never drift apart on model or dimension.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import embed  # noqa: E402


def embed_text(text: str) -> list[float]:
    """One query vector (RETRIEVAL_QUERY task type)."""
    return embed.embed_query(text)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Document vectors (RETRIEVAL_DOCUMENT task type)."""
    return embed.embed_texts(texts, embed.TASK_DOCUMENT)


def embed_image(path: str):
    raise NotImplementedError(
        "images are never embedded in this pipeline - extract.py OCRs the "
        "screenshot (Gemini vision when OCR is empty) and embed.py embeds that "
        "text instead. See readme.md."
    )
