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
import struct
import sys
from pathlib import Path

import numpy as np

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))
BACKEND = os.environ.get("VECTOR_BACKEND", "sqlite").strip().lower()


# =========================================================================
# ACTIAN VECTORAI -- TODO: swap in the real client once the signatures land.
# Nothing outside this block knows Actian exists. Keep it that way.
#
# Expected env:
#   ACTIAN_VECTOR_URL      e.g. https://<host>:<port>
#   ACTIAN_VECTOR_API_KEY
#   ACTIAN_VECTOR_DB       database / vector store name
#   ACTIAN_VECTOR_TABLE    default "screen_memory"
#
# The record we hand it (same JSON embed.py emits):
#   {session_id, timestamp, embedding[768], application, text, source}
# =========================================================================
class ActianBackend:
    TABLE = os.environ.get("ACTIAN_VECTOR_TABLE", "screen_memory")

    def __init__(self):
        self.url = os.environ.get("ACTIAN_VECTOR_URL")
        self.api_key = os.environ.get("ACTIAN_VECTOR_API_KEY")
        self.db = os.environ.get("ACTIAN_VECTOR_DB")
        if not (self.url and self.api_key):
            raise RuntimeError(
                "VECTOR_BACKEND=actian but ACTIAN_VECTOR_URL / "
                "ACTIAN_VECTOR_API_KEY are not set")
        # TODO(actian): construct the real client, e.g.
        #   from actian_vectorai import Client
        #   self.client = Client(url=self.url, api_key=self.api_key, database=self.db)
        #   self.client.create_collection(self.TABLE, dim=768, metric="cosine")
        self.client = None
        raise RuntimeError(
            "Actian client not wired yet - see the TODO block in vectorstore.py. "
            "Run with VECTOR_BACKEND=sqlite until then.")

    def upsert(self, session_id, vector, metadata):
        # TODO(actian): one row per record_id, cosine metric, 768 dims.
        #   rec = {"id": metadata["record_id"], "session_id": session_id,
        #          "embedding": list(vector), **metadata}
        #   self.client.upsert(self.TABLE, [rec])
        #   return metadata["record_id"]
        raise NotImplementedError

    def search(self, vector, k, time_from=None, time_to=None):
        # TODO(actian): push the time window down as a metadata filter, e.g.
        #   flt = {"started_at": {"$gte": time_from, "$lte": time_to}}
        #   hits = self.client.search(self.TABLE, vector=list(vector),
        #                             top_k=k * 4, filter=flt, metric="cosine")
        # then collapse hits to one row per session_id, best score first:
        #   return dedupe([(h["session_id"], h["score"]) for h in hits])[:k]
        raise NotImplementedError

    def count(self):
        raise NotImplementedError
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

    def count(self):
        return self.con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    def clear(self):
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
