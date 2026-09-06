#!/usr/bin/env python3
"""
sessionize.py -- roll contiguous extracted frames into sessions.

Boundary rule: a session ends when the task_thread stays *away* from the
session's slug for 3 consecutive frames. A 1-2 frame detour reverts and is
absorbed into the session's `interruptions` list instead of splitting it.
Also closes on an idle gap > 10 min and on day rollover.

On close: one Gemini call over the frames' summaries -> title, narrative,
resume_hint, key_entities. With no GEMINI_API_KEY (or if the call fails) it
falls back to a deterministic summary so the rest of the chain still runs.

Idempotent: sessions are keyed by started_at. An unchanged session is left
alone (no second Gemini call); a session that grew is re-summarised in place,
keeping its id and vector_id. The still-open tail session is only written once
it is genuinely over (a boundary closed it, it has been idle > 10 min, or
--flush).

    uv run sessionize.py
    uv run sessionize.py --flush     # also close the trailing open session
    uv run sessionize.py --no-llm    # deterministic summaries, zero API calls
    uv run sessionize.py --reset     # rebuild every session from scratch
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path
from typing import List

from pydantic import BaseModel

MODEL = "gemini-2.5-flash"
IDLE_GAP_S = 10 * 60
BOUNDARY_RUN = 3          # frames away from the slug before we split
NOMINAL_FRAME_S = 60      # a single-frame session still lasted *something*

MEM_DIR = Path(os.environ.get("MEM_DIR", Path.home() / "mem"))
DB_PATH = Path(os.environ.get("MEM_DB", MEM_DIR / "mem.db"))


class SessionSummary(BaseModel):
    title: str
    narrative: str
    resume_hint: str
    key_entities: List[str]


SYSTEM_INSTRUCTION = """\
You write the memory card for one work session, from the per-frame summaries of
what was on screen, in order.
- title: 4-8 words, specific, no trailing punctuation. "CORS preflight 401 in mem-api".
- narrative: 2-3 past-tense sentences telling what was actually done and how it
  ended. Name files, errors, tickets. No filler, no "the user".
- resume_hint: the single literal next action, with file and line if the frames
  show one. An instruction, not a description.
- key_entities: up to 8 concrete strings from the frames - filenames, errors,
  URLs, ticket ids. Verbatim, most important first.
"""


# --- db -------------------------------------------------------------------
def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def ensure_schema(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            thread_slug   TEXT NOT NULL,
            title         TEXT,
            started_at    INTEGER NOT NULL,
            ended_at      INTEGER NOT NULL,
            duration_s    INTEGER,
            narrative     TEXT,
            resume_hint   TEXT,
            key_entities  TEXT,      -- json list
            keyframe_path TEXT,
            interruptions TEXT,      -- json list of {slug, started_at, ended_at, frames, summary}
            vector_id     TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_thread ON sessions(thread_slug);
        CREATE INDEX IF NOT EXISTS idx_sessions_ended ON sessions(ended_at);

        CREATE TABLE IF NOT EXISTS threads (
            slug            TEXT PRIMARY KEY,
            title           TEXT,
            first_seen      INTEGER,
            last_seen       INTEGER,
            total_seconds   INTEGER DEFAULT 0,
            status          TEXT,
            rolling_summary TEXT
        );

        CREATE TABLE IF NOT EXISTS entities (
            session_id INTEGER NOT NULL,
            entity     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entities_entity ON entities(entity);
        CREATE INDEX IF NOT EXISTS idx_entities_session ON entities(session_id);
    """)
    con.commit()


