#!/usr/bin/env python3
"""
memory.py -- the public API. Import these three:

    recall_context(query: str, limit: int = 5) -> dict
    what_was_i_doing(minutes_ago: int = 30) -> dict
    todays_receipt() -> dict

Every session object comes back as:
    {session_id, title, thread_slug, started_at, ended_at, duration_min,
     narrative, resume_hint, entities[], keyframe_path}

recall_context: Gemini parses a time window out of the query (regex fallback
when there is no key) -> vector search filtered to that window -> union with an
exact SQL entity match -> rank 0.55*cosine + 0.30*entity_overlap +
0.15*recency_decay(6h half-life) -> expand the winner to its full thread arc.
If the vector layer is unavailable it degrades to SQLite LIKE on narrative and
says so in `degraded`.

    uv run memory.py recall "what was that cors bug"
    uv run memory.py doing 30
    uv run memory.py receipt
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

MODEL = "gemini-2.5-flash"
HALF_LIFE_S = 6 * 3600
W_COSINE, W_ENTITY, W_RECENCY = 0.55, 0.30, 0.15
CANDIDATES = 40

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))

STOP = {"what", "was", "were", "i", "the", "a", "an", "on", "in", "at", "to", "of",
        "my", "me", "did", "do", "doing", "that", "this", "it", "is", "am", "for",
        "about", "with", "and", "or", "show", "find", "when", "how", "again",
        "yesterday", "today", "morning", "afternoon", "evening", "hour", "hours",
        "minute", "minutes", "ago", "last", "night", "week"}


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


# --- shared shapes --------------------------------------------------------
def session_obj(r):
    return {
        "session_id": r["id"],
        "title": r["title"],
        "thread_slug": r["thread_slug"],
        "started_at": datetime.fromtimestamp(r["started_at"]).isoformat(timespec="seconds"),
        "ended_at": datetime.fromtimestamp(r["ended_at"]).isoformat(timespec="seconds"),
        "duration_min": round((r["duration_s"] or 0) / 60),
        "narrative": r["narrative"],
        "resume_hint": r["resume_hint"],
        "entities": json.loads(r["key_entities"] or "[]"),
        "keyframe_path": r["keyframe_path"],
    }


def thread_arc(con, slug):
    rows = con.execute("SELECT * FROM sessions WHERE thread_slug=? ORDER BY started_at",
                       (slug,)).fetchall()
    t = con.execute("SELECT * FROM threads WHERE slug=?", (slug,)).fetchone()
    return {
        "thread_slug": slug,
        "title": (t["title"] if t else slug),
        "status": (t["status"] if t else None),
        "total_minutes": round((t["total_seconds"] if t else 0) / 60),
        "first_seen": datetime.fromtimestamp(t["first_seen"]).isoformat(timespec="seconds") if t else None,
        "last_seen": datetime.fromtimestamp(t["last_seen"]).isoformat(timespec="seconds") if t else None,
        "rolling_summary": (t["rolling_summary"] if t else None),
        "sessions": [session_obj(r) for r in rows],
    }


# --- time window ----------------------------------------------------------
class Window(BaseModel):
    has_window: bool
    time_from: Optional[str] = None     # ISO 8601 local
    time_to: Optional[str] = None
    cleaned_query: str


def _parse_window_regex(query):
    """No-LLM fallback. Handles the phrasings people actually type."""
    q = query.lower()
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lo = hi = None

    m = re.search(r"(\d+)\s*(min|minute|hour|hr|day)s?\s*ago", q)
    if m:
        n = int(m.group(1))
        delta = timedelta(minutes=n) if m.group(2).startswith(("min",)) else (
            timedelta(hours=n) if m.group(2).startswith(("hour", "hr")) else timedelta(days=n))
        lo, hi = now - delta - timedelta(minutes=30), now - delta + timedelta(minutes=30)
    elif re.search(r"last\s+(\d+)\s*(min|minute|hour|hr)s?", q):
        m = re.search(r"last\s+(\d+)\s*(min|minute|hour|hr)s?", q)
        n = int(m.group(1))
        lo = now - (timedelta(minutes=n) if m.group(2).startswith("min") else timedelta(hours=n))
        hi = now
    elif "yesterday" in q:
        lo, hi = midnight - timedelta(days=1), midnight
    elif "this morning" in q:
        lo, hi = midnight + timedelta(hours=5), midnight + timedelta(hours=12)
    elif "this afternoon" in q:
        lo, hi = midnight + timedelta(hours=12), midnight + timedelta(hours=18)
    elif "tonight" in q or "this evening" in q:
        lo, hi = midnight + timedelta(hours=18), now
    elif "today" in q:
        lo, hi = midnight, now
    elif "this week" in q:
        lo, hi = midnight - timedelta(days=now.weekday()), now
    elif "last week" in q:
        lo = midnight - timedelta(days=now.weekday() + 7)
        hi = lo + timedelta(days=7)

    cleaned = " ".join(w for w in re.findall(r"[a-z0-9_.:/#-]+", q) if w not in STOP)
    return (lo.timestamp() if lo else None,
            hi.timestamp() if hi else None,
            cleaned or query)


def parse_window(query):
    """-> (time_from, time_to, cleaned_query) as epoch seconds / str."""
    if not os.environ.get("GEMINI_API_KEY"):
        return _parse_window_regex(query)
    try:
        from google import genai
        from google.genai import types
        now = datetime.now()
        cfg = types.GenerateContentConfig(
            system_instruction=(
                f"Now is {now.isoformat(timespec='seconds')} ({now.strftime('%A')}). "
                "Extract any time window the query implies, as local ISO 8601. "
                "has_window=false when the query names no time at all. "
                "cleaned_query is the query with the time words stripped, keeping "
                "every topic word, filename, error string and ticket id."),
            temperature=0,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            response_mime_type="application/json",
            response_schema=Window,
        )
        c = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        r = c.models.generate_content(model=MODEL, contents=query, config=cfg)
        w = r.parsed or Window(**json.loads(r.text))
        if not w.has_window:
            return None, None, (w.cleaned_query or query)
        f = datetime.fromisoformat(w.time_from).timestamp() if w.time_from else None
        t = datetime.fromisoformat(w.time_to).timestamp() if w.time_to else None
        return f, t, (w.cleaned_query or query)
    except Exception as e:
        print(f"[memory] window parse fell back to regex: {e}", file=sys.stderr)
        return _parse_window_regex(query)


# --- ranking --------------------------------------------------------------
def recency_decay(ended_at, now=None):
    age = max((now or time.time()) - ended_at, 0)
    return 0.5 ** (age / HALF_LIFE_S)


def entity_hits(con, cleaned, time_from, time_to):
    """Exact (case-insensitive) entity match -> {session_id: [entities]}."""
    tokens = [t for t in re.findall(r"[A-Za-z0-9_.:/#-]+", cleaned.lower())
              if len(t) > 2 and t not in STOP]
    if not tokens:
        return {}, []
    ph = ",".join("?" * len(tokens))
    sql = (f"SELECT e.session_id, e.entity FROM entities e JOIN sessions s "
           f"ON s.id = e.session_id WHERE lower(e.entity) IN ({ph})")
    args = list(tokens)
    if time_from is not None:
        sql += " AND s.ended_at >= ?"
        args.append(int(time_from))
    if time_to is not None:
        sql += " AND s.started_at <= ?"
        args.append(int(time_to))
    hits = {}
    for r in con.execute(sql, args):
        hits.setdefault(r["session_id"], []).append(r["entity"])
    # substring pass for things like "cors" inside "cors-preflight-fix"
    for tok in tokens:
        like = f"%{tok}%"
        for r in con.execute(
                "SELECT session_id, entity FROM entities WHERE lower(entity) LIKE ?",
                (like,)):
            hits.setdefault(r["session_id"], [])
            if r["entity"] not in hits[r["session_id"]]:
                hits[r["session_id"]].append(r["entity"])
    return hits, tokens


def like_fallback(con, cleaned, time_from, time_to, limit):
    tokens = [t for t in re.findall(r"[A-Za-z0-9_.:/#-]+", cleaned.lower())
              if len(t) > 2 and t not in STOP] or [cleaned]
    where = " OR ".join(["lower(narrative) LIKE ?", "lower(title) LIKE ?",
                         "lower(thread_slug) LIKE ?"] * len(tokens))
    args = []
    for t in tokens:
        args += [f"%{t}%"] * 3
    sql = f"SELECT * FROM sessions WHERE ({where})"
    if time_from is not None:
        sql += " AND ended_at >= ?"
        args.append(int(time_from))
    if time_to is not None:
        sql += " AND started_at <= ?"
        args.append(int(time_to))
    sql += " ORDER BY started_at DESC LIMIT ?"
    args.append(limit)
    return con.execute(sql, args).fetchall()


# --- public API -----------------------------------------------------------
def recall_context(query: str, limit: int = 5) -> dict:
    con = connect()
    time_from, time_to, cleaned = parse_window(query)
    ents, tokens = entity_hits(con, cleaned, time_from, time_to)

    cosines, degraded = {}, None
    try:
        import embed
        import vectorstore
        qv = embed.embed_query(cleaned)
        cosines = dict(vectorstore.search(qv, CANDIDATES, time_from, time_to))
    except Exception as e:
        degraded = f"{type(e).__name__}: {e}"
        print(f"[memory] vector layer unavailable, using SQLite LIKE: {degraded}",
              file=sys.stderr)

    now = time.time()
    scored = []
    if cosines or ents:
        for sid in set(cosines) | set(ents):
            r = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not r:
                continue
            cos = max(cosines.get(sid, 0.0), 0.0)
            overlap = (len(ents.get(sid, [])) / len(tokens)) if tokens else 0.0
            overlap = min(overlap, 1.0)
            rec = recency_decay(r["ended_at"], now)
            score = W_COSINE * cos + W_ENTITY * overlap + W_RECENCY * rec
            o = session_obj(r)
            o["score"] = round(score, 4)
            o["score_parts"] = {"cosine": round(cos, 4),
                                "entity_overlap": round(overlap, 4),
                                "recency": round(rec, 4)}
            o["matched_entities"] = ents.get(sid, [])
            scored.append(o)
    else:
        for r in like_fallback(con, cleaned, time_from, time_to, limit):
            o = session_obj(r)
            o["score"] = round(W_RECENCY * recency_decay(r["ended_at"], now), 4)
            o["score_parts"] = {"cosine": 0.0, "entity_overlap": 0.0,
                                "recency": round(recency_decay(r["ended_at"], now), 4)}
            o["matched_entities"] = []
            scored.append(o)
        degraded = degraded or "no vector hits; SQLite LIKE fallback"

    scored.sort(key=lambda o: o["score"], reverse=True)
    top = scored[:limit]
    return {
        "query": query,
        "cleaned_query": cleaned,
        "window": {
            "from": datetime.fromtimestamp(time_from).isoformat(timespec="seconds") if time_from else None,
            "to": datetime.fromtimestamp(time_to).isoformat(timespec="seconds") if time_to else None,
        },
        "degraded": degraded,
        "results": top,
        "thread_arc": thread_arc(con, top[0]["thread_slug"]) if top else None,
    }


def what_was_i_doing(minutes_ago: int = 30) -> dict:
    con = connect()
    target = time.time() - minutes_ago * 60
    r = con.execute("SELECT * FROM sessions WHERE ? BETWEEN started_at AND ended_at"
                    " ORDER BY started_at DESC LIMIT 1", (int(target),)).fetchone()
    if not r:
        r = con.execute("SELECT * FROM sessions WHERE ended_at <= ?"
                        " ORDER BY ended_at DESC LIMIT 1", (int(target),)).fetchone()
    if not r:
        return {"as_of": datetime.fromtimestamp(target).isoformat(timespec="seconds"),
                "minutes_ago": minutes_ago, "session": None, "thread_arc": None,
                "resume_hint": None,
                "note": "no closed session covers that moment yet - run sessionize.py"}

    frames = con.execute("SELECT summary, app, ts FROM extractions WHERE ts BETWEEN ?"
                         " AND ? ORDER BY ts", (r["started_at"], r["ended_at"])).fetchall()
    s = session_obj(r)
    return {
        "as_of": datetime.fromtimestamp(target).isoformat(timespec="seconds"),
        "minutes_ago": minutes_ago,
        "session": s,
        "resume_hint": s["resume_hint"],
        "interruptions": json.loads(r["interruptions"] or "[]"),
        "frames": [{"at": datetime.fromtimestamp(f["ts"]).isoformat(timespec="seconds"),
                    "app": f["app"], "summary": f["summary"]} for f in frames],
        "thread_arc": thread_arc(con, r["thread_slug"]),
    }


def todays_receipt() -> dict:
    con = connect()
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    rows = con.execute("SELECT * FROM sessions WHERE started_at >= ?"
                       " ORDER BY started_at", (int(midnight),)).fetchall()
    sessions = [session_obj(r) for r in rows]

    by_thread, ents, interruptions = {}, [], 0
    for r, s in zip(rows, sessions):
        t = by_thread.setdefault(s["thread_slug"], {"thread_slug": s["thread_slug"],
                                                    "title": s["title"], "minutes": 0,
                                                    "sessions": 0})
        t["minutes"] += s["duration_min"]
        t["sessions"] += 1
        interruptions += len(json.loads(r["interruptions"] or "[]"))
        for e in s["entities"]:
            if e not in ents:
                ents.append(e)

    threads = sorted(by_thread.values(), key=lambda t: t["minutes"], reverse=True)
    focus = sum(s["duration_min"] for s in sessions)
    deep = sum(t["minutes"] for t in threads if "break" not in t["thread_slug"])
    biggest = threads[0] if threads else None

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "headline": (
            f"{deep} minutes of focused work across {len(threads)} threads - "
            f"most of it on {biggest['title']}." if biggest else
            "Nothing captured yet today."),
        "focus_minutes": focus,
        "deep_work_minutes": deep,
        "sessions_count": len(sessions),
        "threads": threads,
        "accomplished": [s["narrative"] for s in sessions],
        "touched": ents[:25],
        "interruptions_absorbed": interruptions,
        "picked_up_next": [s["resume_hint"] for s in reversed(sessions)
                           if s["resume_hint"]][:3],
        "sessions": sessions,
    }


# --- cli ------------------------------------------------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "receipt"
    if cmd == "recall":
        out = recall_context(" ".join(sys.argv[2:]) or "what was I doing")
    elif cmd == "doing":
        out = what_was_i_doing(int(sys.argv[2]) if len(sys.argv) > 2 else 30)
    else:
        out = todays_receipt()
    print(json.dumps(out, indent=2, default=str))
