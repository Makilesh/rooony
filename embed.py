#!/usr/bin/env python3
"""
embed.py -- gemini-embedding-001 -> vectorstore. Text only, never images.

Emits exactly this record per row (this is the payload the endpoint / Actian
gets, and what lands in vector metadata):

    {
      "session_id": 25,
      "timestamp": "2026-09-06T12:15:00",
      "embedding": [ ... 768 floats ... ],
      "application": "VS Code",
      "text": "roony group chat",
      "source": "screen_image"
    }

Two granularities, same record shape and same vectorstore interface:
  --level frame    (default) one record per extracted frame; text is the raw
                   OCR of the screen, source "screen_image". No LLM anywhere
                   in this path - OCR in, embedding out.
  --level session  one record per closed session; text is title + narrative +
                   entities, source "session_summary".

Several frame records can share a session_id; vectorstore.search() collapses
them to one hit per session, so memory.py behaves the same either way.

If the vector layer dies, we log and keep going: SQLite stays complete and
correct on its own.

    uv run embed.py                        # index new frame records
    uv run embed.py --level session
    uv run embed.py --emit --limit 1       # print one record, index nothing
    uv run embed.py --out records.jsonl    # dump records for the endpoint
    MEM_FAKE_EMBED=1 uv run embed.py       # deterministic offline vectors
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# --- the one place the embedding model is named -------------------------
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"
# ------------------------------------------------------------------------

BATCH = 16
MAX_TEXT_CHARS = 8000
SOURCE_FRAME = "screen_image"
SOURCE_SESSION = "session_summary"

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))
FAKE = os.environ.get("MEM_FAKE_EMBED", "").strip() not in ("", "0", "false")

import vectorstore  # noqa: E402  (after config so it sees the same env)


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def iso(ts):
    return datetime.fromtimestamp(int(ts)).isoformat(timespec="seconds")


# --- embedding ------------------------------------------------------------
_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            sys.exit("GEMINI_API_KEY is not set (or use MEM_FAKE_EMBED=1)")
        _client = genai.Client(api_key=key)
    return _client


def _fake_vector(text):
    """Deterministic pseudo-embedding so the chain runs with no API key."""
    import numpy as np
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "little")
    v = np.random.default_rng(seed).normal(size=EMBED_DIM)
    return (v / np.linalg.norm(v)).astype(float).tolist()


def embed_texts(texts, task_type=TASK_DOCUMENT):
    """-> list of unit-length EMBED_DIM vectors, one per input text."""
    if FAKE:
        return [_fake_vector(t) for t in texts]

    import numpy as np
    from google.genai import types

    out = []
    for i in range(0, len(texts), BATCH):
        chunk = [t[:MAX_TEXT_CHARS] or " " for t in texts[i:i + BATCH]]
        cfg = types.EmbedContentConfig(task_type=task_type,
                                       output_dimensionality=EMBED_DIM)
        for attempt in range(6):
            try:
                r = client().models.embed_content(model=EMBED_MODEL,
                                                  contents=chunk, config=cfg)
                break
            except Exception as e:
                m = f"{type(e).__name__} {e}".upper()
                retryable = any(t in m for t in ("429", "RESOURCE_EXHAUSTED", "503",
                                                 "500", "UNAVAILABLE", "TIMEOUT"))
                if attempt == 5 or not retryable:
                    raise
                wait = min(2 ** (attempt + 1), 60)
                print(f"[embed] {type(e).__name__}: retry in {wait}s", file=sys.stderr)
                time.sleep(wait)
        for e in r.embeddings:
            v = np.asarray(e.values, dtype=float)
            n = np.linalg.norm(v)          # <3072 dims come back un-normalised
            out.append((v / n if n else v).tolist())
    return out


def embed_query(text):
    """One query vector. Used by memory.py."""
    return embed_texts([text], task_type=TASK_QUERY)[0]


# --- record building ------------------------------------------------------
def session_for(con, ts):
    r = con.execute("SELECT id FROM sessions WHERE ? BETWEEN started_at AND ended_at"
                    " ORDER BY started_at DESC LIMIT 1", (int(ts),)).fetchone()
    return r["id"] if r else None


def ensure_log(con):
    """What has been indexed is tracked in SQLite, not in the vector store.

    Reaching into the backend's own tables only worked for the sqlite backend;
    on Actian it raised, every record looked un-indexed, and each run re-embedded
    everything. SQLite is the system of record, so the log lives here.
    """
    con.execute("""CREATE TABLE IF NOT EXISTS vector_log (
                       record_id  TEXT PRIMARY KEY,
                       session_id INTEGER,
                       backend    TEXT,
                       vector_id  TEXT,
                       indexed_at INTEGER)""")
    con.commit()


def already_indexed(con, record_id):
    return con.execute("SELECT 1 FROM vector_log WHERE record_id=? AND backend=?",
                       (record_id, vectorstore.BACKEND)).fetchone() is not None


def frame_rows(con, limit, force):
    rows = con.execute("""
        SELECT e.frame_id, e.ts, e.app, e.window_title, e.ocr_text, e.summary,
               e.task_thread, e.entities, e.activity_type
        FROM extractions e ORDER BY e.ts LIMIT ?""", (limit,)).fetchall()
    out = []
    for r in rows:
        rid = f"frame:{r['frame_id']}"
        if not force and already_indexed(con, rid):
            continue
        sid = session_for(con, r["ts"])
        if sid is None:
            continue          # frame is in the still-open tail; index it next run
        text = (r["ocr_text"] or "").strip()
        if len(text) < 40:    # empty OCR -> fall back to what we do know
            text = "\n".join(x for x in (r["window_title"], r["summary"],
                                         " ".join(json.loads(r["entities"] or "[]")))
                             if x).strip()
        out.append({
            "record_id": rid,
            "session_id": sid,
            "timestamp": iso(r["ts"]),
            "application": r["app"],
            "text": text,
            "source": SOURCE_FRAME,
            "_ts": r["ts"],
            "_thread": r["task_thread"],
            "_activity": r["activity_type"],
        })
    return out


def session_rows(con, limit, force):
    rows = con.execute("SELECT * FROM sessions ORDER BY started_at LIMIT ?",
                       (limit,)).fetchall()
    out = []
    for r in rows:
        if not force and already_indexed(con, f"session:{r['id']}"):
            continue
        app = con.execute("SELECT app, COUNT(*) c FROM extractions WHERE ts BETWEEN ?"
                          " AND ? GROUP BY app ORDER BY c DESC LIMIT 1",
                          (r["started_at"], r["ended_at"])).fetchone()
        ents = json.loads(r["key_entities"] or "[]")
        out.append({
            "record_id": f"session:{r['id']}",
            "session_id": r["id"],
            "timestamp": iso(r["started_at"]),
            "application": app["app"] if app else None,
            "text": "\n".join([r["title"] or "", r["narrative"] or "",
                               ", ".join(ents)]).strip(),
            "source": SOURCE_SESSION,
            "_ts": r["started_at"],
            "_thread": r["thread_slug"],
            "_activity": None,
        })
    return out


def public(rec):
    """The record exactly as the endpoint wants it."""
    return {
        "session_id": rec["session_id"],
        "timestamp": rec["timestamp"],
        "embedding": rec.get("embedding", []),
        "application": rec["application"],
        "text": rec["text"],
        "source": rec["source"],
    }


# --- run ------------------------------------------------------------------
def run(level, limit, force, emit, out_path):
    con = connect()
    ensure_log(con)
    recs = (session_rows if level == "session" else frame_rows)(con, limit, force)
    if not recs:
        has_sessions = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        print(f"nothing new to embed at level={level}" if has_sessions else
              f"nothing to embed at level={level} - run sessionize.py first")
        return []

    vecs = embed_texts([r["text"] for r in recs], TASK_DOCUMENT)
    for r, v in zip(recs, vecs):
        r["embedding"] = v
    print(f"embedded {len(recs)} {level} records "
          f"({EMBED_MODEL}, {EMBED_DIM}d{', FAKE' if FAKE else ''})")

    if out_path:
        Path(out_path).write_text("\n".join(json.dumps(public(r)) for r in recs))
        print(f"wrote {out_path}")
    if emit:
        for r in recs[:3]:
            p = public(r)
            p["embedding"] = p["embedding"][:4] + ["...", f"{EMBED_DIM} floats"]
            print(json.dumps(p, indent=2))
        return recs

    # Two connections write to mem.db (this one and vectorstore's). Never hold an
    # open write txn here while vectorstore commits, or they deadlock on the WAL
    # writer lock: collect the session updates and apply them after the upserts.
    ok = fail = 0
    updates, logged = [], []
    for r in recs:
        try:
            vid = vectorstore.upsert(r["session_id"], r["embedding"], {
                "record_id": r["record_id"],
                "session_id": r["session_id"],
                "thread_slug": r["_thread"],
                "started_at": r["_ts"],
                "timestamp": r["timestamp"],
                "application": r["application"],
                "activity": r["_activity"],
                "source": r["source"],
                "text": r["text"][:500],
            })
            updates.append((vid, r["session_id"]))
            logged.append((r["record_id"], r["session_id"], vectorstore.BACKEND,
                           vid, int(time.time())))
            ok += 1
        except Exception as e:
            # The vector layer is optional. SQLite is the system of record.
            fail += 1
            print(f"[vector] upsert failed for {r['record_id']}: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)
    if logged:
        con.executemany("INSERT OR REPLACE INTO vector_log (record_id, session_id,"
                        " backend, vector_id, indexed_at) VALUES (?,?,?,?,?)", logged)
    if updates:
        con.executemany(
            "UPDATE sessions SET vector_id=? WHERE id=? AND vector_id IS NULL"
            if level == "frame" else
            "UPDATE sessions SET vector_id=? WHERE id=?", updates)
        con.commit()
    print(f"indexed {ok} records via VECTOR_BACKEND={vectorstore.BACKEND}"
          + (f", {fail} failed (sqlite still correct)" if fail else ""))
    return recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", choices=["frame", "session"], default="frame")
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--force", action="store_true", help="re-embed already indexed rows")
    ap.add_argument("--emit", action="store_true", help="print records, index nothing")
    ap.add_argument("--out", help="write the records as JSONL to this path")
    ap.add_argument("--reset", action="store_true", help="drop the vector index")
    args = ap.parse_args()

    if args.reset:
        con = connect()
        ensure_log(con)
        try:
            vectorstore.clear(EMBED_DIM)
        except Exception as e:
            print(f"[vector] clear failed ({type(e).__name__}: {e}); "
                  "clearing the local log anyway", file=sys.stderr)
        con.execute("DELETE FROM vector_log WHERE backend=?", (vectorstore.BACKEND,))
        con.execute("UPDATE sessions SET vector_id=NULL")
        con.commit()
        print(f"vector index cleared ({vectorstore.BACKEND})")
        return
    run(args.level, args.limit, args.force, args.emit, args.out)


if __name__ == "__main__":
    main()
