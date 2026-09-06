#!/usr/bin/env python3
"""
vectorstore.py -- thin adapter. Everything Actian-specific lives in this file.

Public interface, identical on both backends:

    upsert(session_id, vector, metadata) -> str          # returns vector_id
    search(vector, k, time_from=None, time_to=None) -> [(session_id, score)]

Pick the backend with VECTOR_BACKEND=sqlite (default) | actian.

The sqlite backend is a brute-force cosine scan over a `vectors` table in the
same mem.db. At hackathon scale (thousands of rows x 768 dims) it answers in
low milliseconds, so nothing downstream ever has to wait for Actian.

Records may be per-session or per-frame: several vectors can carry the same
session_id, and search() collapses them by taking each session's best score.
That keeps the return type stable whatever granularity embed.py uses.

    uv run vectorstore.py            # backend + row count + a self-test
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))
BACKEND = os.environ.get("VECTOR_BACKEND", "sqlite").strip().lower()


# =========================================================================
# ACTIAN VECTORAI
#
# The real SDK calls live in src/vectorstore.py (the MCP side wrote and proved
# them against a live server). This class is only the adapter that puts them
# behind the same upsert/search interface the rest of the pipeline uses, so
# there is exactly ONE place that talks to Actian.
#
# Env: VECTORAI_HOST (default localhost:6574), VECTORAI_COLLECTION
#      (default screen_activity). docker-compose.yml brings the server up.
#
# Payload written per point (their MCP tools read description/application/
# activity; memory.py reads session_id/thread_slug/started_at):
#   {session_id, thread_slug, timestamp, timestamp_epoch, application,
#    activity, text, description, source, record_id}
# =========================================================================
class ActianBackend:
    def __init__(self):
        # imported lazily: embed.py imports this module, so a top-level import
        # of src.* (which imports embed) would be circular
        from src import vectorstore as actian
        self.a = actian
        self.host = actian.config.VECTORAI_HOST
        self.collection = actian.config.COLLECTION
        self._ensured = False

    def _ensure(self, dim):
        if not self._ensured:
            self.a.ensure_collection(size=dim)
            self._ensured = True

    def upsert(self, session_id, vector, metadata):
        v = [float(x) for x in vector]
        self._ensure(len(v))
        rid = str(metadata.get("record_id") or f"session:{session_id}")
        text = metadata.get("text", "")
        record = {
            "timestamp": metadata.get("timestamp"),
            "embedding": v,
            "session_id": session_id,
            "thread_slug": metadata.get("thread_slug"),
            "application": metadata.get("application"),
            "activity": metadata.get("activity"),
            "text": text,
            "description": text,      # what the MCP tools' formatter reads
            "source": metadata.get("source"),
            "record_id": rid,
        }
        self.a.upsert_activity(rid, record)   # flushes; unflushed writes are invisible
        return rid

    def search(self, vector, k, time_from=None, time_to=None):
        iso = lambda t: datetime.fromtimestamp(t).isoformat() if t is not None else None
        hits = self.a.search_by_vector(
            [float(x) for x in vector], limit=max(k * 4, k),
            start_iso=iso(time_from), end_iso=iso(time_to))
        best = {}
        for h in hits:
            sid, score = h.get("session_id"), h.get("score")
            if sid is None or score is None:
                continue          # points written by something else (e.g. their seed script)
            if score > best.get(sid, -2.0):
                best[sid] = float(score)
        return sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:k]

    def points(self, vector=None, k=20, time_from=None, time_to=None):
        """Point-level hits (not collapsed to sessions), for the frame tools."""
        iso = lambda t: datetime.fromtimestamp(t).isoformat() if t is not None else None
        if vector is None:
            return self.a.search_by_time(iso(time_from), iso(time_to), limit=k)
        return self.a.search_by_vector([float(x) for x in vector], limit=k,
                                       start_iso=iso(time_from), end_iso=iso(time_to))

    def clear(self, dim=None):
        """Drop and recreate the collection.

        Also the fix for a dimension change: an existing collection's vector
        size cannot be altered in place, so a collection left over from a
        different embedding model must be recreated or every upsert fails.
        """
        self.a.ensure_collection(size=dim or 768, recreate=True)
        self._ensured = True

    def count(self):
        return self.a.count()
# ========================= end Actian block ==============================


class SqliteBackend:
    """Brute-force cosine over float32 blobs. Boring, exact, always available."""

    def __init__(self):
        self.con = sqlite3.connect(DB_PATH, timeout=30)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS vectors (
                record_id   TEXT PRIMARY KEY,
                session_id  INTEGER,
                thread_slug TEXT,
                started_at  INTEGER,
                application TEXT,
                source      TEXT,
                dim         INTEGER,
                vec         BLOB,
                metadata    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_vectors_session ON vectors(session_id);
            CREATE INDEX IF NOT EXISTS idx_vectors_started ON vectors(started_at);
        """)
        self.con.commit()

    def upsert(self, session_id, vector, metadata):
        v = np.asarray(vector, dtype=np.float32)
        n = float(np.linalg.norm(v))
        if n:
            v = v / n                      # store normalised: cosine == dot
        rid = str(metadata.get("record_id") or f"session:{session_id}")
        self.con.execute(
            "INSERT OR REPLACE INTO vectors (record_id, session_id, thread_slug,"
            " started_at, application, source, dim, vec, metadata)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, session_id, metadata.get("thread_slug"),
             metadata.get("started_at"), metadata.get("application"),
             metadata.get("source"), int(v.size), v.tobytes(),
             json.dumps(metadata, default=str)))
        self.con.commit()
        return rid

    def search(self, vector, k, time_from=None, time_to=None):
        sql = "SELECT session_id, started_at, vec FROM vectors WHERE session_id IS NOT NULL"
        args = []
        if time_from is not None:
            sql += " AND started_at >= ?"
            args.append(int(time_from))
        if time_to is not None:
            sql += " AND started_at <= ?"
            args.append(int(time_to))
        rows = self.con.execute(sql, args).fetchall()
        if not rows:
            return []

        q = np.asarray(vector, dtype=np.float32)
        qn = np.linalg.norm(q)
        if qn:
            q = q / qn
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32)
        mat = mat.reshape(len(rows), -1)
        if mat.shape[1] != q.size:
            raise ValueError(f"dim mismatch: index {mat.shape[1]}, query {q.size}")
        scores = mat @ q

        best = {}
        for r, s in zip(rows, scores):
            sid = r["session_id"]
            if s > best.get(sid, -2.0):
                best[sid] = float(s)
        return sorted(best.items(), key=lambda kv: kv[1], reverse=True)[:k]

    def points(self, vector=None, k=20, time_from=None, time_to=None):
        """Point-level hits (not collapsed to sessions), for the frame tools."""
        sql = "SELECT record_id, session_id, started_at, vec, metadata FROM vectors WHERE 1=1"
        args = []
        if time_from is not None:
            sql += " AND started_at >= ?"
            args.append(int(time_from))
        if time_to is not None:
            sql += " AND started_at <= ?"
            args.append(int(time_to))
        rows = self.con.execute(sql + " ORDER BY started_at DESC", args).fetchall()
        if not rows:
            return []

        scores = [None] * len(rows)
        if vector is not None:
            q = np.asarray(vector, dtype=np.float32)
            qn = np.linalg.norm(q)
            if qn:
                q = q / qn
            mat = np.frombuffer(b"".join(r["vec"] for r in rows),
                                dtype=np.float32).reshape(len(rows), -1)
            if mat.shape[1] != q.size:
                raise ValueError(f"dim mismatch: index {mat.shape[1]}, query {q.size}")
            scores = [float(x) for x in (mat @ q)]

        out = []
        for r, sc in zip(rows, scores):
            payload = json.loads(r["metadata"] or "{}")
            payload["id"] = r["record_id"]
            payload.setdefault("description", payload.get("text", ""))
            if sc is not None:
                payload["score"] = sc
            out.append(payload)
        if vector is not None:
            out.sort(key=lambda p: p["score"], reverse=True)
        return out[:k]

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    def clear(self, dim=None):
        self.con.execute("DELETE FROM vectors")
        self.con.commit()


