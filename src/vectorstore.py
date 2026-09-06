"""Thin adapter over the Actian VectorAI Python SDK
(pip package `actian-vectorai-client`, import name `actian_vectorai`).

Kept as a single narrow surface so that when the real schema/collection
(owned by the capture+embedding teammates) lands, only this file should
need to change — not the MCP tool layer.

Record shape assumed for now (adjust `upsert_activity` / `_from_point` when
the real schema arrives):

    {
        "timestamp": "2026-09-06T12:15:00",  # ISO 8601, local time
        "embedding": [...],                   # EMBED_DIM (384) floats
        "activity": "coding",
        "application": "VS Code",
        "description": "Debugging pandas contact matching",
        "source": "screen_image",
    }
"""
from datetime import datetime, timezone

from . import config

_client = None


def get_client():
    global _client
    if _client is None:
        from actian_vectorai import VectorAIClient
        _client = VectorAIClient(config.VECTORAI_HOST)
        _client.connect()
    return _client


def _to_epoch(iso_ts: str) -> float:
    """Naive timestamps are LOCAL, not UTC.

    Everything upstream writes local naive ISO (embed.py uses
    datetime.fromtimestamp(...).isoformat(), time_parse.py uses
    datetime.now()), so reading them as UTC would shift every stored
    timestamp_epoch by the machine's offset and quietly return the wrong
    frames for every time-window query.
    """
    dt = datetime.fromisoformat(iso_ts)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp()


def ensure_collection(size: int = None, recreate: bool = False):
    """Create the collection if it doesn't exist yet. Safe to call repeatedly.

    Pass recreate=True to drop and recreate it — needed when switching
    embedding models/dimensions, since an existing collection's vector size
    can't be changed in place.
    """
    from actian_vectorai import VectorParams, Distance

    client = get_client()
    size = size or config.EMBED_DIM
    if client.collections.exists(config.COLLECTION):
        if not recreate:
            existing = _vector_size(client)
            if existing is not None and existing != size:
                raise RuntimeError(
                    f"collection {config.COLLECTION!r} stores {existing}-dim vectors "
                    f"but this build embeds at {size} ({config.EMBED_MODEL}). An "
                    f"existing collection's vector size cannot be changed in place - "
                    f"run `VECTOR_BACKEND=actian uv run embed.py --reset` to drop and "
                    f"recreate it, then re-embed.")
            return
        client.collections.delete(config.COLLECTION)
    client.collections.create(
        config.COLLECTION,
        vectors_config=VectorParams(size=size, distance=Distance.Cosine),
    )


def _vector_size(client):
    """The collection's configured vector size, or None if the SDK won't say.

    A stale collection left at another model's dimensions is the failure that
    makes every cosine come back 0.0 rather than erroring, so it is worth one
    best-effort lookup. Shapes differ across SDK versions; None means "could
    not tell", and we let the upsert speak for itself.
    """
    try:
        info = client.collections.get_info(config.COLLECTION)
        node = getattr(getattr(info, "config", None), "params", None)
        vectors = getattr(node, "vectors", None)
        size = getattr(vectors, "size", None)
        return int(size) if size else None
    except Exception:
        return None


def upsert_activity(point_id: str, record: dict):
    """record must contain `embedding` plus the metadata fields listed in the
    module docstring. `timestamp` is stored both as the original ISO string
    (for display) and as `timestamp_epoch` (a float) so range filters work."""
    from actian_vectorai import PointStruct

    client = get_client()
    payload = {k: v for k, v in record.items() if k != "embedding"}
    payload["timestamp_epoch"] = _to_epoch(record["timestamp"])

    client.points.upsert(
        config.COLLECTION,
        [PointStruct(id=point_id, vector=record["embedding"], payload=payload)],
    )
    # Writes are buffered until flushed — without this, count()/scroll()/
    # search() won't see this point at all (confirmed empirically: point
    # showed up in collections.get_info()'s points_count but not in scroll()
    # or search() results until flush() was called).
    client.vde.flush(config.COLLECTION)


def _time_range_filter(start_iso: str = None, end_iso: str = None):
    if not start_iso and not end_iso:
        return None
    from actian_vectorai import Field, FilterBuilder

    cond = Field("timestamp_epoch").range(
        gte=_to_epoch(start_iso) if start_iso else None,
        lte=_to_epoch(end_iso) if end_iso else None,
    )
    return FilterBuilder().must(cond).build()


def search_by_time(start_iso: str = None, end_iso: str = None, limit: int = 20):
    """Pure metadata lookup, no embedding involved — 'what was I doing at 16:00'."""
    client = get_client()
    flt = _time_range_filter(start_iso, end_iso)
    points, _next_offset = client.points.scroll(
        config.COLLECTION,
        filter=flt,
        limit=limit,
        with_payload=True,
    )
    return [_from_point(p) for p in points]


def search_by_vector(vector: list[float], limit: int = 5, start_iso: str = None, end_iso: str = None):
    """Semantic search, optionally narrowed to a time window."""
    client = get_client()
    flt = _time_range_filter(start_iso, end_iso)
    results = client.points.search(
        config.COLLECTION,
        vector=vector,
        limit=limit,
        filter=flt,
        with_payload=True,
    )
    return [_from_point(p, score=getattr(p, "score", None)) for p in results]


def count() -> int:
    """How many points the collection holds (-1 if the server can't say)."""
    try:
        info = get_client().collections.get_info(config.COLLECTION)
        return int(getattr(info, "points_count", -1))
    except Exception:
        return -1


def _from_point(p, score: float = None) -> dict:
    payload = dict(getattr(p, "payload", {}) or {})
    payload.pop("timestamp_epoch", None)
    payload["id"] = getattr(p, "id", None)
    if score is not None:
        payload["score"] = score
    return payload
