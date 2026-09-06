import os

VECTORAI_HOST = os.environ.get("VECTORAI_HOST", "localhost:6574")
COLLECTION = os.environ.get("VECTORAI_COLLECTION", "screen_activity")
# CLIP embeds both text and images into the same vector space, which is what
# lets a text query ("what was I doing in Gmail") be compared against an
# embedding produced from a screenshot's pixels. A text-only model (like
# MiniLM) can't do that — it has no notion of image content.
EMBED_MODEL = os.environ.get("EMBED_MODEL", "clip-ViT-B-32")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "512"))