_backend = None


def backend():
    global _backend
    if _backend is None:
        _backend = ActianBackend() if BACKEND == "actian" else SqliteBackend()
    return _backend


def upsert(session_id, vector, metadata) -> str:
    """Index one record. Returns the vector_id to stash on the session row."""
    return backend().upsert(session_id, vector, metadata or {})


def search(vector, k, time_from=None, time_to=None):
    """-> [(session_id, cosine_score)], best first, one entry per session."""
    return backend().search(vector, k, time_from, time_to)


def points(vector=None, k=20, time_from=None, time_to=None):
    """Individual indexed records, newest or most similar first - NOT collapsed
    to one hit per session. The frame-level MCP tools use this."""
    return backend().points(vector, k, time_from, time_to)


def clear(dim=None):
    """Empty the index. On Actian this drops and recreates the collection,
    which is also how you change embedding dimensions."""
    return backend().clear(dim)


def count():
    return backend().count()


if __name__ == "__main__":
    print(f"VECTOR_BACKEND={BACKEND}  db={DB_PATH}")
    b = backend()
    print(f"rows indexed: {b.count()}")
    if "--self-test" in sys.argv:
        rng = np.random.default_rng(0)
        a = rng.normal(size=768)
        upsert(-1, a, {"record_id": "selftest:-1", "started_at": 0, "source": "test"})
        hits = search(a, 3)
        print("self-test hit:", hits[0] if hits else None)
        b.con.execute("DELETE FROM vectors WHERE record_id='selftest:-1'")
        b.con.commit()
