"""Config for the MCP layer.

The embedding model and dimensionality are NOT defined here: they come from
embed.py, which is the one place they are allowed to live. If the query side
and the write side ever disagree on model or dims, cosine distance between a
query and a stored vector is meaningless and search silently returns nonsense.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from embed import EMBED_DIM, EMBED_MODEL  # noqa: E402,F401  (re-exported)

VECTORAI_HOST = os.environ.get("VECTORAI_HOST", "localhost:6574")
COLLECTION = os.environ.get("VECTORAI_COLLECTION", "screen_activity")