# --- grouping -------------------------------------------------------------
def day_of(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def group_frames(rows):
    """-> list of dicts {slug, frames, interruptions, closed}. Pure, no db."""
    sessions = []
    cur = None       # {"slug":..., "frames":[...], "interruptions":[...]}
    run = []         # contiguous frames whose slug != cur["slug"]

    def absorb(run_frames):
        """A short detour: keep the frames in the session, note the interruption."""
        if not run_frames:
            return
        cur["interruptions"].append({
            "slug": run_frames[0]["task_thread"],
            "started_at": run_frames[0]["ts"],
            "ended_at": run_frames[-1]["ts"],
            "frames": len(run_frames),
            "summary": run_frames[0]["summary"],
        })
        cur["frames"].extend(run_frames)

    def close(closed=True):
        nonlocal cur
        if cur and cur["frames"]:
            cur["closed"] = closed
            cur["frames"].sort(key=lambda f: f["ts"])
            sessions.append(cur)
        cur = None

    for f in rows:
        if cur is None:
            cur = {"slug": f["task_thread"], "frames": [f], "interruptions": [],
                   "closed": False}
            run = []
            continue

        last_ts = max(cur["frames"][-1]["ts"], run[-1]["ts"] if run else 0)
        if f["ts"] - last_ts > IDLE_GAP_S or day_of(f["ts"]) != day_of(last_ts):
            absorb(run)
            run = []
            close()
            cur = {"slug": f["task_thread"], "frames": [f], "interruptions": [],
                   "closed": False}
            continue

        if f["task_thread"] == cur["slug"]:
            absorb(run)          # the detour reverted -> it was a blip
            run = []
            cur["frames"].append(f)
            continue

        run.append(f)
        if len(run) >= BOUNDARY_RUN:
            close()              # the thread stayed changed: real boundary
            slug = Counter(r["task_thread"] for r in run).most_common(1)[0][0]
            cur = {"slug": slug, "frames": [], "interruptions": [], "closed": False}
            minority = [r for r in run if r["task_thread"] != slug]
            cur["frames"] = [r for r in run if r["task_thread"] == slug]
            if minority:
                absorb(minority)
            run = []

    if cur:
        absorb(run)
        close(closed=False)      # trailing session may still be running
    return sessions


# --- summarising ----------------------------------------------------------
USAGE = {"calls": 0, "prompt": 0, "output": 0, "total": 0}
_client = None


def client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _client


def pretty(slug):
    return slug.replace("-", " ").strip().capitalize()


def fallback_summary(slug, frames, ents):
    first, last = frames[0]["summary"], frames[-1]["summary"]
    narrative = first if len(frames) == 1 else f"{first} {last}"
    return SessionSummary(
        title=pretty(slug),
        narrative=narrative,
        resume_hint=next((f["resume_hint"] for f in reversed(frames)
                          if f["resume_hint"]), ""),
        key_entities=ents[:8],
    )


def summarise(slug, frames, ents, no_llm=False):
    if no_llm or not os.environ.get("GEMINI_API_KEY"):  # off unless --llm
        return fallback_summary(slug, frames, ents)
    from google.genai import types
    lines = [f"task_thread: {slug}",
             f"span: {time.strftime('%a %H:%M', time.localtime(frames[0]['ts']))}"
             f" - {time.strftime('%H:%M', time.localtime(frames[-1]['ts']))}",
             f"apps: {', '.join(sorted({f['app'] for f in frames if f['app']}))}",
             "", "frame summaries in order:"]
    for f in frames:
        lines.append(f"- [{time.strftime('%H:%M', time.localtime(f['ts']))}] {f['summary']}")
    lines += ["", "entities seen: " + (", ".join(ents[:30]) or "(none)"),
              "last resume_hint: " + (frames[-1]["resume_hint"] or "(none)")]
    cfg = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.2,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        response_mime_type="application/json",
        response_schema=SessionSummary,
    )
    for attempt in range(4):
        try:
            r = client().models.generate_content(
                model=MODEL, contents="\n".join(lines), config=cfg)
            u = getattr(r, "usage_metadata", None)
            if u:
                USAGE["calls"] += 1
                USAGE["prompt"] += getattr(u, "prompt_token_count", 0) or 0
                USAGE["output"] += getattr(u, "candidates_token_count", 0) or 0
                USAGE["total"] += getattr(u, "total_token_count", 0) or 0
            return r.parsed or SessionSummary(**json.loads(r.text))
        except Exception as e:
            import extract                      # one definition of "retryable"
            if attempt == 3 or not extract._retryable(e):
                print(f"[gemini] giving up on {slug}: {e}", file=sys.stderr)
                return fallback_summary(slug, frames, ents)
            time.sleep(2 ** (attempt + 1))


# --- write-back -----------------------------------------------------------
def keyframe(frames):
    best = max(frames, key=lambda f: (f["ocr_chars"] or 0, len(f["summary"] or "")))
    return best["image_path"]


