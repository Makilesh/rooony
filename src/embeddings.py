"""Shared text/image embedding.

Uses CLIP (via sentence-transformers) so a text query and an image both land
in the same vector space — this is what makes "what was I doing in Gmail"
comparable against a vector produced from a screenshot's pixels.

This is a stand-in for whatever embedding model your teammates' capture
pipeline actually uses. If they use a different model/dimensionality, the
query side (embed_text, used by search_by_text/ask) must be switched to
match, or cosine distance between query and stored vectors is meaningless.
"""
from functools import lru_cache

from . import config


@lru_cache(maxsize=1)
def _model():
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(config.EMBED_MODEL)


def embed_text(text: str) -> list[float]:
    vec = _model().encode(text, normalize_embeddings=True)
    return vec.tolist()


def embed_image(path: str) -> list[float]:
    from PIL import Image

    with Image.open(path) as img:
        img = img.convert("RGB")
        vec = _model().encode(img, normalize_embeddings=True)
    return vec.tolist()