def upsert_session(con, s, no_llm):
    frames = s["frames"]
    started, ended = frames[0]["ts"], frames[-1]["ts"]
    ents, seen = [], set()
    for f in frames:
        for e in json.loads(f["entities"] or "[]"):
            k = e.lower()
            if k not in seen:
                seen.add(k)
                ents.append(e)

    row = con.execute("SELECT id, ended_at FROM sessions WHERE started_at = ?",
                      (started,)).fetchone()
    if row and row["ended_at"] == ended:
        return row["id"], False          # unchanged -> no Gemini call

    summary = summarise(s["slug"], frames, ents, no_llm)
    payload = (s["slug"], summary.title, started, ended,
               max(ended - started, NOMINAL_FRAME_S), summary.narrative,
               summary.resume_hint, json.dumps(summary.key_entities or ents[:8]),
               keyframe(frames), json.dumps(s["interruptions"]))

    if row:
        con.execute("""UPDATE sessions SET thread_slug=?, title=?, started_at=?,
                       ended_at=?, duration_s=?, narrative=?, resume_hint=?,
                       key_entities=?, keyframe_path=?, interruptions=?,
                       vector_id=NULL WHERE id=?""", payload + (row["id"],))
        sid = row["id"]
    else:
        cur = con.execute("""INSERT INTO sessions (thread_slug, title, started_at,
                             ended_at, duration_s, narrative, resume_hint,
                             key_entities, keyframe_path, interruptions, vector_id)
                             VALUES (?,?,?,?,?,?,?,?,?,?,NULL)""", payload)
        sid = cur.lastrowid

    con.execute("DELETE FROM entities WHERE session_id = ?", (sid,))
    con.executemany("INSERT INTO entities (session_id, entity) VALUES (?,?)",
                    [(sid, e) for e in (summary.key_entities or ents)[:24]])
    return sid, True


def refresh_threads(con):
    for r in con.execute("""SELECT thread_slug, MIN(started_at) f, MAX(ended_at) l,
                                   SUM(duration_s) t FROM sessions
                            GROUP BY thread_slug""").fetchall():
        narratives = [x["narrative"] for x in con.execute(
            "SELECT narrative FROM sessions WHERE thread_slug=? "
            "ORDER BY started_at DESC LIMIT 3", (r["thread_slug"],)).fetchall()]
        title = con.execute("SELECT title FROM sessions WHERE thread_slug=? "
                            "ORDER BY started_at DESC LIMIT 1",
                            (r["thread_slug"],)).fetchone()["title"]
        age = time.time() - r["l"]
        status = "active" if age < 8 * 3600 else ("recent" if age < 7 * 86400
                                                 else "dormant")
        con.execute("""INSERT INTO threads (slug, title, first_seen, last_seen,
                       total_seconds, status, rolling_summary)
                       VALUES (?,?,?,?,?,?,?)
                       ON CONFLICT(slug) DO UPDATE SET
                         title=excluded.title, first_seen=excluded.first_seen,
                         last_seen=excluded.last_seen,
                         total_seconds=excluded.total_seconds,
                         status=excluded.status,
                         rolling_summary=excluded.rolling_summary""",
                    (r["thread_slug"], title, r["f"], r["l"], r["t"], status,
                     " ".join(reversed(narratives))[:1000]))
    con.commit()


def run(con, flush=False, no_llm=False):
    rows = con.execute("""SELECT frame_id, ts, app, window_title, image_path, summary,
                                 task_thread, entities, resume_hint, activity_type,
                                 ocr_chars FROM extractions ORDER BY ts""").fetchall()
    if not rows:
        print("no extractions yet - run extract.py first")
        return
    groups = group_frames(rows)
    now = time.time()
    written = skipped = 0
    for g in groups:
        stale = now - g["frames"][-1]["ts"] > IDLE_GAP_S
        if not g["closed"] and not (flush or stale):
            print(f"  (open) {g['slug']}: {len(g['frames'])} frames still running")
            continue
        sid, changed = upsert_session(con, g, no_llm)
        written += changed
        skipped += (not changed)
        if changed:
            s = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            span = time.strftime("%H:%M", time.localtime(s["started_at"]))
            print(f"  #{sid} {span} {s['thread_slug']:<24} {len(g['frames'])}f "
                  f"{s['duration_s'] // 60}m  {len(g['interruptions'])} interruptions")
            print(f"      {s['title']}")
    con.commit()
    refresh_threads(con)
    print(f"{written} sessions written, {skipped} unchanged, "
          f"{len(groups)} groups from {len(rows)} frames")
    if USAGE["calls"]:
        print(f"[usage] {USAGE['calls']} {MODEL} calls, {USAGE['prompt']} prompt "
              f"+ {USAGE['output']} output = {USAGE['total']} tokens")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--flush", action="store_true",
                    help="close the trailing session even if it may still be running")
    ap.add_argument("--no-llm", action="store_true", default=True,
                    help="(default) deterministic narratives, no API calls")
    ap.add_argument("--llm", dest="no_llm", action="store_false",
                    help="opt in to Gemini session summaries (needs GEMINI_API_KEY)")
    ap.add_argument("--reset", action="store_true",
                    help="drop sessions/threads/entities and rebuild")
    args = ap.parse_args()

    con = connect()
    ensure_schema(con)
    if args.reset:
        con.executescript("DELETE FROM sessions; DELETE FROM threads; DELETE FROM entities;")
        con.commit()
        print("reset: sessions cleared")
    run(con, flush=args.flush, no_llm=args.no_llm)
    con.close()


if __name__ == "__main__":
    main()
